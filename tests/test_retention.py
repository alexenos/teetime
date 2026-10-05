"""Tests for the retention purge (issue #269): what it deletes, and what it must not."""

from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import (
    Base,
    BookingRecord,
    MemberPseudonymRecord,
    SessionRecord,
    TeeSheetGridRecord,
    WaldenCredentialRecord,
)
from app.models.schemas import BookingStatus, ConversationState
from app.services.retention import purge_expired

NOW = datetime(2026, 10, 5, 12, 0)


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.services.retention.AsyncSessionLocal", session_local)
    yield session_local
    await engine.dispose()


def _ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def _booking(booking_id: str, status: BookingStatus, days_since_played: int) -> BookingRecord:
    return BookingRecord(
        booking_id=booking_id,
        phone_number="1",
        requested_date=(NOW - timedelta(days=days_since_played)).date(),
        requested_time=time(8, 0),
        status=status,
    )


async def _ids(db: async_sessionmaker, column: object) -> set[object]:
    async with db() as s:
        return set((await s.execute(select(column))).scalars().all())


@pytest.mark.asyncio
async def test_old_finished_bookings_go_and_live_ones_stay(db: async_sessionmaker) -> None:
    old = settings.retention_booking_days + 1
    async with db() as s:
        s.add_all(
            [
                _booking("old-ok", BookingStatus.SUCCESS, old),
                _booking("old-failed", BookingStatus.FAILED, old),
                _booking("old-cancelled", BookingStatus.CANCELLED, old),
                # A date this old with a non-terminal status is a stuck row, and
                # still not one for a retention job to decide about.
                _booking("old-pending", BookingStatus.PENDING, old),
                _booking("old-scheduled", BookingStatus.SCHEDULED, old),
                _booking("old-in-progress", BookingStatus.IN_PROGRESS, old),
                _booking("recent-ok", BookingStatus.SUCCESS, 10),
                # Played in the future, finished early: judged by the date played.
                _booking("future-cancelled", BookingStatus.CANCELLED, -5),
            ]
        )
        await s.commit()

    result = await purge_expired(now=NOW)

    assert result.deleted["bookings"] == 3
    assert await _ids(db, BookingRecord.booking_id) == {
        "old-pending",
        "old-scheduled",
        "old-in-progress",
        "recent-ok",
        "future-cancelled",
    }


@pytest.mark.asyncio
async def test_only_idle_old_sessions_go(db: async_sessionmaker) -> None:
    old = _ago(settings.retention_session_days + 1)
    async with db() as s:
        s.add_all(
            [
                SessionRecord(
                    phone_number="idle-old", state=ConversationState.IDLE, last_interaction=old
                ),
                SessionRecord(
                    phone_number="mid-conversation-old",
                    state=ConversationState.AWAITING_CONFIRMATION,
                    last_interaction=old,
                ),
                SessionRecord(
                    phone_number="idle-recent",
                    state=ConversationState.IDLE,
                    last_interaction=_ago(1),
                ),
            ]
        )
        await s.commit()

    result = await purge_expired(now=NOW)

    assert result.deleted["sessions"] == 1
    assert await _ids(db, SessionRecord.phone_number) == {"mid-conversation-old", "idle-recent"}


@pytest.mark.asyncio
async def test_old_grid_readings_go(db: async_sessionmaker) -> None:
    async with db() as s:
        for name, age in (("old", settings.retention_tee_sheet_grid_days + 1), ("new", 1)):
            s.add(
                TeeSheetGridRecord(
                    sheet_date=date(2026, 10, 12),
                    captured_at=_ago(age),
                    slots_json=name,
                    slot_count=0,
                    source="observer",
                )
            )
        await s.commit()

    result = await purge_expired(now=NOW)

    assert result.deleted["tee_sheet_grids"] == 1
    assert await _ids(db, TeeSheetGridRecord.slots_json) == {"new"}


@pytest.mark.asyncio
async def test_only_a_long_rejected_login_goes(db: async_sessionmaker) -> None:
    def cred(who: str, invalid_since: datetime | None, updated: datetime) -> WaldenCredentialRecord:
        return WaldenCredentialRecord(
            phone_number=who,
            member_number_encrypted="x",
            password_encrypted="x",
            invalid_since=invalid_since,
            updated_at=updated,
        )

    stale = _ago(settings.retention_invalid_login_days + 1)
    async with db() as s:
        s.add_all(
            [
                cred("rejected-long-ago", stale, stale),
                cred("rejected-lately", _ago(2), _ago(2)),
                # Untouched for years but never rejected: the member's to remove.
                cred("working-but-unused", None, _ago(1000)),
            ]
        )
        await s.commit()

    result = await purge_expired(now=NOW)

    assert result.deleted["walden_credentials_invalid"] == 1
    assert await _ids(db, WaldenCredentialRecord.phone_number) == {
        "rejected-lately",
        "working-but-unused",
    }


@pytest.mark.asyncio
async def test_pseudonyms_are_never_deleted(db: async_sessionmaker) -> None:
    async with db() as s:
        s.add(MemberPseudonymRecord(requester_id="1", label="Member A", assigned_at=_ago(5000)))
        await s.commit()

    await purge_expired(now=NOW)

    assert await _ids(db, MemberPseudonymRecord.label) == {"Member A"}


@pytest.mark.asyncio
async def test_dry_run_counts_and_deletes_nothing(db: async_sessionmaker) -> None:
    async with db() as s:
        s.add(_booking("old", BookingStatus.SUCCESS, settings.retention_booking_days + 1))
        await s.commit()

    result = await purge_expired(dry_run=True, now=NOW)

    assert result.dry_run is True
    assert result.deleted["bookings"] == 1
    assert await _ids(db, BookingRecord.booking_id) == {"old"}


@pytest.mark.asyncio
async def test_one_failing_rule_does_not_stop_the_others(
    db: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db() as s:
        s.add(
            SessionRecord(
                phone_number="idle-old",
                state=ConversationState.IDLE,
                last_interaction=_ago(settings.retention_session_days + 1),
            )
        )
        await s.commit()

    real_apply = __import__("app.services.retention", fromlist=["_apply"])._apply

    async def flaky(session: object, table: type, condition: object, dry_run: bool) -> int:
        if table is BookingRecord:
            raise RuntimeError("connection reset")
        return await real_apply(session, table, condition, dry_run)

    monkeypatch.setattr("app.services.retention._apply", flaky)

    result = await purge_expired(now=NOW)

    assert result.errors == {"bookings": "RuntimeError"}
    assert result.deleted["sessions"] == 1


def test_purge_endpoint_requires_scheduler_auth() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).post("/jobs/purge-expired")

    assert response.status_code in (401, 403, 422)
