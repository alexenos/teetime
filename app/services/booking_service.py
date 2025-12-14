"""
Booking service for managing tee time reservations.

This module provides the core business logic for handling SMS conversations,
processing booking requests, and executing reservations at the scheduled time.
"""

import uuid
from datetime import datetime, timedelta

import pytz

from app.config import settings
from app.models.schemas import (
    BookingStatus,
    ConversationState,
    ParsedIntent,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)
from app.providers.base import ReservationProvider
from app.services.gemini_service import gemini_service
from app.services.sms_service import sms_service


class BookingService:
    """
    Manages tee time booking requests and SMS conversations.

    This service handles the full lifecycle of booking requests:
    1. Receiving and parsing SMS messages via Gemini LLM
    2. Managing conversation state to collect booking details
    3. Scheduling booking jobs for execution at the reservation open time
    4. Executing bookings via the reservation provider
    5. Sending confirmation/failure notifications via SMS

    Note: Currently uses in-memory storage for sessions and bookings.
    Data will be lost on restart. Future versions will use the database.

    Attributes:
        _sessions: In-memory store of user conversation sessions.
        _bookings: In-memory store of booking records.
        _reservation_provider: Provider for executing bookings on the club website.
    """

    def __init__(self) -> None:
        """Initialize the booking service with empty in-memory stores."""
        self._sessions: dict[str, UserSession] = {}
        self._bookings: dict[str, TeeTimeBooking] = {}
        self._reservation_provider: ReservationProvider | None = None

    def set_reservation_provider(self, provider: ReservationProvider) -> None:
        """Set the reservation provider for executing bookings."""
        self._reservation_provider = provider

    def get_session(self, phone_number: str) -> UserSession:
        """Get or create a session for the given phone number."""
        if phone_number not in self._sessions:
            self._sessions[phone_number] = UserSession(phone_number=phone_number)
        return self._sessions[phone_number]

    def update_session(self, session: UserSession) -> None:
        """Update the session's last interaction time and save it."""
        session.last_interaction = datetime.utcnow()
        self._sessions[session.phone_number] = session

    async def handle_incoming_message(self, phone_number: str, message: str) -> str:
        """
        Process an incoming SMS message and return a response.

        This is the main entry point for SMS messages. It:
        1. Gets or creates a session for the user
        2. Builds context from the current conversation state
        3. Parses the message using Gemini LLM
        4. Processes the parsed intent and generates a response
        5. Updates the session state

        Args:
            phone_number: The sender's phone number.
            message: The text content of the SMS.

        Returns:
            The response message to send back to the user.
        """
        session = self.get_session(phone_number)

        context = None
        if session.state != ConversationState.IDLE:
            context = f"Current state: {session.state.value}"
            if session.pending_request:
                context += f", Pending request: {session.pending_request.model_dump_json()}"

        parsed = await gemini_service.parse_message(message, context)

        response = await self._process_intent(session, parsed)

        self.update_session(session)

        return response

    async def _process_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        """Route the parsed intent to the appropriate handler."""
        if parsed.intent == "book":
            return await self._handle_book_intent(session, parsed)
        elif parsed.intent == "confirm":
            return await self._handle_confirm_intent(session)
        elif parsed.intent == "status":
            return await self._handle_status_intent(session)
        elif parsed.intent == "cancel":
            return await self._handle_cancel_intent(session, parsed)
        elif parsed.intent == "help":
            return parsed.response_message or self._get_help_message()
        else:
            return parsed.response_message or "I'm not sure I understood. Try 'Book Saturday 8am for 4 players'."

    async def _handle_book_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        if not parsed.tee_time_request:
            if parsed.clarification_needed:
                return parsed.clarification_needed
            return "I need more details. What date and time would you like?"

        session.pending_request = parsed.tee_time_request
        session.state = ConversationState.AWAITING_CONFIRMATION

        request = parsed.tee_time_request
        date_str = request.requested_date.strftime("%A, %B %d")
        time_str = request.requested_time.strftime("%I:%M %p")

        return (
            f"I'll book a tee time for {date_str} at {time_str} "
            f"for {request.num_players} players. Reply 'yes' to confirm."
        )

    async def _handle_confirm_intent(self, session: UserSession) -> str:
        if session.state != ConversationState.AWAITING_CONFIRMATION or not session.pending_request:
            return "There's nothing to confirm. Would you like to book a tee time?"

        booking = await self.create_booking(session.phone_number, session.pending_request)

        session.pending_request = None
        session.state = ConversationState.IDLE

        request = booking.request
        date_str = request.requested_date.strftime("%A, %B %d")
        time_str = request.requested_time.strftime("%I:%M %p")
        exec_time = booking.scheduled_execution_time

        if exec_time:
            exec_str = exec_time.strftime("%A at %I:%M %p CT")
            return (
                f"Booking scheduled! I'll attempt to reserve {date_str} at {time_str} "
                f"for {request.num_players} players. The booking window opens {exec_str}. "
                f"I'll text you with the result."
            )
        else:
            return (
                f"Booking request received for {date_str} at {time_str} "
                f"for {request.num_players} players. I'll text you with updates."
            )

    async def _handle_status_intent(self, session: UserSession) -> str:
        user_bookings = [
            b for b in self._bookings.values() if b.phone_number == session.phone_number
        ]

        if not user_bookings:
            return "You don't have any scheduled bookings. Would you like to book a tee time?"

        pending = [b for b in user_bookings if b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED]]
        if not pending:
            return "You don't have any upcoming bookings. Would you like to book a tee time?"

        status_lines = []
        for booking in pending:
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            status_lines.append(f"- {date_str} at {time_str}: {booking.status.value}")

        return "Your upcoming bookings:\n" + "\n".join(status_lines)

    async def _handle_cancel_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        user_bookings = [
            b
            for b in self._bookings.values()
            if b.phone_number == session.phone_number
            and b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED]
        ]

        if not user_bookings:
            return "You don't have any bookings to cancel."

        if len(user_bookings) == 1:
            booking = user_bookings[0]
            booking.status = BookingStatus.CANCELLED
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            return f"Your booking for {date_str} has been cancelled."

        return "Which booking would you like to cancel? Reply with the date."

    async def create_booking(
        self, phone_number: str, request: TeeTimeRequest
    ) -> TeeTimeBooking:
        """
        Create a new booking record and schedule it for execution.

        This is the public API for creating bookings, used by both the SMS
        conversation flow and the REST API.

        Args:
            phone_number: The phone number to associate with the booking.
            request: The tee time request details.

        Returns:
            The created TeeTimeBooking record.
        """
        booking_id = str(uuid.uuid4())[:8]

        execution_time = self._calculate_execution_time(request.requested_date)

        booking = TeeTimeBooking(
            id=booking_id,
            phone_number=phone_number,
            request=request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=execution_time,
        )

        self._bookings[booking_id] = booking
        return booking

    def get_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """
        Get a booking by its ID.

        Args:
            booking_id: The unique identifier of the booking.

        Returns:
            The booking if found, None otherwise.
        """
        return self._bookings.get(booking_id)

    def get_bookings(
        self, phone_number: str | None = None, status: BookingStatus | None = None
    ) -> list[TeeTimeBooking]:
        """
        Get all bookings, optionally filtered by phone number and/or status.

        Args:
            phone_number: Filter by phone number (optional).
            status: Filter by booking status (optional).

        Returns:
            List of matching bookings.
        """
        bookings = list(self._bookings.values())

        if phone_number:
            bookings = [b for b in bookings if b.phone_number == phone_number]

        if status:
            bookings = [b for b in bookings if b.status == status]

        return bookings

    def cancel_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """
        Cancel a booking by its ID.

        Args:
            booking_id: The unique identifier of the booking to cancel.

        Returns:
            The cancelled booking if found and cancellable, None otherwise.
        """
        booking = self._bookings.get(booking_id)
        if not booking:
            return None

        if booking.status not in [BookingStatus.PENDING, BookingStatus.SCHEDULED]:
            return None

        booking.status = BookingStatus.CANCELLED
        return booking

    def _calculate_execution_time(self, target_date: datetime.date) -> datetime:
        tz = pytz.timezone(settings.timezone)

        booking_open_date = target_date - timedelta(days=settings.days_in_advance)

        execution_time = tz.localize(
            datetime.combine(
                booking_open_date,
                datetime.min.time().replace(
                    hour=settings.booking_open_hour,
                    minute=settings.booking_open_minute,
                    second=0,
                ),
            )
        )

        return execution_time

    def _get_help_message(self) -> str:
        """Return a help message explaining how to use the booking service."""
        return (
            "I can help you book tee times at Northgate Country Club!\n\n"
            "Try saying:\n"
            "- 'Book Saturday 8am for 4 players'\n"
            "- 'Check my bookings'\n"
            "- 'Cancel my booking'\n\n"
            "Reservations open 7 days in advance at 6:30am CT."
        )

    async def execute_booking(self, booking_id: str) -> bool:
        """
        Execute a scheduled booking by attempting to reserve on the club website.

        This method is called by the scheduler at the reservation open time
        (6:30am CT, 7 days before the requested date). It:
        1. Retrieves the booking record
        2. Updates status to IN_PROGRESS
        3. Calls the reservation provider to book on the club website
        4. Updates status to SUCCESS or FAILED based on result
        5. Sends SMS notification to the user

        Args:
            booking_id: The unique identifier of the booking to execute.

        Returns:
            True if the booking was successful, False otherwise.
        """
        booking = self.get_booking(booking_id)
        if not booking:
            return False

        if not self._reservation_provider:
            booking.status = BookingStatus.FAILED
            booking.error_message = "Reservation provider not configured"
            await sms_service.send_booking_failure(
                booking.phone_number, "System not configured for booking"
            )
            return False

        booking.status = BookingStatus.IN_PROGRESS

        try:
            result = await self._reservation_provider.book_tee_time(
                date=booking.request.requested_date,
                time=booking.request.requested_time,
                num_players=booking.request.num_players,
                fallback_window_minutes=booking.request.fallback_window_minutes,
            )

            if result.success:
                booking.status = BookingStatus.SUCCESS
                booking.actual_booked_time = result.booked_time
                booking.confirmation_number = result.confirmation_number

                date_str = booking.request.requested_date.strftime("%A, %B %d")
                time_str = (result.booked_time or booking.request.requested_time).strftime(
                    "%I:%M %p"
                )
                details = f"{date_str} at {time_str} for {booking.request.num_players} players"
                if result.confirmation_number:
                    details += f" (Confirmation: {result.confirmation_number})"

                await sms_service.send_booking_confirmation(booking.phone_number, details)
                return True
            else:
                booking.status = BookingStatus.FAILED
                booking.error_message = result.error_message

                await sms_service.send_booking_failure(
                    booking.phone_number,
                    result.error_message or "Unknown error",
                    result.alternatives,
                )
                return False

        except Exception as e:
            booking.status = BookingStatus.FAILED
            booking.error_message = str(e)
            await sms_service.send_booking_failure(booking.phone_number, str(e))
            return False

    def get_pending_bookings(self) -> list[TeeTimeBooking]:
        return [
            b for b in self._bookings.values() if b.status == BookingStatus.SCHEDULED
        ]


booking_service = BookingService()
