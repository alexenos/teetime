"""
Comprehensive unit tests for app/utils/timezone.py (CTDateTime).

Tests cover:
- now() returns timezone-aware CT datetime
- to_naive_ct() strips tzinfo and converts to CT
- from_naive_ct() attaches CT tzinfo to naive datetimes
- normalize_to_ct() handles both aware and naive inputs
- is_booking_window() correctly identifies the 6:28–6:35 AM window
- DST boundary correctness
- Round-trip consistency
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.utils.timezone import CTDateTime

CT_TZ = ZoneInfo("America/Chicago")
UTC_TZ = UTC


# ---------------------------------------------------------------------------
# now()
# ---------------------------------------------------------------------------


class TestNow:
    def test_returns_aware_datetime(self) -> None:
        dt = CTDateTime.now()
        assert dt.tzinfo is not None

    def test_returns_ct_timezone(self) -> None:
        dt = CTDateTime.now()
        # Normalize: zoneinfo returns the canonical zone key
        assert dt.tzinfo == CT_TZ or str(dt.tzinfo) == "America/Chicago"

    def test_close_to_real_time(self) -> None:
        """now() should be within 2 seconds of the actual current time."""
        import time as _time

        before = datetime.now(CT_TZ)
        _time.sleep(0)
        ct_now = CTDateTime.now()
        after = datetime.now(CT_TZ)
        assert before <= ct_now <= after + timedelta(seconds=2)


# ---------------------------------------------------------------------------
# to_naive_ct()
# ---------------------------------------------------------------------------


class TestToNaiveCt:
    def test_aware_ct_becomes_naive(self) -> None:
        aware = datetime(2026, 6, 15, 10, 30, 0, tzinfo=CT_TZ)
        naive = CTDateTime.to_naive_ct(aware)
        assert naive.tzinfo is None
        assert naive == datetime(2026, 6, 15, 10, 30, 0)

    def test_aware_utc_converts_to_ct(self) -> None:
        # 2026-06-15 15:00 UTC == 2026-06-15 10:00 CDT (UTC-5)
        aware_utc = datetime(2026, 6, 15, 15, 0, 0, tzinfo=UTC_TZ)
        naive = CTDateTime.to_naive_ct(aware_utc)
        assert naive.tzinfo is None
        assert naive == datetime(2026, 6, 15, 10, 0, 0)

    def test_aware_utc_winter_converts_to_cst(self) -> None:
        # 2026-01-15 12:00 UTC == 2026-01-15 06:00 CST (UTC-6)
        aware_utc = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC_TZ)
        naive = CTDateTime.to_naive_ct(aware_utc)
        assert naive.tzinfo is None
        assert naive == datetime(2026, 1, 15, 6, 0, 0)

    def test_naive_input_passthrough(self) -> None:
        """Naive datetimes (assumed CT) are returned with tzinfo stripped (no-op)."""
        naive_input = datetime(2026, 3, 10, 8, 0, 0)
        result = CTDateTime.to_naive_ct(naive_input)
        assert result.tzinfo is None
        assert result == naive_input

    def test_already_naive_no_change(self) -> None:
        dt = datetime(2026, 7, 4, 6, 30, 0)
        assert CTDateTime.to_naive_ct(dt) == dt


# ---------------------------------------------------------------------------
# from_naive_ct()
# ---------------------------------------------------------------------------


class TestFromNaiveCt:
    def test_attaches_ct_tzinfo(self) -> None:
        naive = datetime(2026, 6, 15, 10, 30, 0)
        aware = CTDateTime.from_naive_ct(naive)
        assert aware.tzinfo is not None
        assert aware.tzinfo == CT_TZ or str(aware.tzinfo) == "America/Chicago"

    def test_value_unchanged(self) -> None:
        naive = datetime(2026, 6, 15, 10, 30, 0)
        aware = CTDateTime.from_naive_ct(naive)
        assert aware.replace(tzinfo=None) == naive

    def test_raises_if_already_aware(self) -> None:
        aware = datetime(2026, 6, 15, 10, 30, 0, tzinfo=CT_TZ)
        with pytest.raises(ValueError, match="from_naive_ct expects a naive datetime"):
            CTDateTime.from_naive_ct(aware)

    def test_raises_for_utc_aware(self) -> None:
        aware_utc = datetime(2026, 6, 15, 10, 30, 0, tzinfo=UTC_TZ)
        with pytest.raises(ValueError):
            CTDateTime.from_naive_ct(aware_utc)


# ---------------------------------------------------------------------------
# normalize_to_ct()
# ---------------------------------------------------------------------------


class TestNormalizeToCt:
    def test_naive_gets_ct_attached(self) -> None:
        naive = datetime(2026, 6, 15, 10, 30, 0)
        result = CTDateTime.normalize_to_ct(naive)
        assert result.tzinfo is not None
        assert result.replace(tzinfo=None) == naive

    def test_aware_ct_unchanged(self) -> None:
        aware = datetime(2026, 6, 15, 10, 30, 0, tzinfo=CT_TZ)
        result = CTDateTime.normalize_to_ct(aware)
        assert result == aware

    def test_aware_utc_converted_to_ct(self) -> None:
        # 2026-06-15 15:00 UTC == 2026-06-15 10:00 CDT
        aware_utc = datetime(2026, 6, 15, 15, 0, 0, tzinfo=UTC_TZ)
        result = CTDateTime.normalize_to_ct(aware_utc)
        assert result.tzinfo is not None
        assert result.replace(tzinfo=None) == datetime(2026, 6, 15, 10, 0, 0)

    def test_always_returns_aware(self) -> None:
        for dt in [
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC_TZ),
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=CT_TZ),
        ]:
            result = CTDateTime.normalize_to_ct(dt)
            assert result.tzinfo is not None


# ---------------------------------------------------------------------------
# is_booking_window()
# ---------------------------------------------------------------------------


class TestIsBookingWindow:
    # Helper: build a naive CT datetime at a given H:M:S
    @staticmethod
    def _ct(hour: int, minute: int, second: int = 0) -> datetime:
        return datetime(2026, 7, 4, hour, minute, second)

    def test_before_window(self) -> None:
        assert not CTDateTime.is_booking_window(self._ct(6, 27, 59))

    def test_window_start_inclusive(self) -> None:
        assert CTDateTime.is_booking_window(self._ct(6, 28, 0))

    def test_inside_window(self) -> None:
        assert CTDateTime.is_booking_window(self._ct(6, 30, 0))

    def test_window_end_inclusive(self) -> None:
        assert CTDateTime.is_booking_window(self._ct(6, 35, 0))

    def test_after_window(self) -> None:
        assert not CTDateTime.is_booking_window(self._ct(6, 35, 1))

    def test_much_earlier(self) -> None:
        assert not CTDateTime.is_booking_window(self._ct(0, 0, 0))

    def test_much_later(self) -> None:
        assert not CTDateTime.is_booking_window(self._ct(12, 0, 0))

    def test_aware_ct_datetime(self) -> None:
        aware = datetime(2026, 7, 4, 6, 30, 0, tzinfo=CT_TZ)
        assert CTDateTime.is_booking_window(aware)

    def test_aware_utc_in_window(self) -> None:
        # 6:30 CDT == 11:30 UTC (summer, UTC-5)
        aware_utc = datetime(2026, 7, 4, 11, 30, 0, tzinfo=UTC_TZ)
        assert CTDateTime.is_booking_window(aware_utc)

    def test_aware_utc_outside_window(self) -> None:
        # 7:00 CDT == 12:00 UTC (summer, UTC-5) – outside [6:28, 6:35]
        aware_utc = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC_TZ)
        assert not CTDateTime.is_booking_window(aware_utc)


# ---------------------------------------------------------------------------
# DST boundary tests
# ---------------------------------------------------------------------------


class TestDstBoundaries:
    def test_spring_forward_2026(self) -> None:
        """2026-03-08 02:00 clocks spring forward to 03:00 (CDT starts)."""
        # 2026-03-08 08:00 UTC == 2026-03-08 02:00 CST before transition
        # but actually at spring forward UTC-6->UTC-5
        # 2026-03-08 12:00 UTC == 2026-03-08 07:00 CDT (after transition)
        aware_utc = datetime(2026, 3, 8, 12, 0, 0, tzinfo=UTC_TZ)
        naive = CTDateTime.to_naive_ct(aware_utc)
        # CDT = UTC-5 so 12:00 UTC = 07:00 CDT
        assert naive == datetime(2026, 3, 8, 7, 0, 0)

    def test_fall_back_2026(self) -> None:
        """2026-11-01 02:00 clocks fall back to 01:00 (CST starts)."""
        # 2026-11-01 07:00 UTC == 2026-11-01 01:00 CST (UTC-6, after fallback)
        aware_utc = datetime(2026, 11, 1, 7, 0, 0, tzinfo=UTC_TZ)
        naive = CTDateTime.to_naive_ct(aware_utc)
        # CST = UTC-6 so 07:00 UTC = 01:00 CST
        assert naive == datetime(2026, 11, 1, 1, 0, 0)

    def test_round_trip_naive_summer(self) -> None:
        naive = datetime(2026, 7, 4, 6, 30, 0)
        aware = CTDateTime.from_naive_ct(naive)
        back = CTDateTime.to_naive_ct(aware)
        assert back == naive

    def test_round_trip_naive_winter(self) -> None:
        naive = datetime(2026, 1, 15, 6, 30, 0)
        aware = CTDateTime.from_naive_ct(naive)
        back = CTDateTime.to_naive_ct(aware)
        assert back == naive


# ---------------------------------------------------------------------------
# CT_TZ class attribute
# ---------------------------------------------------------------------------


class TestCtTzAttribute:
    def test_is_america_chicago(self) -> None:
        assert str(CTDateTime.CT_TZ) == "America/Chicago"

    def test_is_zoneinfo_instance(self) -> None:
        assert isinstance(CTDateTime.CT_TZ, ZoneInfo)
