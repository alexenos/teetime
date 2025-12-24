"""
Booking service for managing tee time reservations.

This module provides the core business logic for handling SMS conversations,
processing booking requests, and executing reservations at the scheduled time.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

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
from app.services.database_service import database_service
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

    Uses database storage for sessions and bookings via DatabaseService,
    ensuring data persists across restarts.

    Attributes:
        _reservation_provider: Provider for executing bookings on the club website.
    """

    def __init__(self) -> None:
        """Initialize the booking service."""
        self._reservation_provider: ReservationProvider | None = None

    def set_reservation_provider(self, provider: ReservationProvider) -> None:
        """Set the reservation provider for executing bookings."""
        self._reservation_provider = provider

    async def get_session(self, phone_number: str) -> UserSession:
        """Get or create a session for the given phone number."""
        return await database_service.get_or_create_session(phone_number)

    async def update_session(self, session: UserSession) -> None:
        """Update the session's last interaction time and save it."""
        session.last_interaction = datetime.now(UTC).replace(tzinfo=None)
        await database_service.update_session(session)

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
        session = await self.get_session(phone_number)

        context = None
        if session.state != ConversationState.IDLE:
            context = f"Current state: {session.state.value}"
            if session.pending_request:
                context += f", Pending request: {session.pending_request.model_dump_json()}"

        parsed = await gemini_service.parse_message(message, context)

        response = await self._process_intent(session, parsed)

        await self.update_session(session)

        return response

    async def _process_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        """Route the parsed intent to the appropriate handler."""
        # Check if user is in the middle of selecting a booking to cancel
        # This takes priority over Gemini's parsed intent to avoid misinterpreting
        # date/time responses as new booking requests
        if session.state == ConversationState.AWAITING_CANCELLATION_SELECTION:
            return await self._handle_cancellation_selection(session, parsed)

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
            return (
                parsed.response_message
                or "I'm not sure I understood. Try 'Book Saturday 8am for 4 players'."
            )

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

        try:
            booking = await self.create_booking(session.phone_number, session.pending_request)
        except ValueError as e:
            # 48-hour restriction for multi-player bookings
            session.pending_request = None
            session.state = ConversationState.IDLE
            return str(e)

        session.pending_request = None
        session.state = ConversationState.IDLE

        request = booking.request
        date_str = request.requested_date.strftime("%A, %B %d")
        time_str = request.requested_time.strftime("%I:%M %p")

        if booking.status == BookingStatus.SUCCESS:
            booked_time_str = (
                booking.actual_booked_time.strftime("%I:%M %p")
                if booking.actual_booked_time
                else time_str
            )
            return (
                f"Booking confirmed! Reserved {date_str} at {booked_time_str} "
                f"for {request.num_players} players."
            )
        elif booking.status == BookingStatus.FAILED:
            return (
                f"Booking attempted for {date_str} at {time_str} "
                f"for {request.num_players} players, but it failed. "
                f"I'll text you with more details."
            )
        elif booking.status == BookingStatus.IN_PROGRESS:
            return (
                f"Booking in progress for {date_str} at {time_str} "
                f"for {request.num_players} players. I'll text you with the result."
            )

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
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)

        if not user_bookings:
            return "You don't have any scheduled bookings. Would you like to book a tee time?"

        pending = [
            b for b in user_bookings if b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED]
        ]
        if not pending:
            return "You don't have any upcoming bookings. Would you like to book a tee time?"

        status_lines = []
        for booking in pending:
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            status_lines.append(f"- {date_str} at {time_str}: {booking.status.value}")

        return "Your upcoming bookings:\n" + "\n".join(status_lines)

    async def _handle_cancel_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)
        cancellable = [
            b
            for b in user_bookings
            if b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED, BookingStatus.SUCCESS]
        ]

        if not cancellable:
            return "You don't have any bookings to cancel."

        # Check if user is confirming a pending cancellation
        if session.pending_cancellation_id:
            # User is responding to a confirmation prompt
            message_lower = parsed.raw_message.lower() if parsed.raw_message else ""
            if any(word in message_lower for word in ["yes", "confirm", "ok", "sure", "y"]):
                booking = await database_service.get_booking(session.pending_cancellation_id)
                if booking:
                    date_str = booking.request.requested_date.strftime("%A, %B %d")
                    session.pending_cancellation_id = None
                    await self.update_session(session)

                    if booking.status == BookingStatus.SUCCESS:
                        success = await self._cancel_confirmed_booking(booking)
                        if success:
                            return (
                                f"Your confirmed booking for {date_str} has been cancelled "
                                "on the website."
                            )
                        else:
                            return (
                                f"I was unable to cancel your booking for {date_str} on the "
                                "website. Please contact the club directly to cancel."
                            )
                    else:
                        booking.status = BookingStatus.CANCELLED
                        await database_service.update_booking(booking)
                        return f"Your booking for {date_str} has been cancelled."
            else:
                # User declined or gave unclear response
                session.pending_cancellation_id = None
                await self.update_session(session)
                return "Cancellation cancelled. Your booking remains active."

        # Always ask for confirmation before cancelling, even for single bookings
        if len(cancellable) == 1:
            booking = cancellable[0]
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            status_label = "confirmed" if booking.status == BookingStatus.SUCCESS else "scheduled"

            # Store the pending cancellation and ask for confirmation
            session.pending_cancellation_id = booking.id
            await self.update_session(session)

            return (
                f"Are you sure you want to cancel your {status_label} booking for "
                f"{date_str} at {time_str}? Reply 'yes' to confirm."
            )

        # Multiple bookings - ask which one to cancel and set state
        # Sort by date/time for consistent ordering when user replies with a number
        cancellable.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))
        session.state = ConversationState.AWAITING_CANCELLATION_SELECTION

        status_lines = []
        for i, booking in enumerate(cancellable, 1):
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            status_label = "confirmed" if booking.status == BookingStatus.SUCCESS else "scheduled"
            players = booking.request.num_players
            status_lines.append(
                f"{i}. {date_str} at {time_str} for {players} players ({status_label})"
            )

        return "Which booking would you like to cancel? Reply with the number.\n" + "\n".join(
            status_lines
        )

    async def _handle_cancellation_selection(
        self, session: UserSession, parsed: ParsedIntent
    ) -> str:
        """
        Handle user's response when selecting which booking to cancel.

        This method is called when the session is in AWAITING_CANCELLATION_SELECTION state,
        meaning we previously asked the user which booking they want to cancel from a list.
        We try to match their response to one of their cancellable bookings using:
        1. A number (e.g., "1", "2", "3") corresponding to the numbered list
        2. A date/time extracted by Gemini as a fallback

        Args:
            session: The user's session.
            parsed: The parsed intent from Gemini (may have extracted date/time info).

        Returns:
            Response message to send to the user.
        """
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)
        cancellable = [
            b
            for b in user_bookings
            if b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED, BookingStatus.SUCCESS]
        ]

        if not cancellable:
            session.state = ConversationState.IDLE
            return "You don't have any bookings to cancel."

        # Sort by date/time for consistent ordering (same as when we displayed the list)
        cancellable.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))

        matched_booking = None
        raw_message = (parsed.raw_message or "").strip()

        # First, try to parse as a number (e.g., "1", "2", "3")
        try:
            selection_num = int(raw_message)
            if 1 <= selection_num <= len(cancellable):
                matched_booking = cancellable[selection_num - 1]
        except ValueError:
            pass

        # If not a number, try to match by date/time from Gemini's parsing
        if not matched_booking and parsed.tee_time_request:
            target_date = parsed.tee_time_request.requested_date
            target_time = parsed.tee_time_request.requested_time

            # Find bookings matching the date
            date_matches = [b for b in cancellable if b.request.requested_date == target_date]

            if len(date_matches) == 1:
                matched_booking = date_matches[0]
            elif len(date_matches) > 1:
                # Multiple bookings on same date - try to match by time
                time_matches = [b for b in date_matches if b.request.requested_time == target_time]
                if len(time_matches) == 1:
                    matched_booking = time_matches[0]
                elif len(time_matches) > 1:
                    # Still multiple matches - tell user to use the number
                    return (
                        "You have multiple bookings at that time. "
                        "Please reply with the number (1, 2, 3, etc.) from the list."
                    )
                else:
                    # No exact time match, but multiple date matches
                    # Tell user to use the number
                    return (
                        f"You have multiple bookings on {target_date.strftime('%B %d')}. "
                        "Please reply with the number (1, 2, 3, etc.) from the list."
                    )

        if not matched_booking:
            # Could not match - keep state and ask user to try again with number
            return (
                "I couldn't match that to a booking. "
                "Please reply with the number (1, 2, 3, etc.) from the list."
            )

        # Found a matching booking - set up for confirmation
        date_str = matched_booking.request.requested_date.strftime("%A, %B %d")
        time_str = matched_booking.request.requested_time.strftime("%I:%M %p")
        status_label = (
            "confirmed" if matched_booking.status == BookingStatus.SUCCESS else "scheduled"
        )

        # Store the pending cancellation and transition to confirmation
        session.pending_cancellation_id = matched_booking.id
        session.state = ConversationState.IDLE

        return (
            f"Are you sure you want to cancel your {status_label} booking for "
            f"{date_str} at {time_str}? Reply 'yes' to confirm."
        )

    async def _cancel_confirmed_booking(self, booking: TeeTimeBooking) -> bool:
        """
        Cancel a confirmed booking on the club website.

        This method calls the reservation provider to cancel the booking on the
        actual website, then updates the booking status in the database.

        Args:
            booking: The confirmed booking to cancel.

        Returns:
            True if cancellation was successful, False otherwise.
        """
        if not self._reservation_provider:
            return False

        booked_time = booking.actual_booked_time or booking.request.requested_time
        cancellation_id = (
            f"{booking.request.requested_date.strftime('%Y-%m-%d')}_"
            f"{booked_time.strftime('%H:%M')}"
        )

        try:
            success = await self._reservation_provider.cancel_booking(cancellation_id)

            if success:
                booking.status = BookingStatus.CANCELLED
                await database_service.update_booking(booking)
                return True
            else:
                return False

        except Exception as e:
            booking.error_message = f"Cancellation failed: {str(e)}"
            await database_service.update_booking(booking)
            return False

    async def create_booking(self, phone_number: str, request: TeeTimeRequest) -> TeeTimeBooking:
        """
        Create a new booking record and schedule it for execution.

        This is the public API for creating bookings, used by both the SMS
        conversation flow and the REST API.

        If the calculated execution time is in the past (i.e., the booking window
        has already opened), the booking is executed immediately instead of being
        scheduled for later.

        Multi-player bookings (2+ players) are rejected if the tee time is within
        48 hours, because the Walden Golf website disables TBD guest placeholders
        within that window.

        Args:
            phone_number: The phone number to associate with the booking.
            request: The tee time request details.

        Returns:
            The created TeeTimeBooking record.

        Raises:
            ValueError: If multi-player booking is requested within 48 hours.
        """
        # Check 48-hour restriction for multi-player bookings
        if request.num_players > 1:
            tz = pytz.timezone(settings.timezone)
            now_ct = datetime.now(tz)

            # Combine requested date and time into a timezone-aware datetime
            tee_time_naive = datetime.combine(request.requested_date, request.requested_time)
            tee_time_ct = tz.localize(tee_time_naive)

            hours_until_tee_time = (tee_time_ct - now_ct).total_seconds() / 3600

            if hours_until_tee_time < 48:
                raise ValueError(
                    f"Multi-player bookings ({request.num_players} players) cannot be made "
                    f"within 48 hours of the tee time. The Walden Golf website disables "
                    f"TBD guest placeholders within this window. "
                    f"You can still book for 1 player, or choose a tee time more than 48 hours away."
                )

        booking_id = str(uuid.uuid4())[:8]

        execution_time = self._calculate_execution_time(request.requested_date)

        booking = TeeTimeBooking(
            id=booking_id,
            phone_number=phone_number,
            request=request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=execution_time,
        )

        created_booking = await database_service.create_booking(booking)

        # Compare in timezone-aware space for robustness.
        # execution_time is naive (CT wall-clock), so we localize it.
        # This approach is safe even if _calculate_execution_time() is later
        # changed to return an aware datetime.
        tz = pytz.timezone(settings.timezone)
        now_ct = datetime.now(tz)
        if execution_time.tzinfo is None:
            exec_ct = tz.localize(execution_time)
        else:
            exec_ct = execution_time.astimezone(tz)

        if exec_ct <= now_ct:
            await self.execute_booking(created_booking.id)
            return await database_service.get_booking(created_booking.id) or created_booking

        return created_booking

    async def get_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """
        Get a booking by its ID.

        Args:
            booking_id: The unique identifier of the booking.

        Returns:
            The booking if found, None otherwise.
        """
        return await database_service.get_booking(booking_id)

    async def get_bookings(
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
        return await database_service.get_bookings(phone_number=phone_number, status=status)

    async def cancel_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """
        Cancel a booking by its ID.

        For PENDING/SCHEDULED bookings, this simply updates the status to CANCELLED.
        For SUCCESS bookings (already confirmed on the website), this calls the
        reservation provider to cancel on the actual website.

        Args:
            booking_id: The unique identifier of the booking to cancel.

        Returns:
            The cancelled booking if found and cancellable, None otherwise.
        """
        booking = await database_service.get_booking(booking_id)
        if not booking:
            return None

        if booking.status not in [
            BookingStatus.PENDING,
            BookingStatus.SCHEDULED,
            BookingStatus.SUCCESS,
        ]:
            return None

        if booking.status == BookingStatus.SUCCESS:
            success = await self._cancel_confirmed_booking(booking)
            if not success:
                return None
            return await database_service.get_booking(booking_id)

        booking.status = BookingStatus.CANCELLED
        return await database_service.update_booking(booking)

    def _calculate_execution_time(self, target_date: date) -> datetime:
        """
        Calculate when the booking should be executed.

        Returns a naive datetime representing CT wall-clock time.
        The timezone is used for DST-correct calculation but stripped
        before returning to match the database schema (timestamp without timezone).
        """
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

        # Return naive datetime (strip timezone) for database storage
        # The value represents CT wall-clock time
        return execution_time.replace(tzinfo=None)

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
        booking = await self.get_booking(booking_id)
        if not booking:
            return False

        if not self._reservation_provider:
            booking.status = BookingStatus.FAILED
            booking.error_message = "Reservation provider not configured"
            await database_service.update_booking(booking)
            await sms_service.send_booking_failure(
                booking.phone_number, "System not configured for booking"
            )
            return False

        booking.status = BookingStatus.IN_PROGRESS
        await database_service.update_booking(booking)

        try:
            result = await self._reservation_provider.book_tee_time(
                target_date=booking.request.requested_date,
                target_time=booking.request.requested_time,
                num_players=booking.request.num_players,
                fallback_window_minutes=booking.request.fallback_window_minutes,
            )

            if result.success:
                booking.status = BookingStatus.SUCCESS
                booking.actual_booked_time = result.booked_time
                booking.confirmation_number = result.confirmation_number
                await database_service.update_booking(booking)

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
                await database_service.update_booking(booking)

                await sms_service.send_booking_failure(
                    booking.phone_number,
                    result.error_message or "Unknown error",
                    result.alternatives,
                )
                return False

        except Exception as e:
            booking.status = BookingStatus.FAILED
            booking.error_message = str(e)
            await database_service.update_booking(booking)
            await sms_service.send_booking_failure(booking.phone_number, str(e))
            return False

    async def get_pending_bookings(self) -> list[TeeTimeBooking]:
        return await database_service.get_bookings(status=BookingStatus.SCHEDULED)

    async def get_due_bookings(self, current_time: datetime) -> list[TeeTimeBooking]:
        """
        Get all scheduled bookings that are due for execution.

        A booking is due when its scheduled_execution_time is <= current_time.
        This is used by the Cloud Scheduler job to find bookings to execute.

        The filtering is performed at the database layer for efficiency.
        Timezone handling: scheduled_execution_time is stored as naive datetime
        in CT wall-clock time. We strip tzinfo from current_time to ensure
        consistent naive-to-naive comparison in the database query.

        Args:
            current_time: The current time (timezone-aware in CT) to compare against.

        Returns:
            List of bookings that are due for execution.
        """
        naive_current_time = current_time.replace(tzinfo=None)
        return await database_service.get_due_bookings(naive_current_time)


booking_service = BookingService()
