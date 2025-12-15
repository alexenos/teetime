"""
Tests for BookingService in app/services/booking_service.py.

These tests verify the core business logic for managing tee time bookings
and SMS conversations.
"""

from datetime import date, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import (
    BookingStatus,
    ConversationState,
    ParsedIntent,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
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


@pytest.fixture
def sample_session() -> UserSession:
    """Create a sample UserSession for testing."""
    return UserSession(phone_number="+15551234567")


@pytest.fixture
def sample_booking(sample_request: TeeTimeRequest) -> TeeTimeBooking:
    """Create a sample TeeTimeBooking for testing."""
    return TeeTimeBooking(
        id="test1234",
        phone_number="+15551234567",
        request=sample_request,
        status=BookingStatus.SCHEDULED,
        scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
    )


class TestBookingServiceSession:
    """Tests for session management methods."""

    @pytest.mark.asyncio
    async def test_get_session_creates_new(self, booking_service: BookingService) -> None:
        """Test that get_session creates a new session if none exists."""
        new_session = UserSession(phone_number="+15551234567")
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_or_create_session = AsyncMock(return_value=new_session)
            session = await booking_service.get_session("+15551234567")
            assert session.phone_number == "+15551234567"
            assert session.state == ConversationState.IDLE
            assert session.pending_request is None

    @pytest.mark.asyncio
    async def test_get_session_returns_existing(self, booking_service: BookingService) -> None:
        """Test that get_session returns existing session."""
        existing_session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_DATE,
        )
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_or_create_session = AsyncMock(return_value=existing_session)
            session = await booking_service.get_session("+15551234567")
            assert session.state == ConversationState.AWAITING_DATE

    @pytest.mark.asyncio
    async def test_update_session_updates_timestamp(self, booking_service: BookingService) -> None:
        """Test that update_session updates the last_interaction timestamp."""
        session = UserSession(phone_number="+15551234567")
        old_time = session.last_interaction

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.update_session = AsyncMock(return_value=session)
            await booking_service.update_session(session)
            assert session.last_interaction >= old_time


class TestBookingServiceBookings:
    """Tests for booking CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_booking(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test creating a new booking."""
        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)
            booking = await booking_service.create_booking("+15551234567", sample_request)

            assert booking.id is not None
            assert len(booking.id) == 8
            assert booking.phone_number == "+15551234567"
            assert booking.request == sample_request
            assert booking.status == BookingStatus.SCHEDULED
            assert booking.scheduled_execution_time is not None

    @pytest.mark.asyncio
    async def test_get_booking_exists(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test getting an existing booking."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)
            retrieved = await booking_service.get_booking(sample_booking.id)
            assert retrieved is not None
            assert retrieved.id == sample_booking.id

    @pytest.mark.asyncio
    async def test_get_booking_not_exists(self, booking_service: BookingService) -> None:
        """Test getting a non-existent booking."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=None)
            result = await booking_service.get_booking("nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_get_bookings_all(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test getting all bookings."""
        booking2 = TeeTimeBooking(
            id="test5678",
            phone_number="+15559876543",
            request=sample_booking.request,
            status=BookingStatus.SCHEDULED,
        )
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking, booking2])
            bookings = await booking_service.get_bookings()
            assert len(bookings) == 2

    @pytest.mark.asyncio
    async def test_get_bookings_by_phone(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test filtering bookings by phone number."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            bookings = await booking_service.get_bookings(phone_number="+15551234567")
            assert len(bookings) == 1
            assert bookings[0].phone_number == "+15551234567"

    @pytest.mark.asyncio
    async def test_get_bookings_by_status(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test filtering bookings by status."""
        cancelled_booking = TeeTimeBooking(
            id="test5678",
            phone_number="+15559876543",
            request=sample_booking.request,
            status=BookingStatus.CANCELLED,
        )
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(
                side_effect=lambda **kwargs: [sample_booking]
                if kwargs.get("status") == BookingStatus.SCHEDULED
                else [cancelled_booking]
            )
            scheduled = await booking_service.get_bookings(status=BookingStatus.SCHEDULED)
            cancelled = await booking_service.get_bookings(status=BookingStatus.CANCELLED)

            assert len(scheduled) == 1
            assert len(cancelled) == 1

    @pytest.mark.asyncio
    async def test_cancel_booking_success(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test cancelling a scheduled booking."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)
            result = await booking_service.cancel_booking(sample_booking.id)
            assert result is not None
            assert result.status == BookingStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_booking_not_exists(self, booking_service: BookingService) -> None:
        """Test cancelling a non-existent booking."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=None)
            result = await booking_service.cancel_booking("nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_cancel_booking_already_completed(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that completed bookings cannot be cancelled."""
        completed_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SUCCESS,
        )
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=completed_booking)
            result = await booking_service.cancel_booking(completed_booking.id)
            assert result is None

    @pytest.mark.asyncio
    async def test_get_pending_bookings(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test getting pending bookings."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            pending = await booking_service.get_pending_bookings()
            assert len(pending) == 1
            assert pending[0].id == sample_booking.id


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
    async def test_handle_book_intent_complete(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a complete book intent."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=request,
        )

        response = await booking_service._handle_book_intent(sample_session, parsed)

        assert "Saturday, December 20" in response
        assert "08:00 AM" in response
        assert "4 players" in response
        assert sample_session.state == ConversationState.AWAITING_CONFIRMATION
        assert sample_session.pending_request == request

    @pytest.mark.asyncio
    async def test_handle_book_intent_missing_request(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a book intent without request details."""
        parsed = ParsedIntent(
            intent="book",
            clarification_needed="What date would you like?",
        )

        response = await booking_service._handle_book_intent(sample_session, parsed)
        assert response == "What date would you like?"

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_success(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test handling a confirm intent with pending request."""
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=sample_request,
        )

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)
            response = await booking_service._handle_confirm_intent(session)

            assert "scheduled" in response.lower() or "received" in response.lower()
            assert session.state == ConversationState.IDLE
            assert session.pending_request is None

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_nothing_pending(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a confirm intent with nothing pending."""
        response = await booking_service._handle_confirm_intent(sample_session)
        assert "nothing to confirm" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_status_intent_no_bookings(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a status intent with no bookings."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[])
            response = await booking_service._handle_status_intent(sample_session)
            assert "don't have any" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_status_intent_with_bookings(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_booking: TeeTimeBooking,
    ) -> None:
        """Test handling a status intent with existing bookings."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            response = await booking_service._handle_status_intent(sample_session)
            assert "upcoming bookings" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_no_bookings(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a cancel intent with no bookings."""
        parsed = ParsedIntent(intent="cancel")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[])
            response = await booking_service._handle_cancel_intent(sample_session, parsed)
            assert "don't have any bookings to cancel" in response.lower()

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_single_booking(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_booking: TeeTimeBooking,
    ) -> None:
        """Test handling a cancel intent with one booking."""
        parsed = ParsedIntent(intent="cancel")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)
            response = await booking_service._handle_cancel_intent(sample_session, parsed)
            assert "cancelled" in response.lower()


class TestBookingServiceProcessIntent:
    """Tests for the _process_intent router method."""

    @pytest.mark.asyncio
    async def test_process_intent_book(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test routing book intent."""
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
        )

        response = await booking_service._process_intent(sample_session, parsed)
        assert "confirm" in response.lower()

    @pytest.mark.asyncio
    async def test_process_intent_status(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test routing status intent."""
        parsed = ParsedIntent(intent="status")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[])
            response = await booking_service._process_intent(sample_session, parsed)
            assert "don't have any" in response.lower() or "bookings" in response.lower()

    @pytest.mark.asyncio
    async def test_process_intent_help(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test routing help intent."""
        parsed = ParsedIntent(intent="help", response_message="Here's how to use me!")

        response = await booking_service._process_intent(sample_session, parsed)
        assert response == "Here's how to use me!"

    @pytest.mark.asyncio
    async def test_process_intent_unclear(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test routing unclear intent."""
        parsed = ParsedIntent(intent="unclear")

        response = await booking_service._process_intent(sample_session, parsed)
        assert "not sure" in response.lower()


class TestBookingServiceExecuteBooking:
    """Tests for the execute_booking method."""

    @pytest.mark.asyncio
    async def test_execute_booking_no_provider(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test executing a booking without a provider configured."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                result = await booking_service.execute_booking(sample_booking.id)

            assert result is False
            assert sample_booking.status == BookingStatus.FAILED

    @pytest.mark.asyncio
    async def test_execute_booking_not_found(self, booking_service: BookingService) -> None:
        """Test executing a non-existent booking."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=None)
            result = await booking_service.execute_booking("nonexistent")
            assert result is False

    @pytest.mark.asyncio
    async def test_execute_booking_success(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test successful booking execution."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

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
                result = await booking_service.execute_booking(sample_booking.id)

            assert result is True
            assert sample_booking.status == BookingStatus.SUCCESS
            assert sample_booking.confirmation_number == "CONF123"

    @pytest.mark.asyncio
    async def test_execute_booking_failure(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test failed booking execution."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

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
                result = await booking_service.execute_booking(sample_booking.id)

            assert result is False
            assert sample_booking.status == BookingStatus.FAILED
            assert sample_booking.error_message == "Time slot not available"

    @pytest.mark.asyncio
    async def test_execute_booking_exception(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test booking execution with exception."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

            mock_provider = MagicMock()
            mock_provider.book_tee_time = AsyncMock(side_effect=Exception("Network error"))
            booking_service.set_reservation_provider(mock_provider)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                result = await booking_service.execute_booking(sample_booking.id)

            assert result is False
            assert sample_booking.status == BookingStatus.FAILED
            assert "Network error" in sample_booking.error_message


class TestBookingServiceIncomingMessage:
    """Tests for handle_incoming_message method."""

    @pytest.mark.asyncio
    async def test_handle_incoming_message(self, booking_service: BookingService) -> None:
        """Test handling an incoming SMS message."""
        session = UserSession(phone_number="+15551234567")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_or_create_session = AsyncMock(return_value=session)
            mock_db.update_session = AsyncMock(return_value=session)

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
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_DATE,
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_or_create_session = AsyncMock(return_value=session)
            mock_db.update_session = AsyncMock(return_value=session)

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
