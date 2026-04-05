"""
Centralized timezone utility for Central Time (America/Chicago) operations.

All timezone conversions in the application should go through CTDateTime to
ensure consistency and avoid the class of bugs caused by scattered ZoneInfo
lookups and naive/aware datetime mismatches.

Design notes:
- Database stores naive datetimes representing CT wall-clock time.
- In-memory comparisons use timezone-aware CT datetimes.
- Naive datetimes passed into CTDateTime are always assumed to be CT.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

_CT_TZ = ZoneInfo("America/Chicago")

# Booking window: Cloud Scheduler triggers at 6:28am so the system can log in
# before the reservation page opens at 6:30am. The window is considered "open"
# from 6:28am until 6:35am to give a small buffer for retries.
_BOOKING_WINDOW_START_HOUR = 6
_BOOKING_WINDOW_START_MINUTE = 28
_BOOKING_WINDOW_END_HOUR = 6
_BOOKING_WINDOW_END_MINUTE = 35


class CTDateTime:
    """
    Utility class for Central Time datetime operations.

    All methods are class methods so this class can be used without
    instantiation. The CT timezone is fixed to 'America/Chicago' and
    handles DST transitions automatically via the zoneinfo stdlib module.
    """

    CT_TZ: ZoneInfo = _CT_TZ

    @classmethod
    def now(cls) -> datetime:
        """
        Return the current moment as a timezone-aware datetime in Central Time.

        Returns:
            A timezone-aware datetime in the America/Chicago timezone.
        """
        return datetime.now(cls.CT_TZ)

    @classmethod
    def to_naive_ct(cls, dt: datetime) -> datetime:
        """
        Convert any datetime to a naive CT datetime for database storage.

        The database schema stores scheduled_execution_time as a timestamp
        without timezone (naive). This method canonicalises any datetime
        into that form by first converting aware datetimes to CT and then
        stripping the tzinfo.

        Args:
            dt: A timezone-aware or naive datetime. If naive, it is assumed
                to already represent CT wall-clock time and is returned
                unchanged (only tzinfo is stripped if somehow present).

        Returns:
            A naive datetime whose value represents CT wall-clock time.
        """
        if dt.tzinfo is not None:
            dt = dt.astimezone(cls.CT_TZ)
        return dt.replace(tzinfo=None)

    @classmethod
    def from_naive_ct(cls, dt: datetime) -> datetime:
        """
        Convert a naive CT datetime to a timezone-aware CT datetime.

        Use this when retrieving naive datetimes from the database that are
        known to represent CT wall-clock time and need to participate in
        timezone-aware arithmetic or comparisons.

        Args:
            dt: A naive datetime whose value represents CT wall-clock time.

        Returns:
            A timezone-aware datetime in the America/Chicago timezone.

        Raises:
            ValueError: If ``dt`` is already timezone-aware.
        """
        if dt.tzinfo is not None:
            raise ValueError(
                f"from_naive_ct expects a naive datetime, got tzinfo={dt.tzinfo!r}"
            )
        return dt.replace(tzinfo=cls.CT_TZ)

    @classmethod
    def normalize_to_ct(cls, dt: datetime) -> datetime:
        """
        Return a timezone-aware CT datetime regardless of the input's tzinfo.

        This is the safe "always works" conversion: if ``dt`` is already
        timezone-aware it is converted to CT; if it is naive it is treated
        as CT and has tzinfo attached.

        Args:
            dt: A timezone-aware or naive datetime.

        Returns:
            A timezone-aware datetime in the America/Chicago timezone.
        """
        if dt.tzinfo is not None:
            return dt.astimezone(cls.CT_TZ)
        return dt.replace(tzinfo=cls.CT_TZ)

    @classmethod
    def is_booking_window(cls, dt: datetime) -> bool:
        """
        Return True if ``dt`` falls within the 6:28–6:35 AM CT booking window.

        The booking window begins at 6:28 AM (when Cloud Scheduler triggers)
        and ends at 6:35 AM (a small buffer past the 6:30 AM reservation open
        time). Both endpoints are inclusive.

        Args:
            dt: A timezone-aware or naive datetime to check. Naive datetimes
                are treated as CT.

        Returns:
            True if the time component of ``dt`` (in CT) is in [6:28, 6:35].
        """
        ct_dt = cls.normalize_to_ct(dt)
        window_start = ct_dt.replace(
            hour=_BOOKING_WINDOW_START_HOUR,
            minute=_BOOKING_WINDOW_START_MINUTE,
            second=0,
            microsecond=0,
        )
        window_end = ct_dt.replace(
            hour=_BOOKING_WINDOW_END_HOUR,
            minute=_BOOKING_WINDOW_END_MINUTE,
            second=0,
            microsecond=0,
        )
        return window_start <= ct_dt <= window_end
