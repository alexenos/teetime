"""
Tests for BookingService in app/services/booking_service.py.

These tests verify the core business logic for managing tee time bookings
and SMS conversations.
"""

from datetime import date, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import (
    BookingStatus,
    ConversationState,
    ParsedIntent,
    TeeTimeRequest,
)
from app.providers.base import BookingResult
from app.services.booking_service import BookingService


@pytest.fixture
def booking_service() -> BookingService:
    """Create a fresh BookingService instance for each test."""
    return BookingService()


@pytest.fixture
def sample_request() -> TeeTimeRequest:
    """Create a sample TeeTimeRequest for testing."""
    return TeeTimeRequest(
        requested_date=date(2025, 12, 20),
        requested_time=time(8, 0),
        num_players=4,
        fallback_window_minutes=30,
    )


class TestBookingServiceSession:
    """Tests for session management methods."""

    def test_get_session_creates_new(self, booking_service: BookingService) -> None:
        """Test that get_session creates a new session if none exists."""
        session = booking_service.get_session("+15551234567")
        assert session.phone_number == "+15551234567"
        assert session.state == ConversationState.IDLE
        assert session.pending_request is None

    def test_get_session_returns_existing(self, booking_service: BookingService) -> None:
        """Test that get_session returns existing session."""
        session1 = booking_service.get_session("+15551234567")
        session1.state = ConversationState.AWAITING_DATE
        booking_service.update_session(session1)

        session2 = booking_service.get_session("+15551234567")
        assert session2.state == ConversationState.AWAITING_DATE

    def test_update_session_updates_timestamp(self, booking_service: BookingService) -> None:
        """Test that update_session updates the last_interaction timestamp."""
        session = booking_service.get_session("+15551234567")
        old_time = session.last_interaction

        booking_service.update_session(session)
        assert session.last_interaction >= old_time


class TestBookingServiceBookings:
    """Tests for booking CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_booking(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test creating a new booking."""
        booking = await booking_service.create_booking("+15551234567", sample_request)

        assert booking.id is not None
        assert len(booking.id) == 8
        assert booking.phone_number == "+15551234567"
        assert booking.request == sample_request
        assert booking.status == BookingStatus.SCHEDULED
        assert booking.scheduled_execution_time is not None

    def test_get_booking_exists(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test getting an existing booking."""
        import asyncio

        booking = asyncio.get_event_loop().run_until_complete(
            booking_service.create_booking("+15551234567", sample_request)
        )

        retrieved = booking_service.get_booking(booking.id)
        assert retrieved is not None
        assert retrieved.id == booking.id

    def test_get_booking_not_exists(self, booking_service: BookingService) -> None:
        """Test getting a non-existent booking."""
        result = booking_service.get_booking("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_bookings_all(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test getting all bookings."""
        await booking_service.create_booking("+15551234567", sample_request)
        await booking_service.create_booking("+15559876543", sample_request)

        bookings = booking_service.get_bookings()
        assert len(bookings) == 2

    @pytest.mark.asyncio
    async def test_get_bookings_by_phone(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by phone number."""
        await booking_service.create_booking("+15551234567", sample_request)
        await booking_service.create_booking("+15559876543", sample_request)

        bookings = booking_service.get_bookings(phone_number="+15551234567")
        assert len(bookings) == 1
        assert bookings[0].phone_number == "+15551234567"

    @pytest.mark.asyncio
    async def test_get_bookings_by_status(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by status."""
        booking1 = await booking_service.create_booking("+15551234567", sample_request)
        await booking_service.create_booking("+15559876543", sample_request)

        booking_service.cancel_booking(booking1.id)

        scheduled = booking_service.get_bookings(status=BookingStatus.SCHEDULED)
        cancelled = booking_service.get_bookings(status=BookingStatus.CANCELLED)

        assert len(scheduled) == 1
        assert len(cancelled) == 1

    @pytest.mark.asyncio
    async def test_cancel_booking_success(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test cancelling a scheduled booking."""
        booking = await booking_service.create_booking("+15551234567", sample_request)

        result = booking_service.cancel_booking(booking.id)
        assert result is not None
        assert result.status == BookingStatus.CANCELLED

    def test_cancel_booking_not_exists(self, booking_service: BookingService) -> None:
        """Test cancelling a non-existent booking."""
        result = booking_service.cancel_booking("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_booking_already_completed(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that completed bookings cannot be cancelled."""
        booking = await booking_service.create_booking("+15551234567", sample_request)
        booking.status = BookingStatus.SUCCESS

        result = booking_service.cancel_booking(booking.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_pending_bookings(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test getting pending bookings."""
        booking1 = await booking_service.create_booking("+15551234567", sample_request)
        booking2 = await booking_service.create_booking("+15559876543", sample_request)

        booking_service.cancel_booking(booking1.id)

        pending = booking_service.get_pending_bookings()
        assert len(pending) == 1
        assert pending[0].id == booking2.id


class TestBookingServiceExecutionTime:
    """Tests for execution time calculation."""

    def test_calculate_execution_time(self, booking_service: BookingService) -> None:
        """Test that execution time is calculated correctly."""
        target_date = date(2025, 12, 20)
        exec_time = booking_service._calculate_execution_time(target_date)

        assert exec_time.date() == date(2025, 12, 13)
        assert exec_time.hour == 6
        assert exec_time.minute == 30


class TestBookingServiceIntentHandling:
    """Tests for intent processing methods."""

    @pytest.mark.asyncio
    async def test_handle_book_intent_complete(self, booking_service: BookingService) -> None:
        """Test handling a complete book intent."""
        session = booking_service.get_session("+15551234567")
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=request,
        )

        response = await booking_service._handle_book_intent(session, parsed)

        assert "Saturday, December 20" in response
        assert "08:00 AM" in response
        assert "4 players" in response
        assert session.state == ConversationState.AWAITING_CONFIRMATION
        assert session.pending_request == request

    @pytest.mark.asyncio
    async def test_handle_book_intent_missing_request(
        self, booking_service: BookingService
    ) -> None:
        """Test handling a book intent without request details."""
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(
            intent="book",
            clarification_needed="What date would you like?",
        )

        response = await booking_service._handle_book_intent(session, parsed)
        assert response == "What date would you like?"

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_success(self, booking_service: BookingService) -> None:
        """Test handling a confirm intent with pending request."""
        session = booking_service.get_session("+15551234567")
        session.state = ConversationState.AWAITING_CONFIRMATION
        session.pending_request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )

        response = await booking_service._handle_confirm_intent(session)

        assert "scheduled" in response.lower() or "received" in response.lower()
        assert session.state == ConversationState.IDLE
        assert session.pending_request is None

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_nothing_pending(
        self, booking_service: BookingService
    ) -> None:
        """Test handling a confirm intent with nothing pending."""
        session = booking_service.get_session("+15551234567")

        response = await booking_service._handle_confirm_intent(session)
        assert "nothing to confirm" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_status_intent_no_bookings(self, booking_service: BookingService) -> None:
        """Test handling a status intent with no bookings."""
        session = booking_service.get_session("+15551234567")

        response = await booking_service._handle_status_intent(session)
        assert "don't have any" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_status_intent_with_bookings(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test handling a status intent with existing bookings."""
        await booking_service.create_booking("+15551234567", sample_request)
        session = booking_service.get_session("+15551234567")

        response = await booking_service._handle_status_intent(session)
        assert "upcoming bookings" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_no_bookings(self, booking_service: BookingService) -> None:
        """Test handling a cancel intent with no bookings."""
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(intent="cancel")

        response = await booking_service._handle_cancel_intent(session, parsed)
        assert "don't have any bookings to cancel" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_single_booking(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test handling a cancel intent with one booking."""
        await booking_service.create_booking("+15551234567", sample_request)
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(intent="cancel")

        response = await booking_service._handle_cancel_intent(session, parsed)
        assert "cancelled" in response.lower()


class TestBookingServiceProcessIntent:
    """Tests for the _process_intent router method."""

    @pytest.mark.asyncio
    async def test_process_intent_book(self, booking_service: BookingService) -> None:
        """Test routing book intent."""
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
        )

        response = await booking_service._process_intent(session, parsed)
        assert "confirm" in response.lower()

    @pytest.mark.asyncio
    async def test_process_intent_status(self, booking_service: BookingService) -> None:
        """Test routing status intent."""
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(intent="status")

        response = await booking_service._process_intent(session, parsed)
        assert "don't have any" in response.lower() or "bookings" in response.lower()

    @pytest.mark.asyncio
    async def test_process_intent_help(self, booking_service: BookingService) -> None:
        """Test routing help intent."""
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(intent="help", response_message="Here's how to use me!")

        response = await booking_service._process_intent(session, parsed)
        assert response == "Here's how to use me!"

    @pytest.mark.asyncio
    async def test_process_intent_unclear(self, booking_service: BookingService) -> None:
        """Test routing unclear intent."""
        session = booking_service.get_session("+15551234567")
        parsed = ParsedIntent(intent="unclear")

        response = await booking_service._process_intent(session, parsed)
        assert "not sure" in response.lower()


class TestBookingServiceExecuteBooking:
    """Tests for the execute_booking method."""

    @pytest.mark.asyncio
    async def test_execute_booking_no_provider(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test executing a booking without a provider configured."""
        booking = await booking_service.create_booking("+15551234567", sample_request)

        with patch("app.services.booking_service.sms_service") as mock_sms:
            mock_sms.send_booking_failure = AsyncMock()
            result = await booking_service.execute_booking(booking.id)

        assert result is False
        assert booking.status == BookingStatus.FAILED

    @pytest.mark.asyncio
    async def test_execute_booking_not_found(self, booking_service: BookingService) -> None:
        """Test executing a non-existent booking."""
        result = await booking_service.execute_booking("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_booking_success(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test successful booking execution."""
        booking = await booking_service.create_booking("+15551234567", sample_request)

        mock_provider = MagicMock()
        mock_provider.book_tee_time = AsyncMock(
            return_value=BookingResult(
                success=True,
                booked_time=time(8, 0),
                confirmation_number="CONF123",
            )
        )
        booking_service.set_reservation_provider(mock_provider)

        with patch("app.services.booking_service.sms_service") as mock_sms:
            mock_sms.send_booking_confirmation = AsyncMock()
            result = await booking_service.execute_booking(booking.id)

        assert result is True
        assert booking.status == BookingStatus.SUCCESS
        assert booking.confirmation_number == "CONF123"

    @pytest.mark.asyncio
    async def test_execute_booking_failure(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test failed booking execution."""
        booking = await booking_service.create_booking("+15551234567", sample_request)

        mock_provider = MagicMock()
        mock_provider.book_tee_time = AsyncMock(
            return_value=BookingResult(
                success=False,
                error_message="Time slot not available",
            )
        )
        booking_service.set_reservation_provider(mock_provider)

        with patch("app.services.booking_service.sms_service") as mock_sms:
            mock_sms.send_booking_failure = AsyncMock()
            result = await booking_service.execute_booking(booking.id)

        assert result is False
        assert booking.status == BookingStatus.FAILED
        assert booking.error_message == "Time slot not available"

    @pytest.mark.asyncio
    async def test_execute_booking_exception(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test booking execution with exception."""
        booking = await booking_service.create_booking("+15551234567", sample_request)

        mock_provider = MagicMock()
        mock_provider.book_tee_time = AsyncMock(side_effect=Exception("Network error"))
        booking_service.set_reservation_provider(mock_provider)

        with patch("app.services.booking_service.sms_service") as mock_sms:
            mock_sms.send_booking_failure = AsyncMock()
            result = await booking_service.execute_booking(booking.id)

        assert result is False
        assert booking.status == BookingStatus.FAILED
        assert "Network error" in booking.error_message


class TestBookingServiceIncomingMessage:
    """Tests for handle_incoming_message method."""

    @pytest.mark.asyncio
    async def test_handle_incoming_message(self, booking_service: BookingService) -> None:
        """Test handling an incoming SMS message."""
        with patch("app.services.booking_service.gemini_service") as mock_gemini:
            mock_gemini.parse_message = AsyncMock(
                return_value=ParsedIntent(
                    intent="help",
                    response_message="I can help you book tee times!",
                )
            )

            response = await booking_service.handle_incoming_message("+15551234567", "help")

        assert response == "I can help you book tee times!"

    @pytest.mark.asyncio
    async def test_handle_incoming_message_with_context(
        self, booking_service: BookingService
    ) -> None:
        """Test that context is passed when session has state."""
        session = booking_service.get_session("+15551234567")
        session.state = ConversationState.AWAITING_DATE
        booking_service.update_session(session)

        with patch("app.services.booking_service.gemini_service") as mock_gemini:
            mock_gemini.parse_message = AsyncMock(
                return_value=ParsedIntent(
                    intent="unclear",
                    response_message="Please provide a date.",
                )
            )

            await booking_service.handle_incoming_message("+15551234567", "tomorrow")

            call_args = mock_gemini.parse_message.call_args
            assert call_args[0][1] is not None
            assert "awaiting_date" in call_args[0][1].lower()


class TestBookingServiceHelpMessage:
    """Tests for the help message."""

    def test_get_help_message(self, booking_service: BookingService) -> None:
        """Test that help message contains useful information."""
        help_msg = booking_service._get_help_message()

        assert "Northgate" in help_msg
        assert "Book" in help_msg or "book" in help_msg
        assert "6:30" in help_msg or "6:30am" in help_msg.lower()
