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
    @abstractmethod
    async def login(self) -> bool:
        pass

    @abstractmethod
    async def book_tee_time(
        self,
        date: date,
        time: time,
        num_players: int,
        fallback_window_minutes: int = 30,
    ) -> BookingResult:
        pass

    @abstractmethod
    async def get_available_times(self, date: date) -> list[time]:
        pass

    @abstractmethod
    async def cancel_booking(self, confirmation_number: str) -> bool:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass
