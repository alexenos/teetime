"""
Tests for Pydantic schemas in app/models/schemas.py.

These tests verify that all data models work correctly with valid data
and properly validate field constraints.
"""

from datetime import date, datetime, time

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    BookingStatus,
    ConversationState,
    ParsedIntent,
    SMSMessage,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)


class TestBookingStatus:
    """Tests for BookingStatus enum."""

    def test_all_status_values_exist(self) -> None:
        """Test that all expected status values are defined."""
        assert BookingStatus.PENDING == "pending"
        assert BookingStatus.SCHEDULED == "scheduled"
        assert BookingStatus.IN_PROGRESS == "in_progress"
        assert BookingStatus.SUCCESS == "success"
        assert BookingStatus.FAILED == "failed"
        assert BookingStatus.CANCELLED == "cancelled"

    def test_status_is_string_enum(self) -> None:
        """Test that BookingStatus values are strings."""
        for status in BookingStatus:
            assert isinstance(status.value, str)


class TestConversationState:
    """Tests for ConversationState enum."""

    def test_all_state_values_exist(self) -> None:
        """Test that all expected state values are defined."""
        assert ConversationState.IDLE == "idle"
        assert ConversationState.AWAITING_DATE == "awaiting_date"
        assert ConversationState.AWAITING_TIME == "awaiting_time"
        assert ConversationState.AWAITING_PLAYERS == "awaiting_players"
        assert ConversationState.AWAITING_CONFIRMATION == "awaiting_confirmation"

    def test_state_is_string_enum(self) -> None:
        """Test that ConversationState values are strings."""
        for state in ConversationState:
            assert isinstance(state.value, str)


class TestTeeTimeRequest:
    """Tests for TeeTimeRequest model."""

    def test_create_with_required_fields(self) -> None:
        """Test creating a TeeTimeRequest with required fields."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        assert request.requested_date == date(2025, 12, 20)
        assert request.requested_time == time(8, 0)
        assert request.num_players == 4  # default
        assert request.fallback_window_minutes == 32  # default

    def test_create_with_all_fields(self) -> None:
        """Test creating a TeeTimeRequest with all fields."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(9, 30),
            num_players=2,
            fallback_window_minutes=15,
        )
        assert request.requested_date == date(2025, 12, 20)
        assert request.requested_time == time(9, 30)
        assert request.num_players == 2
        assert request.fallback_window_minutes == 15

    def test_num_players_minimum(self) -> None:
        """Test that num_players must be at least 1."""
        with pytest.raises(ValidationError):
            TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=0,
            )

    def test_num_players_maximum(self) -> None:
        """Test that num_players must be at most 4."""
        with pytest.raises(ValidationError):
            TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=5,
            )

    def test_num_players_valid_range(self) -> None:
        """Test that num_players accepts values 1-4."""
        for num in [1, 2, 3, 4]:
            request = TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                num_players=num,
            )
            assert request.num_players == num


class TestTeeTimeBooking:
    """Tests for TeeTimeBooking model."""

    def test_create_with_required_fields(self) -> None:
        """Test creating a TeeTimeBooking with required fields."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        booking = TeeTimeBooking(
            phone_number="+15551234567",
            request=request,
        )
        assert booking.phone_number == "+15551234567"
        assert booking.request == request
        assert booking.status == BookingStatus.PENDING
        assert booking.id is None
        assert booking.confirmation_number is None
        assert booking.error_message is None

    def test_create_with_all_fields(self) -> None:
        """Test creating a TeeTimeBooking with all fields."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        exec_time = datetime(2025, 12, 13, 6, 30)
        booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.SUCCESS,
            scheduled_execution_time=exec_time,
            actual_booked_time=time(8, 8),
            confirmation_number="CONF123",
        )
        assert booking.id == "abc12345"
        assert booking.status == BookingStatus.SUCCESS
        assert booking.scheduled_execution_time == exec_time
        assert booking.actual_booked_time == time(8, 8)
        assert booking.confirmation_number == "CONF123"

    def test_booking_with_error(self) -> None:
        """Test creating a failed booking with error message."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        booking = TeeTimeBooking(
            phone_number="+15551234567",
            request=request,
            status=BookingStatus.FAILED,
            error_message="Time slot not available",
        )
        assert booking.status == BookingStatus.FAILED
        assert booking.error_message == "Time slot not available"


class TestUserSession:
    """Tests for UserSession model."""

    def test_create_with_phone_number(self) -> None:
        """Test creating a UserSession with just phone number."""
        session = UserSession(phone_number="+15551234567")
        assert session.phone_number == "+15551234567"
        assert session.state == ConversationState.IDLE
        assert session.pending_request is None

    def test_create_with_pending_request(self) -> None:
        """Test creating a UserSession with a pending request."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=request,
        )
        assert session.state == ConversationState.AWAITING_CONFIRMATION
        assert session.pending_request == request

    def test_last_interaction_default(self) -> None:
        """Test that last_interaction has a default value."""
        session = UserSession(phone_number="+15551234567")
        assert session.last_interaction is not None
        assert isinstance(session.last_interaction, datetime)


class TestParsedIntent:
    """Tests for ParsedIntent model."""

    def test_create_with_intent_only(self) -> None:
        """Test creating a ParsedIntent with just intent."""
        parsed = ParsedIntent(intent="help")
        assert parsed.intent == "help"
        assert parsed.tee_time_request is None
        assert parsed.booking_id is None
        assert parsed.clarification_needed is None
        assert parsed.response_message is None

    def test_create_book_intent(self) -> None:
        """Test creating a book intent with tee time request."""
        request = TeeTimeRequest(
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
        )
        parsed = ParsedIntent(
            intent="book",
            tee_time_request=request,
            response_message="I'll book that for you!",
        )
        assert parsed.intent == "book"
        assert parsed.tee_time_request == request
        assert parsed.response_message == "I'll book that for you!"

    def test_create_cancel_intent(self) -> None:
        """Test creating a cancel intent with booking ID."""
        parsed = ParsedIntent(
            intent="cancel",
            booking_id="abc12345",
            response_message="Cancelling your booking...",
        )
        assert parsed.intent == "cancel"
        assert parsed.booking_id == "abc12345"

    def test_create_unclear_intent(self) -> None:
        """Test creating an unclear intent with clarification."""
        parsed = ParsedIntent(
            intent="unclear",
            clarification_needed="What date would you like?",
        )
        assert parsed.intent == "unclear"
        assert parsed.clarification_needed == "What date would you like?"


class TestSMSMessage:
    """Tests for SMSMessage model."""

    def test_create_sms_message(self) -> None:
        """Test creating an SMSMessage."""
        msg = SMSMessage(
            from_number="+15551234567",
            to_number="+15559876543",
            body="Book Saturday 8am for 4 players",
        )
        assert msg.from_number == "+15551234567"
        assert msg.to_number == "+15559876543"
        assert msg.body == "Book Saturday 8am for 4 players"

    def test_timestamp_default(self) -> None:
        """Test that timestamp has a default value."""
        msg = SMSMessage(
            from_number="+15551234567",
            to_number="+15559876543",
            body="Hello",
        )
        assert msg.timestamp is not None
        assert isinstance(msg.timestamp, datetime)

    def test_empty_body(self) -> None:
        """Test creating an SMSMessage with empty body."""
        msg = SMSMessage(
            from_number="+15551234567",
            to_number="+15559876543",
            body="",
        )
        assert msg.body == ""
