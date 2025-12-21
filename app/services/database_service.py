"""
Database service for persistent storage of bookings and sessions.

This module provides async CRUD operations for BookingRecord and SessionRecord,
handling conversion between Pydantic schemas and SQLAlchemy models.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, BookingRecord, SessionRecord
from app.models.schemas import (
    BookingStatus,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)


class DatabaseService:
    """
    Provides database operations for bookings and sessions.

    This service handles the conversion between Pydantic models used in the
    application layer and SQLAlchemy models used for persistence.
    """

    def _booking_to_record(self, booking: TeeTimeBooking) -> BookingRecord:
        """Convert a TeeTimeBooking Pydantic model to a BookingRecord SQLAlchemy model."""
        return BookingRecord(
            booking_id=booking.id,
            phone_number=booking.phone_number,
            requested_date=booking.request.requested_date,
            requested_time=booking.request.requested_time,
            num_players=booking.request.num_players,
            fallback_window_minutes=booking.request.fallback_window_minutes,
            status=booking.status,
            scheduled_execution_time=booking.scheduled_execution_time,
            actual_booked_time=booking.actual_booked_time,
            confirmation_number=booking.confirmation_number,
            error_message=booking.error_message,
            created_at=booking.created_at,
            updated_at=booking.updated_at,
        )

    def _record_to_booking(self, record: BookingRecord) -> TeeTimeBooking:
        """Convert a BookingRecord SQLAlchemy model to a TeeTimeBooking Pydantic model."""
        request = TeeTimeRequest(
            requested_date=record.requested_date,  # type: ignore[arg-type]
            requested_time=record.requested_time,  # type: ignore[arg-type]
            num_players=record.num_players,  # type: ignore[arg-type]
            fallback_window_minutes=record.fallback_window_minutes,  # type: ignore[arg-type]
        )
        return TeeTimeBooking(
            id=record.booking_id,  # type: ignore[arg-type]
            phone_number=record.phone_number,  # type: ignore[arg-type]
            request=request,
            status=record.status,  # type: ignore[arg-type]
            scheduled_execution_time=record.scheduled_execution_time,  # type: ignore[arg-type]
            actual_booked_time=record.actual_booked_time,  # type: ignore[arg-type]
            confirmation_number=record.confirmation_number,  # type: ignore[arg-type]
            error_message=record.error_message,  # type: ignore[arg-type]
            created_at=record.created_at,  # type: ignore[arg-type]
            updated_at=record.updated_at,  # type: ignore[arg-type]
        )

    def _session_to_record(self, session: UserSession) -> SessionRecord:
        """Convert a UserSession Pydantic model to a SessionRecord SQLAlchemy model."""
        pending_json = None
        if session.pending_request:
            pending_json = session.pending_request.model_dump_json()
        return SessionRecord(
            phone_number=session.phone_number,
            state=session.state,
            pending_request_json=pending_json,
            last_interaction=session.last_interaction,
        )

    def _record_to_session(self, record: SessionRecord) -> UserSession:
        """Convert a SessionRecord SQLAlchemy model to a UserSession Pydantic model."""
        pending_request = None
        if record.pending_request_json:
            pending_request = TeeTimeRequest.model_validate_json(
                record.pending_request_json  # type: ignore[arg-type]
            )
        return UserSession(
            phone_number=record.phone_number,  # type: ignore[arg-type]
            state=record.state,  # type: ignore[arg-type]
            pending_request=pending_request,
            last_interaction=record.last_interaction,  # type: ignore[arg-type]
        )

    async def create_booking(self, booking: TeeTimeBooking) -> TeeTimeBooking:
        """Create a new booking record in the database."""
        async with AsyncSessionLocal() as db:
            record = self._booking_to_record(booking)
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return self._record_to_booking(record)

    async def get_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """Get a booking by its ID."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BookingRecord).where(BookingRecord.booking_id == booking_id)
            )
            record = result.scalar_one_or_none()
            if record:
                return self._record_to_booking(record)
            return None

    async def get_bookings(
        self,
        phone_number: str | None = None,
        status: BookingStatus | None = None,
    ) -> list[TeeTimeBooking]:
        """Get all bookings, optionally filtered by phone number and/or status."""
        async with AsyncSessionLocal() as db:
            query = select(BookingRecord)
            if phone_number:
                query = query.where(BookingRecord.phone_number == phone_number)
            if status:
                query = query.where(BookingRecord.status == status)
            result = await db.execute(query)
            records = result.scalars().all()
            return [self._record_to_booking(r) for r in records]

    async def update_booking(self, booking: TeeTimeBooking) -> TeeTimeBooking:
        """Update an existing booking record."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BookingRecord).where(BookingRecord.booking_id == booking.id)
            )
            record = result.scalar_one_or_none()
            if not record:
                raise ValueError(f"Booking {booking.id} not found")

            record.status = booking.status  # type: ignore[assignment]
            record.actual_booked_time = booking.actual_booked_time  # type: ignore[assignment]
            record.confirmation_number = booking.confirmation_number  # type: ignore[assignment]
            record.error_message = booking.error_message  # type: ignore[assignment]
            record.updated_at = datetime.now(UTC).replace(tzinfo=None)  # type: ignore[assignment]

            await db.commit()
            await db.refresh(record)
            return self._record_to_booking(record)

    async def get_session(self, phone_number: str) -> UserSession | None:
        """Get a session by phone number."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionRecord).where(SessionRecord.phone_number == phone_number)
            )
            record = result.scalar_one_or_none()
            if record:
                return self._record_to_session(record)
            return None

    async def create_session(self, session: UserSession) -> UserSession:
        """Create a new session record in the database."""
        async with AsyncSessionLocal() as db:
            record = self._session_to_record(session)
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return self._record_to_session(record)

    async def update_session(self, session: UserSession) -> UserSession:
        """Update an existing session record."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionRecord).where(SessionRecord.phone_number == session.phone_number)
            )
            record = result.scalar_one_or_none()
            if not record:
                raise ValueError(f"Session for {session.phone_number} not found")

            record.state = session.state  # type: ignore[assignment]
            pending_json = None
            if session.pending_request:
                pending_json = session.pending_request.model_dump_json()
            record.pending_request_json = pending_json  # type: ignore[assignment]
            record.last_interaction = session.last_interaction  # type: ignore[assignment]

            await db.commit()
            await db.refresh(record)
            return self._record_to_session(record)

    async def get_or_create_session(self, phone_number: str) -> UserSession:
        """Get an existing session or create a new one."""
        session = await self.get_session(phone_number)
        if session:
            return session
        new_session = UserSession(phone_number=phone_number)
        return await self.create_session(new_session)

    async def get_due_bookings(self, due_before: datetime) -> list[TeeTimeBooking]:
        """
        Get all scheduled bookings that are due for execution.

        This performs the filtering at the database layer for efficiency.

        Args:
            due_before: Datetime to compare against (naive, in CT wall-clock time).
                        Bookings with scheduled_execution_time <= due_before are returned.

        Returns:
            List of bookings that are due for execution.
        """
        async with AsyncSessionLocal() as db:
            query = select(BookingRecord).where(
                BookingRecord.status == BookingStatus.SCHEDULED,
                BookingRecord.scheduled_execution_time <= due_before,
            )
            result = await db.execute(query)
            records = result.scalars().all()
            return [self._record_to_booking(r) for r in records]


database_service = DatabaseService()
