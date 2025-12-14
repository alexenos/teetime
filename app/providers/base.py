from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, time


@dataclass
class BookingResult:
    success: bool
    booked_time: time | None = None
    confirmation_number: str | None = None
    error_message: str | None = None
    alternatives: str | None = None


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
        fallback_window_minutes: int = 30,
    ) -> BookingResult:
        """Book a tee time for the specified date and time."""
        pass

    @abstractmethod
    async def get_available_times(self, target_date: date) -> list[time]:
        """Get available tee times for a given date."""
        pass

    @abstractmethod
    async def cancel_booking(self, confirmation_number: str) -> bool:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass
