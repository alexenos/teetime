"""The 06:30 CT race keeps its wall-clock time across daylight-saving changes (#481).

``tests/test_timezone.py`` already checks ``CTDateTime``'s conversions at single
instants. What it did not check is the chain a booking actually travels on a
transition day:

1. ``BookingService._calculate_execution_time`` stores the run time as naive CT.
2. The racer and observer build "this morning's 06:30" with ``_window_instant``.
3. That window, made naive by ``CTDateTime.to_naive_ct``, is the cutoff the
   database compares stored run times against: ``get_due_bookings`` for the
   observer, ``claim_next_due_group`` for the racer.

If any step used UTC or a fixed offset instead of ``America/Chicago``, a booking
whose window opens on a transition day would run an hour early or late, or not
be found as due. The transition days used are the real ones: 2026-11-01 (fall
back, CDT -> CST) and 2027-03-14 (spring forward, CST -> CDT).
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.database import Base
from app.models.schemas import BookingStatus, TeeTimeBooking, TeeTimeRequest
from app.observer import run as observer_run
from app.racer import run as racer_run
from app.services.booking_service import BookingService
from app.services.database_service import DatabaseService
from app.utils.timezone import CTDateTime

FALL_BACK = date(2026, 11, 1)
SPRING_FORWARD = date(2027, 3, 14)

# The UTC instant of 06:30 CT on each transition day: CST is UTC-6, CDT UTC-5.
WINDOW_UTC = {
    FALL_BACK: datetime(2026, 11, 1, 12, 30, tzinfo=UTC),
    SPRING_FORWARD: datetime(2027, 3, 14, 11, 30, tzinfo=UTC),
}

# Target dates whose booking window opens on, just before, or just after each
# transition. The open date is always the target minus seven days.
TARGETS = [
    pytest.param(FALL_BACK + timedelta(days=7), id="opens-on-fall-back"),
    pytest.param(FALL_BACK + timedelta(days=3), id="opens-before-fall-back-plays-after"),
    pytest.param(FALL_BACK + timedelta(days=14), id="opens-week-after-fall-back"),
    pytest.param(SPRING_FORWARD + timedelta(days=7), id="opens-on-spring-forward"),
    pytest.param(SPRING_FORWARD + timedelta(days=3), id="opens-before-spring-forward-plays-after"),
    pytest.param(SPRING_FORWARD + timedelta(days=14), id="opens-week-after-spring-forward"),
]


@pytest.fixture(autouse=True)
def _club_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the window these tests assume: 7 days ahead, opening at 06:30 CT.

    The code reads them from settings, which a local .env can override.
    """
    monkeypatch.setattr(settings, "days_in_advance", 7)
    monkeypatch.setattr(settings, "booking_open_hour", 6)
    monkeypatch.setattr(settings, "booking_open_minute", 30)


@pytest.fixture
def booking_service() -> BookingService:
    return BookingService()


@pytest_asyncio.fixture
async def database_service(monkeypatch: pytest.MonkeyPatch) -> DatabaseService:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.database_service.AsyncSessionLocal", session_local)
    return DatabaseService()


def _ct(day: date, hour: int, minute: int) -> datetime:
    """An aware CT instant, built the way CTDateTime.now() values are."""
    return datetime.combine(day, time(hour, minute), tzinfo=CTDateTime.CT_TZ)


class TestExecutionTime:
    @pytest.mark.parametrize("target", TARGETS)
    def test_runs_at_0630_ct_on_the_open_date(
        self, booking_service: BookingService, target: date
    ) -> None:
        assert booking_service._calculate_execution_time(target) == datetime.combine(
            target - timedelta(days=7), time(6, 30)
        )

    @pytest.mark.parametrize("day", [FALL_BACK, SPRING_FORWARD])
    def test_the_stored_time_is_the_right_instant(
        self, booking_service: BookingService, day: date
    ) -> None:
        stored = booking_service._calculate_execution_time(day + timedelta(days=7))
        assert CTDateTime.from_naive_ct(stored).astimezone(UTC) == WINDOW_UTC[day]


class TestWindowInstant:
    @pytest.mark.parametrize("module", [racer_run, observer_run], ids=["racer", "observer"])
    @pytest.mark.parametrize("day", [FALL_BACK, SPRING_FORWARD])
    def test_is_0630_ct_two_minutes_after_the_0628_trigger(self, module, day: date) -> None:  # type: ignore[no-untyped-def]
        now = _ct(day, 6, 28)
        window = module._window_instant(now)
        assert window.astimezone(UTC) == WINDOW_UTC[day]
        assert window - now == timedelta(minutes=2)


class TestDueOnTransitionDay:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("day", [FALL_BACK, SPRING_FORWARD])
    async def test_a_booking_opening_that_morning_is_due_and_not_before(
        self,
        booking_service: BookingService,
        database_service: DatabaseService,
        day: date,
    ) -> None:
        target = day + timedelta(days=7)
        await database_service.create_booking(
            TeeTimeBooking(
                id=f"dst{day:%m%d}",
                phone_number="+15550000000",
                request=TeeTimeRequest(
                    requested_date=target, requested_time=time(8, 0), num_players=1
                ),
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=booking_service._calculate_execution_time(target),
            )
        )

        window = racer_run._window_instant(_ct(day, 6, 28))
        cutoff = CTDateTime.to_naive_ct(window)

        # A minute early finds nothing, so the stored time is not an hour off
        # in either direction. This is also the observer's query.
        minute_early = CTDateTime.to_naive_ct(window - timedelta(minutes=1))
        assert await database_service.get_due_bookings(minute_early) == []
        due = await database_service.get_due_bookings(cutoff)
        assert [b.id for b in due] == [f"dst{day:%m%d}"]

        # The racer's own query, which claims the group.
        assert await database_service.claim_next_due_group(minute_early) == []
        claimed = await database_service.claim_next_due_group(cutoff)
        assert [b.id for b in claimed] == [f"dst{day:%m%d}"]


class TestWallTimesThatAreNotOneInstant:
    """Times the transitions make ambiguous or skip. No booking uses them (the
    race is at 06:30), so these record today's behaviour rather than choose it."""

    def test_ambiguous_0130_on_fall_back_resolves_to_the_first_cdt_instant(self) -> None:
        aware = CTDateTime.from_naive_ct(datetime(2026, 11, 1, 1, 30))
        assert aware.astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        assert CTDateTime.to_naive_ct(aware) == datetime(2026, 11, 1, 1, 30)

    def test_nonexistent_0230_on_spring_forward_is_0830_utc_and_round_trips_as_typed(
        self,
    ) -> None:
        # zoneinfo reads the skipped hour with the CST offset, so it is the
        # instant 03:30 CDT names; to_naive_ct converts within the same zone,
        # which Python leaves untouched, so the naive value comes back as typed.
        aware = CTDateTime.from_naive_ct(datetime(2027, 3, 14, 2, 30))
        assert aware.astimezone(UTC) == datetime(2027, 3, 14, 8, 30, tzinfo=UTC)
        assert CTDateTime.to_naive_ct(aware) == datetime(2027, 3, 14, 2, 30)
