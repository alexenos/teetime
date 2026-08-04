"""
Tests for BookingService in app/services/booking_service.py.

These tests verify the core business logic for managing tee time bookings
and SMS conversations.
"""

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
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
from app.utils.timezone import CTDateTime


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
    async def test_create_booking(self, booking_service: BookingService) -> None:
        """Test creating a new booking with a future execution time."""
        import pytz

        future_request = TeeTimeRequest(
            requested_date=date(2025, 12, 30),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch.object(CTDateTime, "now") as mock_ct_now:
                tz = pytz.timezone("America/Chicago")
                mock_now_value = datetime(2025, 12, 22, 10, 0)
                mock_ct_now.return_value = tz.localize(mock_now_value)

                booking = await booking_service.create_booking("+15551234567", future_request)

            assert booking.id is not None
            assert len(booking.id) == 8
            assert booking.phone_number == "+15551234567"
            assert booking.request == future_request
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
    async def test_cancel_booking_failed_status(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that FAILED bookings cannot be cancelled."""
        failed_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.FAILED,
        )
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=failed_booking)
            result = await booking_service.cancel_booking(failed_booking.id)
            assert result is None

    @pytest.mark.asyncio
    async def test_cancel_booking_success_status(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that cancelling a SUCCESS booking calls _cancel_confirmed_booking and updates status."""
        success_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            actual_booked_time=time(8, 0),
        )
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=success_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

            mock_provider = MagicMock()
            mock_provider.cancel_booking = AsyncMock(return_value=True)
            booking_service.set_reservation_provider(mock_provider)

            result = await booking_service.cancel_booking(success_booking.id)

            mock_provider.cancel_booking.assert_called_once()
            assert result is not None
            assert result.status == BookingStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_booking_success_provider_fails(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that when provider returns False, the service returns None and status is unchanged."""
        success_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            actual_booked_time=time(8, 0),
        )
        original_status = success_booking.status

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=success_booking)

            mock_provider = MagicMock()
            mock_provider.cancel_booking = AsyncMock(return_value=False)
            booking_service.set_reservation_provider(mock_provider)

            result = await booking_service.cancel_booking(success_booking.id)

            mock_provider.cancel_booking.assert_called_once()
            assert result is None
            assert success_booking.status == original_status

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


class TestBookingServiceImmediateExecution:
    """Tests for immediate booking execution when execution time is in the past."""

    @pytest.mark.asyncio
    async def test_create_booking_executes_immediately_when_past(
        self, booking_service: BookingService
    ) -> None:
        """Test that booking is executed immediately when execution time is in the past."""
        import pytz

        # Request for Dec 29, 2025 at 8:00 AM CT
        # Execution time is Dec 22 at 6:30 AM CT (7 days before)
        # If "now" is Dec 22 at 10:00 AM CT, execution time is in the past
        # Tee time is Dec 29 at 8:00 AM CT, which is 7 days away (well over 48 hours)
        past_request = TeeTimeRequest(
            requested_date=date(2025, 12, 29),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )

        created_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=past_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 22, 6, 30),
        )

        executed_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=past_request,
            status=BookingStatus.SUCCESS,
            scheduled_execution_time=datetime(2025, 12, 22, 6, 30),
            actual_booked_time=time(8, 0),
            confirmation_number="CONF123",
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.create_booking = AsyncMock(return_value=created_booking)
            mock_db.get_booking = AsyncMock(return_value=executed_booking)

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

                with patch.object(CTDateTime, "now") as mock_ct_now:
                    tz = pytz.timezone("America/Chicago")
                    # Now is Dec 22 at 10:00 AM CT, execution time (6:30 AM) is in the past
                    # Tee time is Dec 29 at 8:00 AM CT, which is ~7 days away (over 48 hours)
                    mock_now_value = datetime(2025, 12, 22, 10, 0)
                    mock_ct_now.return_value = tz.localize(mock_now_value)

                    result = await booking_service.create_booking("+15551234567", past_request)

                    # create_booking returns as soon as the record exists so the
                    # caller can acknowledge; the attempt runs in the background.
                    assert result.status == BookingStatus.IN_PROGRESS
                    await booking_service.wait_for_background_bookings()

            mock_provider.book_tee_time.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_booking_schedules_when_future(
        self, booking_service: BookingService
    ) -> None:
        """Test that booking is scheduled (not executed) when execution time is in the future."""
        import pytz

        future_request = TeeTimeRequest(
            requested_date=date(2025, 12, 29),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            mock_provider = MagicMock()
            mock_provider.book_tee_time = AsyncMock()
            booking_service.set_reservation_provider(mock_provider)

            with patch.object(CTDateTime, "now") as mock_ct_now:
                tz = pytz.timezone("America/Chicago")
                mock_now_value = datetime(2025, 12, 20, 10, 0)
                mock_ct_now.return_value = tz.localize(mock_now_value)

                result = await booking_service.create_booking("+15551234567", future_request)

            mock_provider.book_tee_time.assert_not_called()
            assert result.status == BookingStatus.SCHEDULED

    @pytest.mark.asyncio
    async def test_create_booking_executes_immediately_when_exactly_now(
        self, booking_service: BookingService
    ) -> None:
        """Test that booking is executed immediately when execution time equals current time."""
        import pytz

        request = TeeTimeRequest(
            requested_date=date(2025, 12, 29),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )

        created_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 22, 6, 30),
        )

        executed_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.SUCCESS,
            scheduled_execution_time=datetime(2025, 12, 22, 6, 30),
            actual_booked_time=time(8, 0),
            confirmation_number="CONF123",
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.create_booking = AsyncMock(return_value=created_booking)
            mock_db.get_booking = AsyncMock(return_value=executed_booking)

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

                with patch.object(CTDateTime, "now") as mock_ct_now:
                    tz = pytz.timezone("America/Chicago")
                    mock_now_value = datetime(2025, 12, 22, 6, 30)
                    mock_ct_now.return_value = tz.localize(mock_now_value)

                    result = await booking_service.create_booking("+15551234567", request)

                    assert result.status == BookingStatus.IN_PROGRESS
                    await booking_service.wait_for_background_bookings()

            mock_provider.book_tee_time.assert_called_once()


class TestBookingServiceConfirmIntentImmediateExecution:
    """Tests for _handle_confirm_intent with immediate execution."""

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_immediate_success(
        self, booking_service: BookingService
    ) -> None:
        """Test that confirm intent returns success message when booking succeeds immediately."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 15),
            requested_time=time(8, 0),
            num_players=4,
        )
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=request,
        )

        success_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.SUCCESS,
            actual_booked_time=time(8, 0),
        )

        with patch.object(
            booking_service, "create_booking", new=AsyncMock(return_value=success_booking)
        ):
            response = await booking_service._handle_confirm_intent(session)

        assert "confirmed" in response.lower()
        assert "reserved" in response.lower()
        assert session.state == ConversationState.IDLE
        assert session.pending_request is None

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_immediate_failure(
        self, booking_service: BookingService
    ) -> None:
        """Test that confirm intent returns failure message when booking fails immediately."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 15),
            requested_time=time(8, 0),
            num_players=4,
        )
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=request,
        )

        failed_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.FAILED,
            error_message="Time slot not available",
        )

        with patch.object(
            booking_service, "create_booking", new=AsyncMock(return_value=failed_booking)
        ):
            response = await booking_service._handle_confirm_intent(session)

        assert "failed" in response.lower()
        assert session.state == ConversationState.IDLE
        assert session.pending_request is None

    @pytest.mark.asyncio
    async def test_handle_confirm_intent_scheduled(self, booking_service: BookingService) -> None:
        """Test that confirm intent returns scheduled message when booking is scheduled for future."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 29),
            requested_time=time(8, 0),
            num_players=4,
        )
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=request,
        )

        scheduled_booking = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 22, 6, 30),
        )

        with patch.object(
            booking_service, "create_booking", new=AsyncMock(return_value=scheduled_booking)
        ):
            response = await booking_service._handle_confirm_intent(session)

        assert "scheduled" in response.lower()
        assert "booking window opens" in response.lower()
        assert session.state == ConversationState.IDLE
        assert session.pending_request is None


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
    async def test_handle_confirm_intent_success(self, booking_service: BookingService) -> None:
        """Test handling a confirm intent with pending request (future execution time)."""
        import pytz

        future_request = TeeTimeRequest(
            requested_date=date(2025, 12, 30),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=future_request,
        )

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch.object(CTDateTime, "now") as mock_ct_now:
                tz = pytz.timezone("America/Chicago")
                mock_now_value = datetime(2025, 12, 22, 10, 0)
                mock_ct_now.return_value = tz.localize(mock_now_value)

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
        import pytz

        tz = pytz.timezone("America/Chicago")
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            with patch.object(CTDateTime, "now") as mock_ct_now:
                # sample_booking is for Dec 20; anchor "today" before it so the
                # booking actually counts as upcoming rather than being filtered
                # out (which would let the empty-state message satisfy the
                # assertion by coincidence).
                mock_ct_now.return_value = tz.localize(datetime(2025, 12, 13, 8, 0))
                response = await booking_service._handle_status_intent(sample_session)

        assert "upcoming bookings" in response.lower()
        assert "Saturday, December 20" in response
        assert "scheduled" in response

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
        """Test handling a cancel intent with one booking asks for confirmation."""
        parsed = ParsedIntent(intent="cancel")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            mock_db.update_session = AsyncMock()

            response = await booking_service._handle_cancel_intent(sample_session, parsed)
            # Now asks for confirmation instead of immediately cancelling
            assert "are you sure" in response.lower()
            assert sample_session.pending_cancellation_id == sample_booking.id

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_confirm_cancellation(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_booking: TeeTimeBooking,
    ) -> None:
        """Test confirming a pending cancellation."""
        # Set up session with pending cancellation
        sample_session.pending_cancellation_id = sample_booking.id
        parsed = ParsedIntent(intent="cancel", raw_message="yes")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            mock_db.get_booking = AsyncMock(return_value=sample_booking)
            mock_db.update_session = AsyncMock()

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)
            response = await booking_service._handle_cancel_intent(sample_session, parsed)
            assert "cancelled" in response.lower()
            assert sample_session.pending_cancellation_id is None

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_decline_cancellation(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_booking: TeeTimeBooking,
    ) -> None:
        """Test that responding 'no' to confirmation clears pending_cancellation_id."""
        sample_session.pending_cancellation_id = sample_booking.id
        parsed = ParsedIntent(intent="cancel", raw_message="no")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[sample_booking])
            mock_db.update_session = AsyncMock()

            response = await booking_service._handle_cancel_intent(sample_session, parsed)

            assert "remains active" in response.lower()
            assert sample_session.pending_cancellation_id is None

    @pytest.mark.asyncio
    async def test_handle_cancel_intent_multiple_bookings(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_request: TeeTimeRequest,
    ) -> None:
        """Test that when user has >1 cancellable booking, response lists them."""
        booking1 = TeeTimeBooking(
            id="booking1",
            phone_number=sample_session.phone_number,
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="booking2",
            phone_number=sample_session.phone_number,
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                num_players=2,
            ),
            status=BookingStatus.SUCCESS,
        )
        parsed = ParsedIntent(intent="cancel")

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[booking1, booking2])

            response = await booking_service._handle_cancel_intent(sample_session, parsed)

            assert "which booking" in response.lower()
            assert "Saturday, December 20" in response
            assert "Sunday, December 21" in response
            # Verify session state is set to AWAITING_CANCELLATION_SELECTION
            assert sample_session.state == ConversationState.AWAITING_CANCELLATION_SELECTION

    @pytest.mark.asyncio
    async def test_handle_cancellation_selection_matches_booking(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_request: TeeTimeRequest,
    ) -> None:
        """Test that when user replies with date during cancellation selection, it matches the booking."""
        # Set up session in AWAITING_CANCELLATION_SELECTION state
        sample_session.state = ConversationState.AWAITING_CANCELLATION_SELECTION

        booking1 = TeeTimeBooking(
            id="booking1",
            phone_number=sample_session.phone_number,
            request=sample_request,  # Dec 20 at 8:00 AM
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="booking2",
            phone_number=sample_session.phone_number,
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                num_players=2,
            ),
            status=BookingStatus.SUCCESS,
        )

        # Simulate user replying with "12/20" - Gemini parses this as a booking request
        # but since we're in AWAITING_CANCELLATION_SELECTION state, it should be handled
        # as a cancellation selection instead
        parsed = ParsedIntent(
            intent="book",  # Gemini might parse date as booking intent
            tee_time_request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=4,
            ),
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[booking1, booking2])

            response = await booking_service._process_intent(sample_session, parsed)

            # Should ask for confirmation to cancel, NOT create a new booking
            assert "cancel" in response.lower()
            assert "confirm" in response.lower() or "yes" in response.lower()
            assert "Saturday, December 20" in response
            # Session should have pending_cancellation_id set
            assert sample_session.pending_cancellation_id == "booking1"
            # Session state should be reset to IDLE
            assert sample_session.state == ConversationState.IDLE

    @pytest.mark.asyncio
    async def test_cancellation_selection_by_number(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_request: TeeTimeRequest,
    ) -> None:
        """Test that user can select a booking to cancel by number."""
        sample_session.state = ConversationState.AWAITING_CANCELLATION_SELECTION

        booking1 = TeeTimeBooking(
            id="booking1",
            phone_number=sample_session.phone_number,
            request=sample_request,  # Dec 20 at 8:00 AM
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="booking2",
            phone_number=sample_session.phone_number,
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                num_players=2,
            ),
            status=BookingStatus.SUCCESS,
        )

        # User replies with "2" to select the second booking
        parsed = ParsedIntent(
            intent="unclear",
            raw_message="2",
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[booking1, booking2])

            response = await booking_service._process_intent(sample_session, parsed)

            # Should ask for confirmation to cancel booking2
            assert "cancel" in response.lower()
            assert "Sunday, December 21" in response
            assert sample_session.pending_cancellation_id == "booking2"
            assert sample_session.state == ConversationState.IDLE

    @pytest.mark.asyncio
    async def test_cancellation_selection_no_match_keeps_state(
        self,
        booking_service: BookingService,
        sample_session: UserSession,
        sample_request: TeeTimeRequest,
    ) -> None:
        """Test that when user's date doesn't match any booking, state is kept for retry."""
        sample_session.state = ConversationState.AWAITING_CANCELLATION_SELECTION

        booking1 = TeeTimeBooking(
            id="booking1",
            phone_number=sample_session.phone_number,
            request=sample_request,  # Dec 20
            status=BookingStatus.SCHEDULED,
        )

        # User replies with a date that doesn't match any booking
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=TeeTimeRequest(
                requested_date=date(2025, 12, 25),  # No booking on this date
                requested_time=time(8, 0),
                num_players=4,
            ),
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[booking1])

            response = await booking_service._process_intent(sample_session, parsed)

            # Should indicate no match found and ask for number
            assert "couldn't match" in response.lower()
            assert "number" in response.lower()
            # State should remain AWAITING_CANCELLATION_SELECTION so user can try again
            assert sample_session.state == ConversationState.AWAITING_CANCELLATION_SELECTION


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
    async def test_execute_booking_failure_includes_booking_details_in_sms(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test that booking failure SMS includes specific booking details."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

            mock_provider = MagicMock()
            mock_provider.book_tee_time = AsyncMock(
                return_value=BookingResult(
                    success=False,
                    error_message="No time slots available",
                )
            )
            booking_service.set_reservation_provider(mock_provider)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                await booking_service.execute_booking(sample_booking.id)

                # Verify send_booking_failure was called with booking_details
                mock_sms.send_booking_failure.assert_called_once()
                call_kwargs = mock_sms.send_booking_failure.call_args
                # Check that booking_details was passed (positional arg 3 or kwarg)
                args, kwargs = call_kwargs
                booking_details = kwargs.get("booking_details") or (
                    args[3] if len(args) > 3 else None
                )
                assert booking_details is not None
                # Verify it contains date, time, and players
                assert "Saturday" in booking_details or "December" in booking_details
                assert "08:00 AM" in booking_details
                assert "4 players" in booking_details

    @pytest.mark.asyncio
    async def test_execute_booking_exception_includes_booking_details_in_sms(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test that booking exception SMS includes specific booking details."""
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
                await booking_service.execute_booking(sample_booking.id)

                # Verify send_booking_failure was called with booking_details
                mock_sms.send_booking_failure.assert_called_once()
                call_kwargs = mock_sms.send_booking_failure.call_args
                args, kwargs = call_kwargs
                booking_details = kwargs.get("booking_details") or (
                    args[3] if len(args) > 3 else None
                )
                assert booking_details is not None
                assert "4 players" in booking_details

    @pytest.mark.asyncio
    async def test_execute_booking_no_provider_includes_booking_details_in_sms(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test that no provider failure SMS includes specific booking details."""
        # Ensure no provider is set
        booking_service._reservation_provider = None

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                await booking_service.execute_booking(sample_booking.id)

                # Verify send_booking_failure was called with booking_details
                mock_sms.send_booking_failure.assert_called_once()
                call_kwargs = mock_sms.send_booking_failure.call_args
                args, kwargs = call_kwargs
                booking_details = kwargs.get("booking_details") or (
                    args[3] if len(args) > 3 else None
                )
                assert booking_details is not None
                assert "4 players" in booking_details

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


class TestBookingServiceGetDueBookings:
    """Tests for get_due_bookings method with timezone handling."""

    @pytest.mark.asyncio
    async def test_get_due_bookings_calls_database_service(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test that get_due_bookings calls database_service.get_due_bookings."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        current_time = tz.localize(datetime(2025, 12, 13, 6, 30))

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_due_bookings = AsyncMock(return_value=[sample_booking])

            result = await booking_service.get_due_bookings(current_time)

            assert len(result) == 1
            assert result[0].id == sample_booking.id
            mock_db.get_due_bookings.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_due_bookings_strips_timezone(self, booking_service: BookingService) -> None:
        """Test that get_due_bookings strips tzinfo for naive DB comparison."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        current_time = tz.localize(datetime(2025, 12, 13, 6, 30))

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_due_bookings = AsyncMock(return_value=[])

            await booking_service.get_due_bookings(current_time)

            call_args = mock_db.get_due_bookings.call_args[0][0]
            assert call_args.tzinfo is None
            assert call_args == datetime(2025, 12, 13, 6, 30)

    @pytest.mark.asyncio
    async def test_get_due_bookings_returns_empty_list(
        self, booking_service: BookingService
    ) -> None:
        """Test that get_due_bookings returns empty list when no bookings are due."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        current_time = tz.localize(datetime(2025, 12, 13, 6, 30))

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_due_bookings = AsyncMock(return_value=[])

            result = await booking_service.get_due_bookings(current_time)

            assert result == []

    @pytest.mark.asyncio
    async def test_get_due_bookings_returns_multiple_bookings(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that get_due_bookings returns multiple due bookings."""
        import pytz

        booking1 = TeeTimeBooking(
            id="booking1",
            phone_number="+15551111111",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        booking2 = TeeTimeBooking(
            id="booking2",
            phone_number="+15552222222",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 29),
        )

        tz = pytz.timezone("America/Chicago")
        current_time = tz.localize(datetime(2025, 12, 13, 6, 30))

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_due_bookings = AsyncMock(return_value=[booking1, booking2])

            result = await booking_service.get_due_bookings(current_time)

            assert len(result) == 2
            result_ids = {b.id for b in result}
            assert "booking1" in result_ids
            assert "booking2" in result_ids


class TestBookingService48HourRestriction:
    """Tests for the 48-hour restriction on multi-player bookings."""

    @pytest.mark.asyncio
    async def test_multi_player_booking_rejected_within_48_hours(
        self, booking_service: BookingService
    ) -> None:
        """Test that multi-player bookings are rejected within 48 hours of tee time."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        # Current time: Dec 23, 2025 at 10:00 AM CT
        mock_now_value = tz.localize(datetime(2025, 12, 23, 10, 0))

        # Request for Dec 24, 2025 at 8:00 AM CT (22 hours away - within 48 hours)
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 24),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )

        with patch.object(CTDateTime, "now") as mock_ct_now:
            mock_ct_now.return_value = mock_now_value

            with pytest.raises(ValueError) as exc_info:
                await booking_service.create_booking("+15551234567", request)

            assert "Multi-player bookings" in str(exc_info.value)
            assert "48 hours" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_single_player_booking_allowed_within_48_hours(
        self, booking_service: BookingService
    ) -> None:
        """Test that single-player bookings are allowed within 48 hours of tee time.

        This verifies the single-player carve-out: multi-player bookings within
        48 hours of tee time are rejected, but single-player bookings are always
        allowed regardless of timing.

        We use a date where execution time is in the future (so booking is scheduled,
        not executed immediately) but verify the 48-hour check doesn't apply to
        single-player requests by using the same timing that would fail for
        multi-player.
        """
        import pytz

        tz = pytz.timezone("America/Chicago")
        # Current time: Dec 16, 2025 at 6:00 AM CT (before execution time)
        # Tee time: Dec 17, 2025 at 8:00 AM CT (26 hours away - within 48 hours)
        # Execution time: Dec 10, 2025 at 6:30 AM CT (past, but we test the check)
        #
        # The 48-hour check happens before execution time calculation, so we need
        # mock_now to be close enough to tee_time to trigger the 48-hour path.
        # But execution time is based on requested_date - 7 days, not now.
        #
        # For Dec 17 tee time: execution = Dec 10 at 6:30 AM
        # If now = Dec 16 at 6:00 AM, execution is in the past → immediate exec
        # So let's use Dec 23 tee time with now = Dec 22 at 10 AM:
        # - Tee time Dec 23 8:00 AM is 22 hours away (within 48h)
        # - Execution Dec 16 6:30 AM is in past → immediate exec
        # - But single player should bypass the 48h check entirely
        #
        # Actually, the simplest test: same timing as multi_player_rejected test
        # but with num_players=1 - should NOT raise.
        mock_now_value = tz.localize(datetime(2025, 12, 23, 10, 0))

        # Request for Dec 24, 2025 at 8:00 AM CT - 22 hours away (within 48 hours)
        # This is the SAME timing that raises ValueError for multi-player
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 24),
            requested_time=time(8, 0),
            num_players=1,  # Single player - should bypass 48-hour restriction
            fallback_window_minutes=30,
        )

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)
            mock_db.get_booking = AsyncMock(return_value=None)  # For execute path
            mock_db.update_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = mock_now_value

                # Should not raise ValueError - single player bypasses 48-hour restriction
                # even though tee time is only 22 hours away (same timing that fails
                # for multi-player in test_multi_player_booking_rejected_within_48_hours)
                booking = await booking_service.create_booking("+15551234567", request)
                assert booking.request.num_players == 1
                # Drain the background attempt before the database patch unwinds.
                await booking_service.wait_for_background_bookings()

    @pytest.mark.asyncio
    async def test_multi_player_booking_allowed_after_48_hours(
        self, booking_service: BookingService
    ) -> None:
        """Test that multi-player bookings are allowed more than 48 hours before tee time."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        # Current time: Dec 16, 2025 at 6:00 AM CT
        # This is before the execution time of Dec 16 at 6:30 AM CT
        mock_now_value = tz.localize(datetime(2025, 12, 16, 6, 0))

        # Request for Dec 23, 2025 at 8:00 AM CT (7+ days away, well over 48 hours)
        # Execution time is Dec 16 at 6:30 AM CT (30 min in future, so scheduled)
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 23),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = mock_now_value

                # Should not raise - more than 48 hours away
                booking = await booking_service.create_booking("+15551234567", request)
                assert booking.request.num_players == 4

    @pytest.mark.asyncio
    async def test_two_player_booking_rejected_within_48_hours(
        self, booking_service: BookingService
    ) -> None:
        """Test that 2-player bookings are also rejected within 48 hours."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        mock_now_value = tz.localize(datetime(2025, 12, 23, 10, 0))

        # Request for Dec 24, 2025 at 8:00 AM CT (within 48 hours)
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 24),
            requested_time=time(8, 0),
            num_players=2,
            fallback_window_minutes=30,
        )

        with patch.object(CTDateTime, "now") as mock_ct_now:
            mock_ct_now.return_value = mock_now_value

            with pytest.raises(ValueError) as exc_info:
                await booking_service.create_booking("+15551234567", request)

            assert "Multi-player bookings" in str(exc_info.value)
            assert "2 players" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_confirm_intent_returns_error_for_48_hour_restriction(
        self, booking_service: BookingService
    ) -> None:
        """Test that confirm intent returns error message for 48-hour restriction."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        mock_now_value = tz.localize(datetime(2025, 12, 23, 10, 0))

        # Session with pending request within 48 hours
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 24),
            requested_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=30,
        )
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=request,
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.update_session = AsyncMock(return_value=session)

            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = mock_now_value

                response = await booking_service._handle_confirm_intent(session)

                assert "Multi-player bookings" in response
                assert "48 hours" in response
                assert session.state == ConversationState.IDLE
                assert session.pending_request is None


class TestMultipleBookings:
    """Tests for multiple booking functionality."""

    @pytest.mark.asyncio
    async def test_handle_book_intent_multiple_requests(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a book intent with multiple tee time requests."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=4,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                num_players=4,
            ),
        ]
        parsed = ParsedIntent(
            intent="book",
            tee_time_requests=requests,
        )

        response = await booking_service._handle_book_intent(sample_session, parsed)

        assert "2 tee times" in response
        assert "Saturday, December 20" in response
        assert "Sunday, December 21" in response
        assert "08:00 AM" in response
        assert "09:00 AM" in response
        assert sample_session.state == ConversationState.AWAITING_CONFIRMATION
        assert sample_session.pending_requests == requests
        assert sample_session.pending_request is None

    @pytest.mark.asyncio
    async def test_handle_confirm_multiple_bookings_success(
        self, booking_service: BookingService
    ) -> None:
        """Test confirming multiple bookings successfully."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 30),
                requested_time=time(8, 0),
                num_players=4,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 31),
                requested_time=time(9, 0),
                num_players=4,
            ),
        ]
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_requests=requests,
        )

        async def create_booking_side_effect(
            phone_number: str,
            request: TeeTimeRequest,
            origin_channel_id: str | None = None,
            defer_execution: bool = False,
        ) -> TeeTimeBooking:
            return TeeTimeBooking(
                id="test123",
                phone_number=phone_number,
                request=request,
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 23, 6, 30),
            )

        with patch.object(
            booking_service, "create_booking", new=AsyncMock(side_effect=create_booking_side_effect)
        ):
            response = await booking_service._handle_confirm_intent(session)

        assert "scheduled" in response.lower()
        assert session.state == ConversationState.IDLE
        assert session.pending_requests is None
        assert session.pending_request is None

    @pytest.mark.asyncio
    async def test_handle_confirm_multiple_bookings_partial_failure(
        self, booking_service: BookingService
    ) -> None:
        """Test confirming multiple bookings where one fails."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 24),
                requested_time=time(8, 0),
                num_players=4,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 30),
                requested_time=time(9, 0),
                num_players=4,
            ),
        ]
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_requests=requests,
        )

        call_count = 0

        async def create_booking_side_effect(
            phone_number: str,
            request: TeeTimeRequest,
            origin_channel_id: str | None = None,
            defer_execution: bool = False,
        ) -> TeeTimeBooking:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("Multi-player bookings within 48 hours")
            return TeeTimeBooking(
                id="test123",
                phone_number=phone_number,
                request=request,
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 23, 6, 30),
            )

        with patch.object(
            booking_service, "create_booking", new=AsyncMock(side_effect=create_booking_side_effect)
        ):
            response = await booking_service._handle_confirm_intent(session)

        assert "Could not schedule" in response
        assert "48 hours" in response
        assert "scheduled" in response.lower()
        assert session.state == ConversationState.IDLE
        assert session.pending_requests is None

    @pytest.mark.asyncio
    async def test_single_booking_in_tee_time_requests_uses_single_field(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test that a single booking in tee_time_requests still uses pending_request."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
            num_players=4,
        )
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=request,
        )

        response = await booking_service._handle_book_intent(sample_session, parsed)

        assert "Saturday, December 20" in response
        assert sample_session.state == ConversationState.AWAITING_CONFIRMATION
        assert sample_session.pending_request == request
        assert sample_session.pending_requests is None

    @pytest.mark.asyncio
    async def test_handle_confirm_all_bookings_fail(self, booking_service: BookingService) -> None:
        """Test confirming multiple bookings where all fail."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 24),
                requested_time=time(8, 0),
                num_players=4,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 25),
                requested_time=time(9, 0),
                num_players=4,
            ),
        ]
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_requests=requests,
        )

        async def create_booking_side_effect(
            phone_number: str,
            request: TeeTimeRequest,
            origin_channel_id: str | None = None,
            defer_execution: bool = False,
        ) -> TeeTimeBooking:
            raise ValueError("Multi-player bookings within 48 hours")

        with patch.object(
            booking_service, "create_booking", new=AsyncMock(side_effect=create_booking_side_effect)
        ):
            response = await booking_service._handle_confirm_intent(session)

        assert "Could not schedule" in response
        assert session.state == ConversationState.IDLE
        assert session.pending_requests is None

    @pytest.mark.asyncio
    async def test_handle_book_intent_three_requests(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling a book intent with three tee time requests."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=4,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                num_players=4,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 22),
                requested_time=time(10, 0),
                num_players=2,
            ),
        ]
        parsed = ParsedIntent(
            intent="book",
            tee_time_requests=requests,
        )

        response = await booking_service._handle_book_intent(sample_session, parsed)

        assert "3 tee times" in response
        assert "Saturday, December 20" in response
        assert "Sunday, December 21" in response
        assert "Monday, December 22" in response
        assert sample_session.state == ConversationState.AWAITING_CONFIRMATION
        assert sample_session.pending_requests == requests

    @pytest.mark.asyncio
    async def test_handle_book_intent_multiple_requests_different_player_counts(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Test handling multiple bookings with different player counts."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=2,
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                num_players=4,
            ),
        ]
        parsed = ParsedIntent(
            intent="book",
            tee_time_requests=requests,
        )

        response = await booking_service._handle_book_intent(sample_session, parsed)

        assert "2 tee times" in response
        assert "2 players" in response
        assert "4 players" in response
        assert sample_session.pending_requests == requests


class TestOriginChannelRouting:
    """A booking's notification must reply where its conversation started.

    The 6:30am result arrives days after the request, so the originating channel
    is captured on the session, copied onto the booking, and handed to the
    messaging provider at notification time.
    """

    @pytest.mark.asyncio
    async def test_incoming_message_records_origin_channel(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """The channel an inbound message arrived in is stored on the session."""
        with (
            patch("app.services.booking_service.database_service") as mock_db,
            patch("app.services.booking_service.gemini_service") as mock_gemini,
        ):
            mock_db.get_or_create_session = AsyncMock(return_value=sample_session)
            mock_db.update_session = AsyncMock()
            mock_gemini.parse_message = AsyncMock(return_value=ParsedIntent(intent="help"))

            await booking_service.handle_incoming_message("+15551234567", "help", "778899")

        assert sample_session.origin_channel_id == "778899"

    @pytest.mark.asyncio
    async def test_incoming_message_without_channel_keeps_existing(
        self, booking_service: BookingService
    ) -> None:
        """An SMS message (no channel) must not erase a recorded Discord origin."""
        session = UserSession(phone_number="+15551234567", origin_channel_id="778899")
        with (
            patch("app.services.booking_service.database_service") as mock_db,
            patch("app.services.booking_service.gemini_service") as mock_gemini,
        ):
            mock_db.get_or_create_session = AsyncMock(return_value=session)
            mock_db.update_session = AsyncMock()
            mock_gemini.parse_message = AsyncMock(return_value=ParsedIntent(intent="help"))

            await booking_service.handle_incoming_message("+15551234567", "help")

        assert session.origin_channel_id == "778899"

    @pytest.mark.asyncio
    async def test_create_booking_stores_origin_channel(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """create_booking persists the channel so it survives until execution."""
        import pytz

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            future_request = TeeTimeRequest(
                requested_date=date(2025, 12, 29),
                requested_time=time(8, 0),
                num_players=4,
            )
            with patch.object(CTDateTime, "now") as mock_ct_now:
                tz = pytz.timezone("America/Chicago")
                mock_ct_now.return_value = tz.localize(datetime(2025, 12, 20, 10, 0))
                result = await booking_service.create_booking(
                    "+15551234567", future_request, "778899"
                )

        assert result.origin_channel_id == "778899"

    @pytest.mark.asyncio
    async def test_confirm_intent_passes_session_origin_to_booking(
        self, booking_service: BookingService, sample_request: TeeTimeRequest
    ) -> None:
        """Confirming a pending request carries the session's channel onto the booking."""
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=sample_request,
            origin_channel_id="778899",
        )

        async def create_booking_side_effect(
            phone_number: str,
            request: TeeTimeRequest,
            origin_channel_id: str | None = None,
            defer_execution: bool = False,
        ) -> TeeTimeBooking:
            return TeeTimeBooking(
                id="test1234",
                phone_number=phone_number,
                request=request,
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
                origin_channel_id=origin_channel_id,
            )

        with patch.object(
            booking_service, "create_booking", side_effect=create_booking_side_effect
        ) as mock_create:
            await booking_service._handle_confirm_intent(session)

        mock_create.assert_awaited_once_with("+15551234567", sample_request, "778899")

    @pytest.mark.asyncio
    async def test_confirm_multiple_bookings_passes_session_origin_to_each(
        self, booking_service: BookingService
    ) -> None:
        """Confirming several requests at once is a separate loop from the single
        booking path; every booking it creates must carry the channel too."""
        requests = [
            TeeTimeRequest(
                requested_date=date(2025, 12, 30), requested_time=time(8, 0), num_players=4
            ),
            TeeTimeRequest(
                requested_date=date(2025, 12, 31), requested_time=time(9, 0), num_players=4
            ),
        ]
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_requests=requests,
            origin_channel_id="778899",
        )

        async def create_booking_side_effect(
            phone_number: str,
            request: TeeTimeRequest,
            origin_channel_id: str | None = None,
            defer_execution: bool = False,
        ) -> TeeTimeBooking:
            return TeeTimeBooking(
                id="test1234",
                phone_number=phone_number,
                request=request,
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 23, 6, 30),
                origin_channel_id=origin_channel_id,
            )

        with patch.object(
            booking_service, "create_booking", side_effect=create_booking_side_effect
        ) as mock_create:
            await booking_service._handle_confirm_multiple_bookings(session)

        assert len(mock_create.await_args_list) == len(requests)
        assert all(call.args[2] == "778899" for call in mock_create.await_args_list)

    @pytest.mark.asyncio
    async def test_execute_booking_success_notifies_origin_channel(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """The confirmation is routed back to the booking's channel, not a DM."""
        sample_booking.origin_channel_id = "778899"
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)
            mock_db.update_booking = AsyncMock(side_effect=lambda b: b)

            mock_provider = MagicMock()
            mock_provider.book_tee_time = AsyncMock(
                return_value=BookingResult(
                    success=True, booked_time=time(8, 0), confirmation_number="CONF123"
                )
            )
            booking_service.set_reservation_provider(mock_provider)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_confirmation = AsyncMock()
                await booking_service.execute_booking(sample_booking.id)

        assert mock_sms.send_booking_confirmation.await_args.args[2] == "778899"

    @pytest.mark.asyncio
    async def test_execute_booking_failure_notifies_origin_channel(
        self, booking_service: BookingService, sample_booking: TeeTimeBooking
    ) -> None:
        """Failures follow the same route as confirmations."""
        sample_booking.origin_channel_id = "778899"
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=sample_booking)
            mock_db.update_booking = AsyncMock(side_effect=lambda b: b)

            mock_provider = MagicMock()
            mock_provider.book_tee_time = AsyncMock(
                return_value=BookingResult(success=False, error_message="No slots")
            )
            booking_service.set_reservation_provider(mock_provider)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                await booking_service.execute_booking(sample_booking.id)

        assert mock_sms.send_booking_failure.await_args.args[4] == "778899"


class TestImmediateMultiBookingRunsAsOneBatch:
    """Several already-open bookings share one browser session.

    Each booking used to spawn its own background task, so confirming two
    ad-hoc bookings drove two headless Chromes onto the club's login form
    ~80ms apart. The portal let one through and left the other sitting on the
    login page, failing that booking outright. They now run as a single batch:
    one driver, one login, attempts made in sequence.
    """

    REQUESTED_DATE = date(2025, 12, 29)
    # 6:30am CT on the 22nd is when the window for the 29th opens, so a "now"
    # later that morning puts both bookings past their execution time.
    NOW_CT = datetime(2025, 12, 22, 10, 0)

    @staticmethod
    def _session() -> UserSession:
        return UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            origin_channel_id="778899",
            pending_requests=[
                TeeTimeRequest(
                    requested_date=TestImmediateMultiBookingRunsAsOneBatch.REQUESTED_DATE,
                    requested_time=time(17, 0),
                    num_players=4,
                ),
                TeeTimeRequest(
                    requested_date=TestImmediateMultiBookingRunsAsOneBatch.REQUESTED_DATE,
                    requested_time=time(17, 8),
                    num_players=4,
                ),
            ],
        )

    @staticmethod
    def _fake_database(mock_db: MagicMock) -> dict[str, TeeTimeBooking]:
        """Back the mocked database_service with a dict keyed by booking id."""
        stored: dict[str, TeeTimeBooking] = {}

        async def save(booking: TeeTimeBooking) -> TeeTimeBooking:
            if booking.id is not None:
                stored[booking.id] = booking
            return booking

        async def load(booking_id: str) -> TeeTimeBooking | None:
            return stored.get(booking_id)

        mock_db.create_booking = AsyncMock(side_effect=save)
        mock_db.update_booking = AsyncMock(side_effect=save)
        mock_db.get_booking = AsyncMock(side_effect=load)
        return stored

    @pytest.mark.asyncio
    async def test_both_bookings_run_in_a_single_batched_attempt(
        self, booking_service: BookingService
    ) -> None:
        """One background task, one batch call carrying both tee times."""
        import pytz

        from app.providers.base import BatchBookingItemResult, BatchBookingResult

        session = self._session()

        async def book_multiple(
            target_date: date, requests: list, execute_at: datetime | None = None
        ) -> BatchBookingResult:
            return BatchBookingResult(
                results=[
                    BatchBookingItemResult(
                        booking_id=item.booking_id,
                        result=BookingResult(success=True, booked_time=item.target_time),
                    )
                    for item in requests
                ],
                total_succeeded=len(requests),
            )

        provider = MagicMock()
        provider.book_tee_time = AsyncMock()
        provider.book_multiple_tee_times = AsyncMock(side_effect=book_multiple)
        booking_service.set_reservation_provider(provider)

        with patch("app.services.booking_service.database_service") as mock_db:
            self._fake_database(mock_db)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_confirmation = AsyncMock()
                mock_sms.send_booking_failure = AsyncMock()

                with patch.object(CTDateTime, "now") as mock_ct_now:
                    mock_ct_now.return_value = pytz.timezone("America/Chicago").localize(
                        self.NOW_CT
                    )

                    await booking_service._handle_confirm_intent(session)

                    # The regression: two bookings, one task. Checked before
                    # awaiting, while the task set is still populated.
                    assert len(booking_service._background_tasks) == 1

                    await booking_service.wait_for_background_bookings()

        # One session, not one per booking.
        provider.book_tee_time.assert_not_called()
        assert provider.book_multiple_tee_times.await_count == 1

        batch_requests = provider.book_multiple_tee_times.await_args.kwargs["requests"]
        assert [item.target_time for item in batch_requests] == [time(17, 0), time(17, 8)]
        # Off-race, so nothing to wait for before firing.
        assert provider.book_multiple_tee_times.await_args.kwargs["execute_at"] is None

        # Both outcomes still reach the user, in the channel they asked from.
        assert mock_sms.send_booking_confirmation.await_count == 2
        assert all(
            call.args[2] == "778899" for call in mock_sms.send_booking_confirmation.await_args_list
        )
        mock_sms.send_booking_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_batch_that_raises_still_reports_every_booking(
        self, booking_service: BookingService
    ) -> None:
        """Both bookings were acknowledged, so both must get an answer."""
        import pytz

        session = self._session()

        provider = MagicMock()
        provider.book_tee_time = AsyncMock()
        provider.book_multiple_tee_times = AsyncMock(side_effect=RuntimeError("driver died"))
        booking_service.set_reservation_provider(provider)

        with patch("app.services.booking_service.database_service") as mock_db:
            stored = self._fake_database(mock_db)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_confirmation = AsyncMock()
                mock_sms.send_booking_failure = AsyncMock()

                with patch.object(CTDateTime, "now") as mock_ct_now:
                    mock_ct_now.return_value = pytz.timezone("America/Chicago").localize(
                        self.NOW_CT
                    )

                    await booking_service._handle_confirm_intent(session)
                    await booking_service.wait_for_background_bookings()

        assert mock_sms.send_booking_failure.await_count == 2
        assert all(
            call.args[4] == "778899" for call in mock_sms.send_booking_failure.await_args_list
        )
        mock_sms.send_booking_confirmation.assert_not_called()

        # Nothing is left claiming to still be running.
        assert len(stored) == 2
        assert all(b.status == BookingStatus.FAILED for b in stored.values())
        assert all("driver died" in (b.error_message or "") for b in stored.values())

    @pytest.mark.asyncio
    async def test_a_booking_missing_from_the_batch_result_is_still_answered(
        self, booking_service: BookingService
    ) -> None:
        """A result the provider never reported must not become silence."""
        import pytz

        from app.providers.base import BatchBookingItemResult, BatchBookingResult

        session = self._session()

        async def book_only_the_first(
            target_date: date, requests: list, execute_at: datetime | None = None
        ) -> BatchBookingResult:
            return BatchBookingResult(
                results=[
                    BatchBookingItemResult(
                        booking_id=requests[0].booking_id,
                        result=BookingResult(success=True, booked_time=requests[0].target_time),
                    )
                ],
                total_succeeded=1,
            )

        provider = MagicMock()
        provider.book_tee_time = AsyncMock()
        provider.book_multiple_tee_times = AsyncMock(side_effect=book_only_the_first)
        booking_service.set_reservation_provider(provider)

        with patch("app.services.booking_service.database_service") as mock_db:
            self._fake_database(mock_db)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_confirmation = AsyncMock()
                mock_sms.send_booking_failure = AsyncMock()

                with patch.object(CTDateTime, "now") as mock_ct_now:
                    mock_ct_now.return_value = pytz.timezone("America/Chicago").localize(
                        self.NOW_CT
                    )

                    await booking_service._handle_confirm_intent(session)
                    await booking_service.wait_for_background_bookings()

        assert mock_sms.send_booking_confirmation.await_count == 1
        assert mock_sms.send_booking_failure.await_count == 1


class TestStatusIntentVisibility:
    """A booking must never be invisible just because it is mid-attempt.

    A booking that was executing when the process died stayed IN_PROGRESS, and
    the status listing only showed PENDING/SCHEDULED - so asking "did you book
    it?" reported nothing at all about the booking that had just been made.
    """

    @staticmethod
    def _booking(
        status: BookingStatus,
        requested_date: date,
        requested_time: time = time(8, 0),
        error_message: str | None = None,
        actual_booked_time: time | None = None,
    ) -> TeeTimeBooking:
        return TeeTimeBooking(
            id=f"id-{status.value}-{requested_date}",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=requested_date,
                requested_time=requested_time,
                num_players=4,
            ),
            status=status,
            error_message=error_message,
            actual_booked_time=actual_booked_time,
        )

    @pytest.mark.asyncio
    async def test_in_progress_booking_is_listed(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """A booking that is mid-attempt shows up in the status listing."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        in_progress = self._booking(BookingStatus.IN_PROGRESS, date(2026, 8, 8), time(17, 0))

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[in_progress])
            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = tz.localize(datetime(2026, 8, 2, 15, 31))
                response = await booking_service._handle_status_intent(sample_session)

        assert "Saturday, August 08" in response
        assert "05:00 PM" in response
        assert "in_progress" in response

    @pytest.mark.asyncio
    async def test_success_shows_the_time_actually_booked(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """A fallback booking lists the time secured, not the time asked for."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        # Asked for 5:00 PM, the fallback window landed 5:08 PM.
        booked = self._booking(
            BookingStatus.SUCCESS,
            date(2026, 8, 8),
            requested_time=time(17, 0),
            actual_booked_time=time(17, 8),
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[booked])
            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = tz.localize(datetime(2026, 8, 2, 15, 31))
                response = await booking_service._handle_status_intent(sample_session)

        assert "05:08 PM" in response
        # Showing the requested time here would tell the user to turn up eight
        # minutes early for a slot that isn't theirs.
        assert "05:00 PM" not in response
        assert "success" in response

    @pytest.mark.asyncio
    async def test_future_failure_is_listed_with_reason(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Failures for tee times that haven't happened yet are surfaced."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        failed = self._booking(
            BookingStatus.FAILED,
            date(2026, 8, 9),
            error_message="No time slots with 4 available spots",
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[failed])
            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = tz.localize(datetime(2026, 8, 2, 15, 31))
                response = await booking_service._handle_status_intent(sample_session)

        assert "Recent failures" in response
        assert "No time slots with 4 available spots" in response

    @pytest.mark.asyncio
    async def test_past_bookings_are_not_listed(
        self, booking_service: BookingService, sample_session: UserSession
    ) -> None:
        """Old failures don't accumulate in the listing forever."""
        import pytz

        tz = pytz.timezone("America/Chicago")
        stale = [
            self._booking(BookingStatus.FAILED, date(2023, 12, 23)),
            self._booking(BookingStatus.SUCCESS, date(2025, 12, 29)),
        ]

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=stale)
            with patch.object(CTDateTime, "now") as mock_ct_now:
                mock_ct_now.return_value = tz.localize(datetime(2026, 8, 2, 15, 31))
                response = await booking_service._handle_status_intent(sample_session)

        assert "don't have any upcoming bookings" in response.lower()


class TestReconcileInterruptedBookings:
    """Bookings orphaned in IN_PROGRESS by a crash must be resolved and reported.

    A row left at IN_PROGRESS by a run that died mid-attempt (an OOM kill, a
    deploy) sits there forever, and the user is never told the attempt went
    nowhere. Reconciliation resolves those - but only those: this instance sets
    IN_PROGRESS itself for attempts that are still running, and failing one of
    those would both lie to the user and clobber the result it is about to write.
    """

    @staticmethod
    def _in_progress_booking(
        origin_channel_id: str | None = None,
        updated_at: datetime | None = None,
    ) -> TeeTimeBooking:
        return TeeTimeBooking(
            id="orphan01",
            phone_number="+15550001111",
            request=TeeTimeRequest(
                requested_date=date(2026, 8, 8),
                requested_time=time(17, 0),
                num_players=4,
            ),
            status=BookingStatus.IN_PROGRESS,
            origin_channel_id=origin_channel_id,
            # Default to a row last touched well before any test-constructed
            # service started, i.e. a genuine prior-run orphan.
            updated_at=updated_at or datetime(2026, 8, 2, 20, 21, 54),
        )

    @staticmethod
    def _recorder(sink: list[TeeTimeBooking]) -> Callable[[TeeTimeBooking], TeeTimeBooking]:
        """Build an update_booking side effect that records what it was given."""

        def record(booking: TeeTimeBooking) -> TeeTimeBooking:
            sink.append(booking)
            return booking

        return record

    @pytest.mark.asyncio
    async def test_marks_orphan_failed_and_notifies(self, booking_service: BookingService) -> None:
        """An orphaned booking is marked failed and the user is told."""
        orphan = self._in_progress_booking(origin_channel_id="9990001112223330")
        updated: list[TeeTimeBooking] = []

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[orphan])
            mock_db.update_booking = AsyncMock(side_effect=self._recorder(updated))

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                result = await booking_service.reconcile_interrupted_bookings()

        assert len(result) == 1
        assert updated[0].status == BookingStatus.FAILED
        # The reservation's true state is unknown - the message must say so
        # rather than claiming the booking definitely failed.
        assert updated[0].error_message is not None
        assert "may or may not" in updated[0].error_message
        mock_sms.send_booking_failure.assert_awaited_once()
        assert (
            mock_sms.send_booking_failure.await_args.kwargs["origin_channel_id"]
            == "9990001112223330"
        )

    @pytest.mark.asyncio
    async def test_no_orphans_is_a_noop(self, booking_service: BookingService) -> None:
        """With nothing stuck, reconciliation does nothing and sends nothing."""
        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[])
            mock_db.update_booking = AsyncMock()

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                result = await booking_service.reconcile_interrupted_bookings()

        assert result == []
        mock_db.update_booking.assert_not_awaited()
        mock_sms.send_booking_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notification_failure_still_reconciles(
        self, booking_service: BookingService
    ) -> None:
        """A dead messaging channel must not leave the row stuck IN_PROGRESS."""
        orphan = self._in_progress_booking()
        updated: list[TeeTimeBooking] = []

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[orphan])
            mock_db.update_booking = AsyncMock(side_effect=self._recorder(updated))

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock(side_effect=RuntimeError("no gateway"))
                result = await booking_service.reconcile_interrupted_bookings()

        assert len(result) == 1
        assert updated[0].status == BookingStatus.FAILED

    @pytest.mark.asyncio
    async def test_live_attempt_from_this_instance_is_left_alone(
        self, booking_service: BookingService
    ) -> None:
        """An attempt this process started is still running, not orphaned.

        The REST API, the scheduler's batch job and a confirmed chat booking can
        all set IN_PROGRESS after startup. Reconciliation runs as a background
        task once the gateway is ready, so it can overlap with those - and
        failing one would both lie to the user and clobber the real outcome the
        attempt is about to write.
        """
        live = self._in_progress_booking(
            updated_at=booking_service._started_at + timedelta(seconds=1)
        )

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[live])
            mock_db.update_booking = AsyncMock()

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                result = await booking_service.reconcile_interrupted_bookings()

        assert result == []
        mock_db.update_booking.assert_not_awaited()
        mock_sms.send_booking_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unwritable_row_does_not_strand_the_others(
        self, booking_service: BookingService
    ) -> None:
        """One row that won't persist must not abort the whole sweep."""
        first = self._in_progress_booking()
        first.id = "orphan-bad"
        second = self._in_progress_booking()
        second.id = "orphan-good"

        async def update(booking: TeeTimeBooking) -> TeeTimeBooking:
            if booking.id == "orphan-bad":
                raise ValueError("Booking orphan-bad not found")
            return booking

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_bookings = AsyncMock(return_value=[first, second])
            mock_db.update_booking = AsyncMock(side_effect=update)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_failure = AsyncMock()
                result = await booking_service.reconcile_interrupted_bookings()

        # Only the row we actually persisted is reported, and only it is
        # notified - telling the user about a failure we could not record would
        # leave the row stuck and the message wrong.
        assert [b.id for b in result] == ["orphan-good"]
        assert mock_sms.send_booking_failure.await_count == 1


class TestImmediateBookingDoesNotBlockReply:
    """The caller's reply must not depend on the booking attempt finishing.

    execute_booking used to be awaited inline inside create_booking, which meant
    the Discord gateway could not send its reply until a ~75s Selenium run
    finished. When the container was OOM-killed mid-attempt the reply died with
    it and the user got silence.
    """

    @pytest.mark.asyncio
    async def test_create_booking_returns_before_attempt_completes(
        self, booking_service: BookingService
    ) -> None:
        """create_booking returns while the attempt is still running."""
        import pytz

        request = TeeTimeRequest(
            requested_date=date(2025, 12, 29),
            requested_time=time(8, 0),
            num_players=4,
        )
        release = asyncio.Event()

        async def slow_booking(**_kwargs: object) -> BookingResult:
            await release.wait()
            return BookingResult(success=True, booked_time=time(8, 0))

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.create_booking = AsyncMock(side_effect=lambda b: b)
            mock_db.update_booking = AsyncMock(side_effect=lambda b: b)
            mock_db.get_booking = AsyncMock(
                side_effect=lambda _id: TeeTimeBooking(
                    id=_id,
                    phone_number="+15551234567",
                    request=request,
                    status=BookingStatus.IN_PROGRESS,
                )
            )

            mock_provider = MagicMock()
            mock_provider.book_tee_time = AsyncMock(side_effect=slow_booking)
            booking_service.set_reservation_provider(mock_provider)

            with patch("app.services.booking_service.sms_service") as mock_sms:
                mock_sms.send_booking_confirmation = AsyncMock()

                with patch.object(CTDateTime, "now") as mock_ct_now:
                    tz = pytz.timezone("America/Chicago")
                    mock_ct_now.return_value = tz.localize(datetime(2025, 12, 22, 10, 0))

                    result = await booking_service.create_booking("+15551234567", request)

                    # The attempt is deliberately still blocked here: create_booking
                    # returned without waiting for it.
                    assert result.status == BookingStatus.IN_PROGRESS
                    assert not mock_sms.send_booking_confirmation.await_count

                    release.set()
                    await booking_service.wait_for_background_bookings()

                # Only once the attempt finishes does the user get the outcome.
                mock_sms.send_booking_confirmation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_background_attempt_crash_is_contained(
        self, booking_service: BookingService
    ) -> None:
        """An exception in the background attempt is logged, not left unretrieved."""
        with patch.object(
            booking_service, "execute_booking", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            booking_service._spawn_booking_execution("orphan01")
            assert booking_service._background_tasks

            # Must not raise, and must not leave the task result unretrieved.
            await booking_service.wait_for_background_bookings()

        assert not booking_service._background_tasks

    @pytest.mark.asyncio
    async def test_shutdown_wait_cancels_a_hung_attempt(
        self, booking_service: BookingService
    ) -> None:
        """A hung Selenium run must not block gateway and provider cleanup.

        Shutdown awaits this before stopping the gateway and closing the
        provider, and Cloud Run allows only seconds between SIGTERM and SIGKILL.
        """

        async def never_finishes(_booking_id: str) -> bool:
            await asyncio.Event().wait()
            return True

        with patch.object(booking_service, "execute_booking", new=never_finishes):
            booking_service._spawn_booking_execution("hung01")

            await booking_service.wait_for_background_bookings(timeout=0.05)

        assert not booking_service._background_tasks

    @pytest.mark.asyncio
    async def test_spawned_task_is_strongly_referenced(
        self, booking_service: BookingService
    ) -> None:
        """The task is held until it completes, so it can't be garbage collected."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_execute(_booking_id: str) -> bool:
            started.set()
            await release.wait()
            return True

        with patch.object(booking_service, "execute_booking", new=slow_execute):
            booking_service._spawn_booking_execution("orphan01")
            await started.wait()
            assert len(booking_service._background_tasks) == 1

            release.set()
            await booking_service.wait_for_background_bookings()

        assert not booking_service._background_tasks
