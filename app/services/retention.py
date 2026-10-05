"""Deleting data that has outlived its purpose (issue #269).

The periods are in app/config.py and the reasoning for each is in
operations/retention.md. This module only applies them. It is run daily by
Cloud Scheduler through POST /jobs/purge-expired, well away from the 06:28 race.

Each rule deletes one kind of row and reports how many. Rules are independent:
one failing is logged and does not stop the others.

What is deliberately *not* here:

* member_pseudonyms. A label is never reused - an old public race report must
  not come to mean someone else - so these rows outlive the member.
* Live bookings. Only rows in a terminal status are ever deleted, so a booking
  the racer may still claim or report on is never touched.
* A credential that is merely old. A login is deleted when the member forgets
  it or leaves (telegram_members), or here when Walden has rejected it and
  nobody has replaced it. An unused but working login is the member's to remove.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import (
    AsyncSessionLocal,
    BookingRecord,
    SessionRecord,
    TeeSheetGridRecord,
    WaldenCredentialRecord,
)
from app.models.schemas import BookingStatus, ConversationState

logger = logging.getLogger(__name__)

# A booking in one of these can no longer change. PENDING, SCHEDULED and
# IN_PROGRESS are still the racer's or the reconciler's to act on.
_TERMINAL_BOOKING_STATUSES = (
    BookingStatus.SUCCESS,
    BookingStatus.FAILED,
    BookingStatus.CANCELLED,
)


@dataclass
class PurgeResult:
    """Rows deleted (or, on a dry run, that would be) per table; errors per rule."""

    dry_run: bool
    deleted: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _utcnow() -> datetime:
    """Naive UTC, which is what every created_at / updated_at column holds."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _apply(
    db: AsyncSession, table: type, condition: ColumnElement[bool], dry_run: bool
) -> int:
    if dry_run:
        count = await db.scalar(select(func.count()).select_from(table).where(condition))
        return int(count or 0)
    result = await db.execute(delete(table).where(condition))
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def purge_expired(dry_run: bool = False, now: datetime | None = None) -> PurgeResult:
    """Delete rows older than their retention period.

    Args:
        dry_run: Count what would be deleted and delete nothing.
        now: Naive UTC "now", for tests.
    """
    now = now or _utcnow()
    result = PurgeResult(dry_run=dry_run)

    rules: dict[str, tuple[type, ColumnElement[bool]]] = {
        # Idle sessions carry no state worth keeping: the next message from the
        # member creates a fresh one. A session mid-conversation is never idle.
        "sessions": (
            SessionRecord,
            (SessionRecord.state == ConversationState.IDLE)
            & (
                SessionRecord.last_interaction
                < now - timedelta(days=settings.retention_session_days)
            ),
        ),
        # Judged by the date played, not by when the row was written: a request
        # made months ahead is not "old" until its date is.
        "bookings": (
            BookingRecord,
            BookingRecord.status.in_(_TERMINAL_BOOKING_STATUSES)
            & (
                BookingRecord.requested_date
                < (now - timedelta(days=settings.retention_booking_days)).date()
            ),
        ),
        # Appended, never updated, and the latest reading of a date is the one
        # used. Past dates are never read by a booking conversation; the older
        # readings only serve a post-mortem.
        "tee_sheet_grids": (
            TeeSheetGridRecord,
            TeeSheetGridRecord.captured_at
            < now - timedelta(days=settings.retention_tee_sheet_grid_days),
        ),
        # Walden refused this login and nothing has replaced it since (saving a
        # new one clears invalid_since). It cannot book, and it is still the
        # member's real password, encrypted.
        "walden_credentials_invalid": (
            WaldenCredentialRecord,
            WaldenCredentialRecord.invalid_since.is_not(None)
            & (
                WaldenCredentialRecord.invalid_since
                < now - timedelta(days=settings.retention_invalid_login_days)
            ),
        ),
    }

    for name, (table, condition) in rules.items():
        try:
            async with AsyncSessionLocal() as db:
                result.deleted[name] = await _apply(db, table, condition, dry_run)
        except Exception as e:
            logger.exception("RETENTION_PURGE: %s failed", name)
            result.errors[name] = type(e).__name__

    logger.info(
        "RETENTION_PURGE: %s deleted=%s errors=%s",
        "dry run" if dry_run else "done",
        result.deleted,
        result.errors,
    )
    return result
