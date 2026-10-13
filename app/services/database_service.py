"""
Database service for persistent storage of bookings and sessions.

This module provides async CRUD operations for BookingRecord and SessionRecord,
handling conversion between Pydantic schemas and SQLAlchemy models.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy import select, update

from app.models.database import (
    AsyncSessionLocal,
    BookingRecord,
    SessionRecord,
    TeeSheetGridRecord,
    engine,
)
from app.models.schemas import (
    BookingStatus,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)
from app.providers.walden_sheet_grid import (
    GridSlot,
    SheetGrid,
    slots_from_json,
    slots_to_json,
)

logger = logging.getLogger(__name__)

# How many times claim_next_due_group re-reads after losing a group to another
# task. Each loss removes that group from the due set, so a handful of racer
# tasks never needs more than a few; this bound only turns a bug into an error
# instead of a loop.
_MAX_CLAIM_ATTEMPTS = 20


def _ladder_to_json(ladder: list[time] | None) -> str | None:
    """A fallback ladder as stored: JSON "HH:MM" strings, in order.

    An empty list is kept as "[]", not NULL: an agreed slot with nothing open
    near it has a ladder, and it is empty. NULL means no ladder was worked out.
    """
    if ladder is None:
        return None
    return json.dumps([t.strftime("%H:%M") for t in ladder])


def _ladder_from_json(text: str | None) -> list[time] | None:
    """The stored ladder, or None when there is none - or none that can be read.

    Tolerant on purpose. Every read of a booking passes through here, including
    the racer's claim at 06:28, and the ladder only describes the race: a value
    that fails to parse must cost the description, never the booking.
    """
    if text is None:
        return None
    try:
        return [datetime.strptime(value, "%H:%M").time() for value in json.loads(text)]
    except (TypeError, ValueError) as e:
        logger.warning("Ignoring an unreadable fallback_ladder %r: %s", text[:80], e)
        return None


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
            origin_channel_id=booking.origin_channel_id,
            channel=booking.channel,
            requester_handle=booking.requester_handle,
            slot_confirmed=booking.request.slot_confirmed,
            asked_time=booking.request.asked_time,
            fallback_ladder=_ladder_to_json(booking.request.fallback_ladder),
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
            slot_confirmed=record.slot_confirmed,  # type: ignore[arg-type]
            asked_time=record.asked_time,  # type: ignore[arg-type]
            fallback_ladder=_ladder_from_json(record.fallback_ladder),  # type: ignore[arg-type]
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
            origin_channel_id=record.origin_channel_id,  # type: ignore[arg-type]
            channel=record.channel,  # type: ignore[arg-type]
            requester_handle=record.requester_handle,  # type: ignore[arg-type]
            created_at=record.created_at,  # type: ignore[arg-type]
            updated_at=record.updated_at,  # type: ignore[arg-type]
        )

    def _session_to_record(self, session: UserSession) -> SessionRecord:
        """Convert a UserSession Pydantic model to a SessionRecord SQLAlchemy model."""
        import json

        pending_json = None
        if session.pending_requests:
            pending_json = json.dumps([r.model_dump(mode="json") for r in session.pending_requests])
        elif session.pending_request:
            pending_json = session.pending_request.model_dump_json()
        return SessionRecord(
            phone_number=session.phone_number,
            state=session.state,
            pending_request_json=pending_json,
            pending_cancellation_id=session.pending_cancellation_id,
            pending_replace_booking_id=session.pending_replace_booking_id,
            replace_clarifications=session.replace_clarifications,
            pending_proxy_target=session.pending_proxy_target,
            origin_channel_id=session.origin_channel_id,
            channel=session.channel,
            requester_handle=session.requester_handle,
            last_interaction=session.last_interaction,
        )

    def _record_to_session(self, record: SessionRecord) -> UserSession:
        """Convert a SessionRecord SQLAlchemy model to a UserSession Pydantic model."""
        import json

        pending_request = None
        pending_requests = None
        if record.pending_request_json:
            try:
                data = json.loads(record.pending_request_json)  # type: ignore[arg-type]
                if isinstance(data, list):
                    pending_requests = [TeeTimeRequest.model_validate(r) for r in data]
                else:
                    pending_request = TeeTimeRequest.model_validate(data)
            except (json.JSONDecodeError, TypeError):
                pending_request = TeeTimeRequest.model_validate_json(
                    record.pending_request_json  # type: ignore[arg-type]
                )
        return UserSession(
            phone_number=record.phone_number,  # type: ignore[arg-type]
            state=record.state,  # type: ignore[arg-type]
            pending_request=pending_request,
            pending_requests=pending_requests,
            pending_cancellation_id=record.pending_cancellation_id,  # type: ignore[arg-type]
            pending_replace_booking_id=record.pending_replace_booking_id,  # type: ignore[arg-type]
            replace_clarifications=record.replace_clarifications or 0,  # type: ignore[arg-type]
            pending_proxy_target=record.pending_proxy_target,  # type: ignore[arg-type]
            origin_channel_id=record.origin_channel_id,  # type: ignore[arg-type]
            channel=record.channel,  # type: ignore[arg-type]
            requester_handle=record.requester_handle,  # type: ignore[arg-type]
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

    async def get_live_bookings_on(
        self, phone_number: str, dates: list[date]
    ) -> list[TeeTimeBooking]:
        """A member's bookings on ``dates`` that hold, or are about to hold, their round.

        Pending, scheduled, in progress or won; filtered in the query, so the
        same-day check (issue #284) does not read a member's whole history.
        """
        if not dates:
            return []
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BookingRecord).where(
                    BookingRecord.phone_number == phone_number,
                    BookingRecord.requested_date.in_(dates),
                    BookingRecord.status.in_(
                        [
                            BookingStatus.PENDING,
                            BookingStatus.SCHEDULED,
                            BookingStatus.IN_PROGRESS,
                            BookingStatus.SUCCESS,
                        ]
                    ),
                )
            )
            return [self._record_to_booking(r) for r in result.scalars().all()]

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

    async def cancel_pending_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """Mark a booking CANCELLED only if it has not started, in one statement.

        A read-then-write cancel races the racer's claim: if
        claim_next_due_group moves the row to IN_PROGRESS between the read and
        the write, the write overwrites a race already under way with
        CANCELLED. This UPDATE re-checks the status in the database, the same
        way the claim does, so whichever commits first wins and the other
        changes nothing.

        Returns the cancelled booking, or None when it no longer exists or has
        already moved past PENDING/SCHEDULED.
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(BookingRecord)
                .where(
                    BookingRecord.booking_id == booking_id,
                    BookingRecord.status.in_([BookingStatus.PENDING, BookingStatus.SCHEDULED]),
                )
                .values(
                    status=BookingStatus.CANCELLED,
                    updated_at=datetime.now(UTC).replace(tzinfo=None),
                )
                .returning(BookingRecord.booking_id)
                .execution_options(synchronize_session=False)
            )
            cancelled = result.scalar_one_or_none()
            await db.commit()

        if cancelled is None:
            return None
        return await self.get_booking(booking_id)

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
        import json

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionRecord).where(SessionRecord.phone_number == session.phone_number)
            )
            record = result.scalar_one_or_none()
            if not record:
                raise ValueError(f"Session for {session.phone_number} not found")

            record.state = session.state  # type: ignore[assignment]
            pending_json = None
            if session.pending_requests:
                pending_json = json.dumps(
                    [r.model_dump(mode="json") for r in session.pending_requests]
                )
            elif session.pending_request:
                pending_json = session.pending_request.model_dump_json()
            record.pending_request_json = pending_json  # type: ignore[assignment]
            record.pending_cancellation_id = session.pending_cancellation_id  # type: ignore[assignment]
            record.pending_replace_booking_id = session.pending_replace_booking_id  # type: ignore[assignment]
            record.replace_clarifications = session.replace_clarifications  # type: ignore[assignment]
            record.pending_proxy_target = session.pending_proxy_target  # type: ignore[assignment]
            record.origin_channel_id = session.origin_channel_id  # type: ignore[assignment]
            record.channel = session.channel  # type: ignore[assignment]
            record.requester_handle = session.requester_handle  # type: ignore[assignment]
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
            query = (
                select(BookingRecord)
                .where(
                    BookingRecord.status == BookingStatus.SCHEDULED,
                    BookingRecord.scheduled_execution_time <= due_before,
                )
                # A stated order, not whatever the query planner returns. On
                # 2026-09-13 two requesters' groups ran in the reverse of the
                # order insertion predicted, and nobody had decided either one.
                .order_by(
                    BookingRecord.requested_date,
                    BookingRecord.phone_number,
                    BookingRecord.requested_time,
                    BookingRecord.booking_id,
                )
            )
            result = await db.execute(query)
            records = result.scalars().all()
            return [self._record_to_booking(r) for r in records]

    async def claim_next_due_group(self, due_before: datetime) -> list[TeeTimeBooking]:
        """Take one requester's due bookings for a date, so no other racer can.

        Each racer task (``app/racer/``, issue #184) calls this once and races
        whatever it gets. A group is every due booking sharing a requested date
        and a requester - the same grouping ``execute_bookings_batch`` uses,
        so a group is exactly one login's work.

        The claim is a conditional UPDATE that only moves rows still SCHEDULED
        to IN_PROGRESS. Two tasks going for the same group cannot both win it:
        Postgres re-checks the status once the first writer commits, so the
        loser updates nothing and goes back for the next group. Nothing here
        needs an advisory lock, and SQLite behaves the same way for tests.

        The UPDATE selects by the group's (date, requester) key, not by the ids
        the read returned, so a booking committed for the same requester and
        date between the read and the update joins this claim instead of being
        left SCHEDULED for another task to race concurrently. One created after
        the claim commits still forms a group of its own later; a second
        booking for a member already racing that date is refused by the club's
        one-round-per-day rule regardless, and refusing it at creation is #134.

        Returns the claimed bookings, marked IN_PROGRESS, or an empty list when
        no unclaimed group is left. Raises rather than returning empty if
        claims keep coming back empty while rows are still due, because an
        empty answer would leave those bookings unraced and unreported.
        """
        for _ in range(_MAX_CLAIM_ATTEMPTS):
            due = await self.get_due_bookings(due_before)
            if not due:
                return []

            first = due[0]

            claimed_at = datetime.now(UTC).replace(tzinfo=None)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    update(BookingRecord)
                    .where(
                        BookingRecord.requested_date == first.request.requested_date,
                        BookingRecord.phone_number == first.phone_number,
                        BookingRecord.status == BookingStatus.SCHEDULED,
                        BookingRecord.scheduled_execution_time <= due_before,
                    )
                    .values(status=BookingStatus.IN_PROGRESS, updated_at=claimed_at)
                    .returning(BookingRecord.booking_id)
                    .execution_options(synchronize_session=False)
                )
                won = [row[0] for row in result.all()]
                await db.commit()

            if won:
                # Re-read rather than reuse the earlier read: the claim may
                # include a row that read never saw.
                async with AsyncSessionLocal() as db:
                    rows = await db.execute(
                        select(BookingRecord)
                        .where(BookingRecord.booking_id.in_(won))
                        .order_by(BookingRecord.requested_time, BookingRecord.booking_id)
                    )
                    return [self._record_to_booking(r) for r in rows.scalars().all()]
            # Another task claimed (or someone cancelled) this group between
            # the read and the update. Those rows are no longer SCHEDULED, so
            # the next read cannot return them again.

        raise RuntimeError(
            f"Could not claim a due booking group after {_MAX_CLAIM_ATTEMPTS} attempts "
            "while bookings were still due"
        )

    async def ensure_tee_sheet_grid_table(self) -> None:
        """Create the grid table if this database does not have it yet.

        The service creates every table at startup (init_db), but the observer
        is a separate job that can run before the service has ever started on a
        new revision - with scale-to-zero, nothing guarantees the order. Only
        this one table is touched: init_db's column migrations take locks on
        the bookings table, and the observer writes while the racer may still be
        reporting on it.
        """
        async with engine.begin() as conn:
            await conn.run_sync(TeeSheetGridRecord.__table__.create, checkfirst=True)  # type: ignore[attr-defined]

    async def save_tee_sheet_grid(
        self,
        sheet_date: date,
        slots: Sequence[GridSlot],
        captured_at: datetime,
        source: str,
    ) -> None:
        """Append one reading of a date's grid (issue #216)."""
        async with AsyncSessionLocal() as db:
            db.add(
                TeeSheetGridRecord(
                    sheet_date=sheet_date,
                    captured_at=captured_at,
                    slots_json=slots_to_json(slots),
                    slot_count=len(slots),
                    source=source,
                )
            )
            await db.commit()

    async def get_tee_sheet_grid(self, sheet_date: date) -> SheetGrid | None:
        """The latest reading of a date's grid, or None if it was never read."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TeeSheetGridRecord)
                .where(TeeSheetGridRecord.sheet_date == sheet_date)
                .order_by(TeeSheetGridRecord.captured_at.desc(), TeeSheetGridRecord.id.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            return SheetGrid(
                sheet_date=record.sheet_date,  # type: ignore[arg-type]
                slots=slots_from_json(record.slots_json),  # type: ignore[arg-type]
                captured_at=record.captured_at,  # type: ignore[arg-type]
            )


database_service = DatabaseService()
