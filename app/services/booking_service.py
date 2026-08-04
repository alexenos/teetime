"""
Booking service for managing tee time reservations.

This module provides the core business logic for handling SMS conversations,
processing booking requests, and executing reservations at the scheduled time.
"""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from app.config import settings
from app.models.schemas import (
    BookingStatus,
    ConversationState,
    ParsedIntent,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)
from app.providers.base import BatchBookingRequest, BookingResult, ReservationProvider
from app.services.database_service import database_service
from app.services.gemini_service import gemini_service
from app.services.sms_service import sms_service
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

# Error recorded on bookings that were still IN_PROGRESS when the process died.
# The club website may or may not have accepted the reservation, so the message
# deliberately tells the user to verify rather than asserting either outcome.
INTERRUPTED_ERROR_MESSAGE = (
    "The booking attempt was interrupted before it finished (the service "
    "restarted mid-attempt). The reservation may or may not have gone through - "
    "please check the club website before rebooking."
)


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
        # Strong references to in-flight background booking tasks. asyncio only
        # holds weak references to tasks, so without this a booking run could be
        # garbage collected mid-attempt.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # When this instance came up, in the same naive-UTC space the database
        # stamps onto updated_at. Startup reconciliation uses it to tell rows
        # orphaned by a previous process from attempts running right now.
        self._started_at = datetime.now(UTC).replace(tzinfo=None)

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

    async def handle_incoming_message(
        self, phone_number: str, message: str, origin_channel_id: str | None = None
    ) -> str:
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
            origin_channel_id: For Discord, the channel this message arrived in.
                Stored on the session and copied onto any booking created from
                this conversation, so the booking result days later replies in
                the same place. None for SMS, which has no channels.

        Returns:
            The response message to send back to the user.
        """
        session = await self.get_session(phone_number)
        if origin_channel_id:
            session.origin_channel_id = origin_channel_id

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
        if parsed.tee_time_requests and len(parsed.tee_time_requests) > 1:
            session.pending_requests = parsed.tee_time_requests
            session.pending_request = None
            session.state = ConversationState.AWAITING_CONFIRMATION

            booking_summaries = []
            for i, request in enumerate(parsed.tee_time_requests, 1):
                date_str = request.requested_date.strftime("%A, %B %d")
                time_str = request.requested_time.strftime("%I:%M %p")
                booking_summaries.append(
                    f"{i}. {date_str} at {time_str} for {request.num_players} players"
                )

            return (
                f"I'll book {len(parsed.tee_time_requests)} tee times:\n"
                + "\n".join(booking_summaries)
                + "\n\nReply 'yes' to confirm all bookings."
            )

        if not parsed.tee_time_request:
            if parsed.clarification_needed:
                return parsed.clarification_needed
            return "I need more details. What date and time would you like?"

        session.pending_request = parsed.tee_time_request
        session.pending_requests = None
        session.state = ConversationState.AWAITING_CONFIRMATION

        request = parsed.tee_time_request
        date_str = request.requested_date.strftime("%A, %B %d")
        time_str = request.requested_time.strftime("%I:%M %p")

        return (
            f"I'll book a tee time for {date_str} at {time_str} "
            f"for {request.num_players} players. Reply 'yes' to confirm."
        )

    async def _handle_confirm_intent(self, session: UserSession) -> str:
        if session.state != ConversationState.AWAITING_CONFIRMATION:
            return "There's nothing to confirm. Would you like to book a tee time?"

        if session.pending_requests and len(session.pending_requests) > 0:
            return await self._handle_confirm_multiple_bookings(session)

        if not session.pending_request:
            return "There's nothing to confirm. Would you like to book a tee time?"

        try:
            booking = await self.create_booking(
                session.phone_number, session.pending_request, session.origin_channel_id
            )
        except ValueError as e:
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
            # The booking window is already open, so the attempt is running right
            # now in the background. This is the acknowledgement the user gets
            # up front; the outcome arrives as a separate message.
            return (
                f"On it - booking {date_str} at {time_str} "
                f"for {request.num_players} players now. "
                f"This takes a minute or two; I'll message you with the result."
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

    async def _handle_confirm_multiple_bookings(self, session: UserSession) -> str:
        """Handle confirmation of multiple booking requests."""
        if not session.pending_requests:
            return "There's nothing to confirm. Would you like to book a tee time?"

        successful_bookings: list[TeeTimeBooking] = []
        failed_requests: list[tuple[TeeTimeRequest, str]] = []

        for request in session.pending_requests:
            try:
                booking = await self.create_booking(
                    session.phone_number,
                    request,
                    session.origin_channel_id,
                    defer_execution=True,
                )
                successful_bookings.append(booking)
            except ValueError as e:
                failed_requests.append((request, str(e)))

        session.pending_requests = None
        session.pending_request = None
        session.state = ConversationState.IDLE

        # Bookings whose window is already open run as ONE batch: a single
        # driver, a single login, attempts made in sequence. Letting
        # create_booking spawn its own task per booking put two headless Chromes
        # on the club's login form ~80ms apart; the portal accepted one and left
        # the other sitting on the login page, failing that booking outright.
        immediate_booking_ids = [
            booking.id
            for booking in successful_bookings
            if booking.status != BookingStatus.SCHEDULED and booking.id is not None
        ]
        if immediate_booking_ids:
            self._spawn_bookings_batch_execution(immediate_booking_ids)

        response_parts = []

        if successful_bookings:
            scheduled_bookings = [
                b for b in successful_bookings if b.status == BookingStatus.SCHEDULED
            ]
            immediate_bookings = [
                b for b in successful_bookings if b.status != BookingStatus.SCHEDULED
            ]

            if scheduled_bookings:
                booking_summaries = []
                for booking in scheduled_bookings:
                    date_str = booking.request.requested_date.strftime("%A, %B %d")
                    time_str = booking.request.requested_time.strftime("%I:%M %p")
                    booking_summaries.append(
                        f"- {date_str} at {time_str} for {booking.request.num_players} players"
                    )

                if len(scheduled_bookings) == 1:
                    exec_time = scheduled_bookings[0].scheduled_execution_time
                    exec_str = exec_time.strftime("%A at %I:%M %p CT") if exec_time else "soon"
                    response_parts.append(
                        f"Booking scheduled! The booking window opens {exec_str}:\n"
                        + "\n".join(booking_summaries)
                    )
                else:
                    response_parts.append(
                        f"{len(scheduled_bookings)} bookings scheduled! "
                        "I'll text you with results when the booking windows open:\n"
                        + "\n".join(booking_summaries)
                    )

            if immediate_bookings:
                for booking in immediate_bookings:
                    date_str = booking.request.requested_date.strftime("%A, %B %d")
                    time_str = booking.request.requested_time.strftime("%I:%M %p")
                    if booking.status == BookingStatus.SUCCESS:
                        booked_time_str = (
                            booking.actual_booked_time.strftime("%I:%M %p")
                            if booking.actual_booked_time
                            else time_str
                        )
                        response_parts.append(
                            f"Booking confirmed! Reserved {date_str} at {booked_time_str} "
                            f"for {booking.request.num_players} players."
                        )
                    elif booking.status == BookingStatus.FAILED:
                        response_parts.append(
                            f"Booking for {date_str} at {time_str} failed. "
                            "I'll text you with more details."
                        )
                    elif booking.status == BookingStatus.IN_PROGRESS:
                        response_parts.append(
                            f"On it - booking {date_str} at {time_str} now. "
                            "I'll message you with the result."
                        )

        if failed_requests:
            for request, error in failed_requests:
                date_str = request.requested_date.strftime("%A, %B %d")
                time_str = request.requested_time.strftime("%I:%M %p")
                response_parts.append(f"Could not schedule {date_str} at {time_str}: {error}")

        return "\n\n".join(response_parts) if response_parts else "No bookings were created."

    async def _handle_status_intent(self, session: UserSession) -> str:
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)

        if not user_bookings:
            return "You don't have any scheduled bookings. Would you like to book a tee time?"

        today = CTDateTime.now().date()
        upcoming = [b for b in user_bookings if b.request.requested_date >= today]

        # IN_PROGRESS and SUCCESS belong here alongside PENDING/SCHEDULED. Leaving
        # them out once made a booking that was mid-attempt look like it had never
        # been created at all, which is worse than showing an unfinished state.
        active = [
            b
            for b in upcoming
            if b.status
            in [
                BookingStatus.PENDING,
                BookingStatus.SCHEDULED,
                BookingStatus.IN_PROGRESS,
                BookingStatus.SUCCESS,
            ]
        ]
        # Failures are only worth surfacing for tee times that haven't happened
        # yet - those are the ones the user can still do something about.
        failed = [b for b in upcoming if b.status == BookingStatus.FAILED]

        if not active and not failed:
            return "You don't have any upcoming bookings. Would you like to book a tee time?"

        def describe(booking: TeeTimeBooking) -> str:
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            if booking.status == BookingStatus.SUCCESS and booking.actual_booked_time:
                time_str = booking.actual_booked_time.strftime("%I:%M %p")
            return f"- {date_str} at {time_str}: {booking.status.value}"

        active.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))
        failed.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))

        sections = []
        if active:
            sections.append("Your upcoming bookings:\n" + "\n".join(describe(b) for b in active))
        if failed:
            failure_lines = []
            for booking in failed:
                line = describe(booking)
                if booking.error_message:
                    line += f"\n  {booking.error_message}"
                failure_lines.append(line)
            sections.append("Recent failures:\n" + "\n".join(failure_lines))

        return "\n\n".join(sections)

    async def reconcile_interrupted_bookings(self) -> list[TeeTimeBooking]:
        """Resolve bookings left IN_PROGRESS by a crash or restart.

        A booking attempt drives Selenium for a minute or more with the row held
        at IN_PROGRESS. If the process dies in that window (an OOM kill, a
        deploy, a Cloud Run instance replacement), nothing ever moves the row
        off IN_PROGRESS and nothing tells the user - the request simply goes
        quiet.

        Only rows last touched before this process started are treated as
        orphans. This instance can set IN_PROGRESS itself - through the REST
        API, the scheduler's batch job, or a confirmed chat booking - and those
        attempts are still running, so failing them here would both lie to the
        user and clobber the real outcome they are about to write.

        Each orphan is marked FAILED with an explanation that the reservation's
        true state is unknown, and the user is notified. Callers should invoke
        this once at startup, after the messaging channel is ready.

        Returns:
            The bookings that were successfully reconciled.
        """
        in_progress = await database_service.get_bookings(status=BookingStatus.IN_PROGRESS)
        orphaned = [b for b in in_progress if b.updated_at < self._started_at]
        if not orphaned:
            return []

        live = len(in_progress) - len(orphaned)
        logger.warning(
            "Found %d booking(s) stuck IN_PROGRESS from a previous run; marking failed "
            "(%d in-progress booking(s) started by this instance left alone)",
            len(orphaned),
            live,
        )

        reconciled: list[TeeTimeBooking] = []
        for booking in orphaned:
            booking.status = BookingStatus.FAILED
            booking.error_message = INTERRUPTED_ERROR_MESSAGE

            try:
                await database_service.update_booking(booking)
            except Exception:
                # One unwritable row must not strand every other orphan. Skip it
                # and leave it for the next startup rather than telling the user
                # about a failure we could not actually record.
                logger.exception("Could not reconcile interrupted booking %s", booking.id)
                continue

            reconciled.append(booking)

            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            booking_details = f"{date_str} at {time_str} for {booking.request.num_players} players"
            logger.warning("Reconciled interrupted booking %s (%s)", booking.id, booking_details)

            try:
                await sms_service.send_booking_failure(
                    booking.phone_number,
                    INTERRUPTED_ERROR_MESSAGE,
                    booking_details=booking_details,
                    origin_channel_id=booking.origin_channel_id,
                )
            except Exception:
                # A notification failure must not stop us reconciling the rest.
                logger.exception("Could not notify user about interrupted booking %s", booking.id)

        return reconciled

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
            f"{booking.request.requested_date.strftime('%Y-%m-%d')}_{booked_time.strftime('%H:%M')}"
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

    async def create_booking(
        self,
        phone_number: str,
        request: TeeTimeRequest,
        origin_channel_id: str | None = None,
        defer_execution: bool = False,
    ) -> TeeTimeBooking:
        """
        Create a new booking record and schedule it for execution.

        This is the public API for creating bookings, used by both the SMS
        conversation flow and the REST API.

        If the calculated execution time is in the past (i.e., the booking window
        has already opened), the attempt starts immediately in the background
        rather than being scheduled for later. This call returns as soon as the
        record is created, with the booking in IN_PROGRESS; the outcome is
        reported to the user by execute_booking when the attempt finishes.

        Multi-player bookings (2+ players) are rejected if the tee time is within
        48 hours, because the Walden Golf website disables TBD guest placeholders
        within that window.

        Args:
            phone_number: The phone number to associate with the booking.
            request: The tee time request details.
            origin_channel_id: Discord channel this booking was requested in, so
                the success/failure notification replies there. None for SMS and
                REST API callers.
            defer_execution: Create the record and mark it IN_PROGRESS, but do
                not start the attempt. Callers creating several bookings at once
                set this so they can run the whole set as one batch instead of
                one concurrent attempt per booking; they then own both starting
                the work and reporting the outcome.

        Returns:
            The created TeeTimeBooking record.

        Raises:
            ValueError: If multi-player booking is requested within 48 hours.
        """
        # Check 48-hour restriction for multi-player bookings
        if request.num_players > 1:
            now_ct = CTDateTime.now()

            # Combine requested date and time into a timezone-aware CT datetime
            tee_time_naive = datetime.combine(request.requested_date, request.requested_time)
            tee_time_ct = CTDateTime.from_naive_ct(tee_time_naive)

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
            origin_channel_id=origin_channel_id,
        )

        created_booking = await database_service.create_booking(booking)

        # Compare in timezone-aware space for robustness.
        # execution_time is naive (CT wall-clock), so we localize it.
        # normalize_to_ct handles both naive and aware inputs safely.
        now_ct = CTDateTime.now()
        exec_ct = CTDateTime.normalize_to_ct(execution_time)

        if exec_ct <= now_ct:
            booking_id_opt: str | None = created_booking.id
            if booking_id_opt is not None:
                # The booking window is already open, so this runs now rather
                # than waiting for the scheduler. Run it as a background task
                # instead of awaiting it: a booking attempt drives Selenium for
                # a minute or more, and the caller (the Discord gateway) cannot
                # send its reply until this returns. Awaiting it here means any
                # crash during the attempt takes the user's reply down with it,
                # leaving them with silence. The caller now acknowledges
                # immediately and execute_booking sends the real outcome when
                # it lands.
                created_booking.status = BookingStatus.IN_PROGRESS
                await database_service.update_booking(created_booking)
                if not defer_execution:
                    self._spawn_booking_execution(booking_id_opt)

        return created_booking

    def _spawn_booking_execution(self, booking_id: str) -> None:
        """Run execute_booking as a tracked background task."""
        task = asyncio.create_task(
            self._execute_booking_in_background(booking_id),
            name=f"execute-booking-{booking_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _execute_booking_in_background(self, booking_id: str) -> None:
        """Execute a booking, ensuring failures are logged rather than swallowed.

        execute_booking already reports its own outcome to the user and records
        it on the booking row. This wrapper only guards against an exception
        escaping into an unretrieved task result, where it would vanish silently.
        """
        try:
            await self.execute_booking(booking_id)
        except Exception:
            logger.exception("Background booking execution failed for %s", booking_id)

    def _spawn_bookings_batch_execution(self, booking_ids: list[str]) -> None:
        """Run several already-open bookings as one tracked background task."""
        task = asyncio.create_task(
            self._execute_bookings_batch_in_background(booking_ids),
            name=f"execute-bookings-batch-{len(booking_ids)}-from-{booking_ids[0]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _execute_bookings_batch_in_background(self, booking_ids: list[str]) -> None:
        """Attempt a set of bookings in one session, ensuring failures are logged.

        Mirrors _execute_booking_in_background: the work itself reports its own
        outcomes, and this wrapper only stops an exception from escaping into an
        unretrieved task result, where it would vanish silently.
        """
        try:
            await self._run_bookings_batch(booking_ids)
        except Exception:
            logger.exception("Background batch booking execution failed for %s", booking_ids)

    async def _run_bookings_batch(self, booking_ids: list[str]) -> None:
        """Book a set of tee times in one session and report each outcome.

        execute_bookings_batch deliberately leaves notification to its caller,
        so this reports every booking itself. Each of these bookings has already
        been acknowledged to the user, so every path out of here has to end in a
        message per booking - silence would leave them waiting on a reply that
        never comes. That is also why each notification is isolated: one booking
        whose message fails to send must not strand the rest of the batch.
        """
        bookings = [
            booking
            for booking in [await self.get_booking(booking_id) for booking_id in booking_ids]
            if booking is not None
        ]
        if not bookings:
            logger.error("Batch booking execution found no records for %s", booking_ids)
            return

        try:
            results = await self.execute_bookings_batch(bookings, execute_at=None)
        except Exception as e:
            logger.exception("Batch booking attempt failed for %s", booking_ids)
            for booking in bookings:
                await self._try_notify_unreported_booking(booking, str(e))
            return

        reported: set[str] = set()
        for booking_id, result in results:
            attempted = await self.get_booking(booking_id)
            if attempted is None:
                continue
            reported.add(booking_id)
            await self._try_notify_booking_result(attempted, result)

        # execute_bookings_batch appends a result per request on every path it
        # returns from, so this is a backstop against a booking silently going
        # unanswered rather than an expected branch.
        for booking in bookings:
            if booking.id is not None and booking.id not in reported:
                await self._try_notify_unreported_booking(
                    booking, "The booking attempt ended without reporting a result."
                )

    async def _try_notify_booking_result(
        self, booking: TeeTimeBooking, result: BookingResult
    ) -> None:
        """Report one outcome, keeping a send failure from stranding the batch."""
        try:
            await self._notify_booking_result(booking, result)
        except Exception:
            logger.exception("Could not report the outcome of booking %s", booking.id)

    async def _try_notify_unreported_booking(self, booking: TeeTimeBooking, error: str) -> None:
        """Same, for a booking the batch never accounted for."""
        try:
            await self._notify_unreported_booking(booking, error)
        except Exception:
            logger.exception("Could not report unaccounted booking %s", booking.id)

    async def _notify_unreported_booking(self, booking: TeeTimeBooking, error: str) -> None:
        """Report a booking the batch left unaccounted for.

        The batch may have resolved and persisted the row before failing, so
        this reports what was actually recorded and only invents a failure for
        rows still sitting in IN_PROGRESS.
        """
        persisted = await self.get_booking(booking.id) if booking.id is not None else None
        current = persisted or booking

        if current.status == BookingStatus.SUCCESS:
            await self._notify_booking_result(
                current,
                BookingResult(
                    success=True,
                    booked_time=current.actual_booked_time,
                    confirmation_number=current.confirmation_number,
                ),
            )
            return

        # Only write back a row that is still there to write to; update_booking
        # raises on a missing record, and the message matters more than the row.
        if current.status != BookingStatus.FAILED:
            current.status = BookingStatus.FAILED
            current.error_message = error
            if persisted is not None:
                await database_service.update_booking(current)

        await self._notify_booking_result(
            current,
            BookingResult(success=False, error_message=current.error_message or error),
        )

    async def wait_for_background_bookings(self, timeout: float | None = None) -> None:
        """Wait for all in-flight background booking attempts to finish.

        Used by shutdown and by tests, which need the attempt to complete before
        asserting on its result.

        Args:
            timeout: Seconds to wait before cancelling whatever is still
                running. A booking attempt drives Selenium and can hang, so
                shutdown passes a bound to keep one stuck attempt from blocking
                gateway and provider cleanup. None waits indefinitely.
        """
        while self._background_tasks:
            pending = tuple(self._background_tasks)
            if timeout is None:
                await asyncio.gather(*pending, return_exceptions=True)
                continue

            _, still_running = await asyncio.wait(pending, timeout=timeout)
            if still_running:
                logger.warning(
                    "Cancelling %d booking attempt(s) still running after %ss",
                    len(still_running),
                    timeout,
                )
                for task in still_running:
                    task.cancel()
                await asyncio.gather(*still_running, return_exceptions=True)
            return

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
        booking_open_date = target_date - timedelta(days=settings.days_in_advance)

        execution_time = datetime.combine(
            booking_open_date,
            datetime.min.time().replace(
                hour=settings.booking_open_hour,
                minute=settings.booking_open_minute,
                second=0,
            ),
        ).replace(tzinfo=CTDateTime.CT_TZ)

        # Return naive datetime (strip timezone) for database storage
        # The value represents CT wall-clock time
        return CTDateTime.to_naive_ct(execution_time)

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

            await self._notify_booking_result(
                booking,
                BookingResult(success=False, error_message="System not configured for booking"),
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

                await self._notify_booking_result(booking, result)
                return True
            else:
                booking.status = BookingStatus.FAILED
                booking.error_message = result.error_message
                await database_service.update_booking(booking)

                await self._notify_booking_result(booking, result)
                return False

        except Exception as e:
            booking.status = BookingStatus.FAILED
            booking.error_message = str(e)
            await database_service.update_booking(booking)

            await self._notify_booking_result(
                booking, BookingResult(success=False, error_message=str(e))
            )
            return False

    async def _notify_booking_result(self, booking: TeeTimeBooking, result: BookingResult) -> None:
        """Tell the user how a finished booking attempt turned out.

        Shared by every path that runs an attempt, so an ad-hoc booking reads
        the same to the user whether it ran alone or as part of a batch.
        """
        date_str = booking.request.requested_date.strftime("%A, %B %d")
        num_players = booking.request.num_players

        if result.success:
            time_str = (result.booked_time or booking.request.requested_time).strftime("%I:%M %p")
            details = f"{date_str} at {time_str} for {num_players} players"
            if result.confirmation_number:
                details += f" (Confirmation: {result.confirmation_number})"

            if result.fallback_reason:
                details += f"\n\nNote: {result.fallback_reason}"

            await sms_service.send_booking_confirmation(
                booking.phone_number, details, booking.origin_channel_id
            )
            return

        time_str = booking.request.requested_time.strftime("%I:%M %p")
        booking_details = f"{date_str} at {time_str} for {num_players} players"

        await sms_service.send_booking_failure(
            booking.phone_number,
            result.error_message or "Unknown error",
            result.alternatives,
            booking_details,
            booking.origin_channel_id,
        )

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

    async def execute_bookings_batch(
        self,
        bookings: list[TeeTimeBooking],
        execute_at: datetime | None = None,
    ) -> list[tuple[str, BookingResult]]:
        """
        Execute multiple bookings in a batch for efficiency.

        This method groups bookings by date and uses the provider's batch booking
        method to book all times for each date in a single session. This is much
        faster than booking each time individually because:
        1. Only one login is required per date
        2. The driver session is reused for all bookings on the same date
        3. If execute_at is provided, the system logs in early and waits

        SMS notifications are NOT sent by this method - the caller is responsible
        for sending notifications after all bookings are complete.

        Args:
            bookings: List of bookings to execute
            execute_at: Optional datetime to wait until before starting bookings.
                       If provided, the provider will log in early and wait until
                       this time before refreshing the page and booking.

        Returns:
            List of (booking_id, BookingResult) tuples for each booking
        """
        if not bookings:
            return []

        if not self._reservation_provider:
            results = []
            for booking in bookings:
                booking_id = booking.id or ""
                booking.status = BookingStatus.FAILED
                booking.error_message = "Reservation provider not configured"
                await database_service.update_booking(booking)
                results.append(
                    (
                        booking_id,
                        BookingResult(
                            success=False,
                            error_message="Reservation provider not configured",
                        ),
                    )
                )
            return results

        bookings_by_date: dict[date, list[TeeTimeBooking]] = {}
        for booking in bookings:
            target_date = booking.request.requested_date
            if target_date not in bookings_by_date:
                bookings_by_date[target_date] = []
            bookings_by_date[target_date].append(booking)

        all_results: list[tuple[str, BookingResult]] = []

        for target_date, date_bookings in bookings_by_date.items():
            for booking in date_bookings:
                booking.status = BookingStatus.IN_PROGRESS
                await database_service.update_booking(booking)

            batch_requests = [
                BatchBookingRequest(
                    booking_id=booking.id or "",
                    target_time=booking.request.requested_time,
                    num_players=booking.request.num_players,
                    fallback_window_minutes=booking.request.fallback_window_minutes,
                )
                for booking in date_bookings
            ]

            batch_result = await self._reservation_provider.book_multiple_tee_times(
                target_date=target_date,
                requests=batch_requests,
                execute_at=execute_at,
            )

            booking_map = {b.id: b for b in date_bookings if b.id is not None}
            for item_result in batch_result.results:
                booking_opt = booking_map.get(item_result.booking_id)
                if not booking_opt:
                    continue

                result = item_result.result
                if result.success:
                    booking_opt.status = BookingStatus.SUCCESS
                    booking_opt.actual_booked_time = result.booked_time
                    booking_opt.confirmation_number = result.confirmation_number
                    all_results.append((item_result.booking_id, result))
                else:
                    booking_opt.status = BookingStatus.FAILED
                    booking_opt.error_message = result.error_message
                    all_results.append((item_result.booking_id, result))

                await database_service.update_booking(booking_opt)

        return all_results


booking_service = BookingService()
