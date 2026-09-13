"""Tests for the racer job (issue #184): the claim, and one task's morning.

The claim runs against a real in-memory SQLite database, because what matters
about it is the SQL - that a conditional UPDATE lets exactly one task take a
group. The task's run is tested with the claim and the race stubbed out.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import log_safety
from app.api.jobs import JobExecutionResult
from app.models.database import Base
from app.models.schemas import BookingStatus, TeeTimeBooking, TeeTimeRequest
from app.observer import run as observer_run
from app.racer import run as racer_run
from app.services.database_service import DatabaseService
from app.utils.timezone import CTDateTime

DUE_BEFORE = datetime(2026, 9, 13, 6, 30)


@pytest_asyncio.fixture
async def database_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.database_service.AsyncSessionLocal", session_local)
    yield DatabaseService()
    await engine.dispose()


def _booking(
    booking_id: str,
    phone: str,
    when: date = date(2026, 9, 20),
    at: time = time(12, 0),
    status: BookingStatus = BookingStatus.SCHEDULED,
) -> TeeTimeBooking:
    return TeeTimeBooking(
        id=booking_id,
        phone_number=phone,
        request=TeeTimeRequest(requested_date=when, requested_time=at, num_players=4),
        status=status,
        scheduled_execution_time=datetime(2026, 9, 13, 6, 30),
    )


async def _statuses(service: DatabaseService) -> dict[str, BookingStatus]:
    return {b.id or "": b.status for b in await service.get_bookings()}


class TestClaimNextDueGroup:
    @pytest.mark.asyncio
    async def test_nothing_due_claims_nothing(self, database_service: DatabaseService) -> None:
        assert await database_service.claim_next_due_group(DUE_BEFORE) == []

    @pytest.mark.asyncio
    async def test_claims_one_requesters_group_and_nobody_elses(
        self, database_service: DatabaseService
    ) -> None:
        await database_service.create_booking(_booking("ron", "8501282320", at=time(12, 8)))
        await database_service.create_booking(_booking("melissa", "8537795292"))

        claimed = await database_service.claim_next_due_group(DUE_BEFORE)

        assert [b.id for b in claimed] == ["ron"]
        assert claimed[0].status == BookingStatus.IN_PROGRESS
        assert await _statuses(database_service) == {
            "ron": BookingStatus.IN_PROGRESS,
            "melissa": BookingStatus.SCHEDULED,
        }

    @pytest.mark.asyncio
    async def test_each_claim_takes_the_next_group_until_none_are_left(
        self, database_service: DatabaseService
    ) -> None:
        await database_service.create_booking(_booking("ron", "8501282320"))
        await database_service.create_booking(_booking("melissa", "8537795292"))

        first = await database_service.claim_next_due_group(DUE_BEFORE)
        second = await database_service.claim_next_due_group(DUE_BEFORE)
        third = await database_service.claim_next_due_group(DUE_BEFORE)

        assert [b.id for b in first] == ["ron"]
        assert [b.id for b in second] == ["melissa"]
        assert third == []

    @pytest.mark.asyncio
    async def test_one_requesters_bookings_on_one_date_are_one_group(
        self, database_service: DatabaseService
    ) -> None:
        """One login's work stays in one container - the same grouping as the batch."""
        await database_service.create_booking(_booking("late", "8501282320", at=time(12, 8)))
        await database_service.create_booking(_booking("early", "8501282320", at=time(12, 0)))

        claimed = await database_service.claim_next_due_group(DUE_BEFORE)

        assert [b.id for b in claimed] == ["early", "late"]

    @pytest.mark.asyncio
    async def test_one_requester_on_two_dates_is_two_groups(
        self, database_service: DatabaseService
    ) -> None:
        await database_service.create_booking(_booking("sat", "8501282320", when=date(2026, 9, 19)))
        await database_service.create_booking(_booking("sun", "8501282320", when=date(2026, 9, 20)))

        first = await database_service.claim_next_due_group(DUE_BEFORE)
        second = await database_service.claim_next_due_group(DUE_BEFORE)

        assert [b.id for b in first] == ["sat"]
        assert [b.id for b in second] == ["sun"]

    @pytest.mark.asyncio
    async def test_rows_no_longer_scheduled_are_never_claimed(
        self, database_service: DatabaseService
    ) -> None:
        await database_service.create_booking(
            _booking("racing", "8501282320", status=BookingStatus.IN_PROGRESS)
        )
        await database_service.create_booking(
            _booking("cancelled", "8537795292", status=BookingStatus.CANCELLED)
        )

        assert await database_service.claim_next_due_group(DUE_BEFORE) == []

    @pytest.mark.asyncio
    async def test_a_group_lost_to_another_task_is_skipped(
        self, database_service: DatabaseService
    ) -> None:
        """The read said the group was free; the update is what actually decides.

        Another task claims Ron's group between this task's read and its
        update. The update must move nothing, and the task must go on to the
        next group rather than racing Ron's booking a second time.
        """
        await database_service.create_booking(_booking("ron", "8501282320"))
        await database_service.create_booking(_booking("melissa", "8537795292"))
        stale_read = await database_service.get_due_bookings(DUE_BEFORE)
        assert [b.id for b in await database_service.claim_next_due_group(DUE_BEFORE)] == ["ron"]

        real_read = database_service.get_due_bookings
        reads = AsyncMock(side_effect=[stale_read, await real_read(DUE_BEFORE)])
        with patch.object(database_service, "get_due_bookings", new=reads):
            claimed = await database_service.claim_next_due_group(DUE_BEFORE)

        assert [b.id for b in claimed] == ["melissa"]
        assert reads.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_claims_never_share_a_group(
        self, database_service: DatabaseService
    ) -> None:
        await database_service.create_booking(_booking("ron", "8501282320"))
        await database_service.create_booking(_booking("melissa", "8537795292"))

        results = await asyncio.gather(
            *(database_service.claim_next_due_group(DUE_BEFORE) for _ in range(3))
        )

        claimed_ids = sorted(b.id or "" for group in results for b in group)
        assert claimed_ids == ["melissa", "ron"]
        assert sum(1 for group in results if not group) == 1

    @pytest.mark.asyncio
    async def test_claims_that_keep_coming_back_empty_raise_instead_of_dropping_bookings(
        self, database_service: DatabaseService
    ) -> None:
        """An empty answer would mean "nothing to race" while a booking is still due."""
        await database_service.create_booking(
            _booking("gone", "8501282320", status=BookingStatus.CANCELLED)
        )
        phantom = [_booking("gone", "8501282320")]
        with (
            patch.object(database_service, "get_due_bookings", new=AsyncMock(return_value=phantom)),
            pytest.raises(RuntimeError, match="Could not claim"),
        ):
            await database_service.claim_next_due_group(DUE_BEFORE)


class TestDueBookingsOrder:
    @pytest.mark.asyncio
    async def test_due_bookings_come_back_in_a_stated_order(
        self, database_service: DatabaseService
    ) -> None:
        """Not whatever the planner returns - 2026-09-13's groups ran in an order nobody chose."""
        await database_service.create_booking(_booking("b-late", "2", at=time(12, 8)))
        await database_service.create_booking(_booking("b-early", "2", at=time(12, 0)))
        await database_service.create_booking(_booking("a", "1", at=time(13, 0)))
        await database_service.create_booking(_booking("sat", "3", when=date(2026, 9, 19)))

        due = await database_service.get_due_bookings(DUE_BEFORE)

        assert [b.id for b in due] == ["sat", "a", "b-early", "b-late"]


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 13, hour, minute, second, tzinfo=CTDateTime.CT_TZ)


def _result(succeeded: int = 1, failed: int = 0) -> JobExecutionResult:
    return JobExecutionResult(
        executed_at=_at(6, 25),
        total_due=succeeded + failed,
        succeeded=succeeded,
        failed=failed,
        results=[],
    )


class _RaceHarness:
    """Stubs for everything one task touches, so a test can say what happened."""

    def __init__(self, claimed: list[TeeTimeBooking], now: datetime) -> None:
        self.claim = AsyncMock(return_value=claimed)
        # Synchronous in the code, so a MagicMock: an AsyncMock's side_effect
        # only fires when the returned coroutine is awaited, and nobody awaits it.
        self.install = MagicMock()
        self.run = AsyncMock(return_value=_result())
        self.sleep = AsyncMock()
        self.notify = AsyncMock()
        self.now = now

    def __enter__(self) -> "_RaceHarness":
        self._patches = [
            patch.object(racer_run.database_service, "claim_next_due_group", new=self.claim),
            patch.object(racer_run, "install_reservation_provider", new=self.install),
            patch.object(racer_run, "run_bookings_and_report", new=self.run),
            patch.object(racer_run.asyncio, "sleep", new=self.sleep),
            patch.object(racer_run.booking_service, "notify_unreported_bookings", new=self.notify),
            patch.object(racer_run.CTDateTime, "now", return_value=self.now),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestRace:
    @pytest.mark.asyncio
    async def test_nothing_to_claim_exits_cleanly_without_touching_the_club(self) -> None:
        with _RaceHarness(claimed=[], now=_at(6, 27)) as h:
            assert await racer_run.race() is True

        h.claim.assert_awaited_once_with(datetime(2026, 9, 13, 6, 30))
        h.install.assert_not_called()
        h.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_races_the_claimed_group_at_the_window_after_holding_to_0628(self) -> None:
        claimed = [_booking("ron", "8501282320", status=BookingStatus.IN_PROGRESS)]
        with _RaceHarness(claimed=claimed, now=_at(6, 27)) as h:
            assert await racer_run.race() is True

        h.install.assert_called_once_with(racer_run.booking_service)
        h.sleep.assert_awaited_once_with(60.0)
        h.run.assert_awaited_once()
        assert h.run.await_args.args == (claimed,)
        assert h.run.await_args.kwargs["execute_at"] == datetime(2026, 9, 13, 6, 30)

    @pytest.mark.asyncio
    async def test_a_late_start_races_at_once_and_says_so(self, caplog: Any) -> None:
        claimed = [_booking("ron", "8501282320", status=BookingStatus.IN_PROGRESS)]
        with (
            caplog.at_level(logging.ERROR, logger="app.racer.run"),
            _RaceHarness(claimed=claimed, now=_at(6, 28, 30)) as h,
        ):
            assert await racer_run.race() is True

        h.sleep.assert_not_awaited()
        h.run.assert_awaited_once()
        assert "started late" in caplog.text

    @pytest.mark.asyncio
    async def test_a_refused_booking_is_not_a_failed_task(self) -> None:
        """The member has been told; a red task per lost slot would bury real breakage."""
        claimed = [_booking("melissa", "8537795292", status=BookingStatus.IN_PROGRESS)]
        with _RaceHarness(claimed=claimed, now=_at(6, 27)) as h:
            h.run.return_value = _result(succeeded=0, failed=1)
            assert await racer_run.race() is True

    @pytest.mark.asyncio
    async def test_a_failure_around_the_race_still_tells_the_members(self) -> None:
        """Claimed rows belong to this task alone - if it goes quiet, nobody reports them."""
        claimed = [_booking("ron", "8501282320", status=BookingStatus.IN_PROGRESS)]
        with _RaceHarness(claimed=claimed, now=_at(6, 27)) as h:
            h.install.side_effect = RuntimeError("no provider")
            assert await racer_run.race() is False

        h.run.assert_not_awaited()
        h.notify.assert_awaited_once_with(claimed, "no provider")


class TestLoginLead:
    def test_login_is_held_to_the_same_0628_the_service_always_used(self) -> None:
        assert timedelta(seconds=racer_run.LOGIN_LEAD_S) == timedelta(minutes=2)

    def test_the_observer_checks_its_ordering_against_the_racers_real_login(self) -> None:
        """The observer cannot import the racer (it may link no booking code), so
        the two constants are kept in step by this test instead."""
        assert observer_run.RACER_LOGIN_LEAD_S == racer_run.LOGIN_LEAD_S


class TestWireLoggersAreSilenced:
    """The racer logs in with a member's password at LOG_LEVEL=DEBUG (CWE-532)."""

    @pytest.mark.parametrize("name", log_safety.WIRE_LOGGERS)
    def test_main_pins_each_wire_logger_at_warning(self, name: str) -> None:
        def _close_without_running(coro: Any) -> bool:
            coro.close()  # main() builds race(); nothing here should drive a browser
            return True

        logging.getLogger(name).setLevel(logging.DEBUG)
        with (
            patch.object(racer_run.settings, "log_level", "DEBUG"),
            patch.object(racer_run.asyncio, "run", side_effect=_close_without_running),
        ):
            assert racer_run.main() == 0
        assert logging.getLogger(name).level == logging.WARNING
