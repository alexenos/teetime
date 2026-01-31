from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time


@dataclass
class BatchBookingRequest:
    """A single booking request within a batch."""

    booking_id: str
    target_time: time
    num_players: int
    fallback_window_minutes: int = 32
    tee_time_interval_minutes: int = 8


@dataclass
class BookingResult:
    """Result of a single booking attempt."""

    success: bool
    course_name: str | None = None
    booked_time: time | None = None
    confirmation_number: str | None = None
    error_message: str | None = None
    alternatives: str | None = None
    fallback_reason: str | None = None


@dataclass
class BatchBookingItemResult:
    """Result of a single booking within a batch."""

    booking_id: str
    result: BookingResult


@dataclass
class BatchBookingResult:
    """Result of a batch booking operation."""

    results: list[BatchBookingItemResult] = field(default_factory=list)
    total_succeeded: int = 0
    total_failed: int = 0


class ReservationProvider(ABC):
    """Abstract base class for golf course reservation providers."""

    @abstractmethod
    async def login(self) -> bool:
        """Authenticate with the booking system."""
        pass

    @abstractmethod
    async def book_tee_time(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int = 32,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        """Book a tee time for the specified date and time."""
        pass

    @abstractmethod
    async def get_available_times(self, target_date: date) -> list[time]:
        """Get available tee times for a given date."""
        pass

    @abstractmethod
    async def book_multiple_tee_times(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None = None,
    ) -> BatchBookingResult:
        """
        Book multiple tee times in a single session for efficiency.

        This method is optimized for booking multiple tee times on the same date:
        1. Creates a single WebDriver session
        2. Logs in once
        3. If execute_at is provided, waits until that time before booking
        4. Books all requested times in sequence
        5. Returns results for all bookings

        Args:
            target_date: The date to book (all requests must be for this date)
            requests: List of booking requests to execute
            execute_at: Optional datetime to wait until before starting bookings.
                       If provided, the method will log in early and wait until
                       this time before refreshing the page and booking.

        Returns:
            BatchBookingResult with results for each booking request
        """
        pass

    @abstractmethod
    async def cancel_booking(self, confirmation_number: str) -> bool:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass
