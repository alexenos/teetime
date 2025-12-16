"""
Tests for DatabaseService in app/services/database_service.py.

These tests use an in-memory SQLite database to verify actual SQL behavior,
including CRUD operations, filtering, constraints, and edge cases.
"""

from datetime import date, datetime, time

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, BookingRecord, SessionRecord
from app.models.schemas import (
    BookingStatus,
    ConversationState,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)
from app.services.database_service import DatabaseService


@pytest_asyncio.fixture
async def test_engine():
    """Create an in-memory SQLite engine for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session_local(test_engine):
    """Create a sessionmaker bound to the test engine."""
    return sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def database_service(test_session_local, monkeypatch):
    """Create a DatabaseService that uses the test database."""
    monkeypatch.setattr("app.services.database_service.AsyncSessionLocal", test_session_local)
    return DatabaseService()


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
def sample_booking(sample_request: TeeTimeRequest) -> TeeTimeBooking:
    """Create a sample TeeTimeBooking for testing."""
    return TeeTimeBooking(
        id="test1234",
        phone_number="+15551234567",
        request=sample_request,
        status=BookingStatus.SCHEDULED,
        scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        created_at=datetime(2025, 12, 6, 10, 0),
        updated_at=datetime(2025, 12, 6, 10, 0),
    )


@pytest.fixture
def sample_session() -> UserSession:
    """Create a sample UserSession for testing."""
    return UserSession(phone_number="+15551234567")


@pytest.fixture
def sample_session_with_request(sample_request: TeeTimeRequest) -> UserSession:
    """Create a sample UserSession with a pending request for testing."""
    return UserSession(
        phone_number="+15551234567",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_request=sample_request,
    )


class TestBookingConversion:
    """Tests for booking conversion methods."""

    def test_booking_to_record(self, sample_booking: TeeTimeBooking) -> None:
        """Test converting a TeeTimeBooking to a BookingRecord."""
        service = DatabaseService()
        record = service._booking_to_record(sample_booking)

        assert record.booking_id == sample_booking.id
        assert record.phone_number == sample_booking.phone_number
        assert record.requested_date == sample_booking.request.requested_date
        assert record.requested_time == sample_booking.request.requested_time
        assert record.num_players == sample_booking.request.num_players
        assert record.fallback_window_minutes == sample_booking.request.fallback_window_minutes
        assert record.status == sample_booking.status
        assert record.scheduled_execution_time == sample_booking.scheduled_execution_time
        assert record.actual_booked_time == sample_booking.actual_booked_time
        assert record.confirmation_number == sample_booking.confirmation_number
        assert record.error_message == sample_booking.error_message

    def test_record_to_booking(self, sample_request: TeeTimeRequest) -> None:
        """Test converting a BookingRecord to a TeeTimeBooking."""
        service = DatabaseService()
        record = BookingRecord(
            booking_id="test1234",
            phone_number="+15551234567",
            requested_date=sample_request.requested_date,
            requested_time=sample_request.requested_time,
            num_players=sample_request.num_players,
            fallback_window_minutes=sample_request.fallback_window_minutes,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
            actual_booked_time=time(8, 8),
            confirmation_number="CONF123",
            error_message=None,
            created_at=datetime(2025, 12, 6, 10, 0),
            updated_at=datetime(2025, 12, 6, 10, 0),
        )

        booking = service._record_to_booking(record)

        assert booking.id == record.booking_id
        assert booking.phone_number == record.phone_number
        assert booking.request.requested_date == record.requested_date
        assert booking.request.requested_time == record.requested_time
        assert booking.request.num_players == record.num_players
        assert booking.request.fallback_window_minutes == record.fallback_window_minutes
        assert booking.status == record.status
        assert booking.scheduled_execution_time == record.scheduled_execution_time
        assert booking.actual_booked_time == record.actual_booked_time
        assert booking.confirmation_number == record.confirmation_number
        assert booking.error_message == record.error_message

    def test_booking_roundtrip(self, sample_booking: TeeTimeBooking) -> None:
        """Test that booking conversion is reversible."""
        service = DatabaseService()
        record = service._booking_to_record(sample_booking)
        converted_booking = service._record_to_booking(record)

        assert converted_booking.id == sample_booking.id
        assert converted_booking.phone_number == sample_booking.phone_number
        assert converted_booking.request.requested_date == sample_booking.request.requested_date
        assert converted_booking.request.requested_time == sample_booking.request.requested_time
        assert converted_booking.status == sample_booking.status


class TestSessionConversion:
    """Tests for session conversion methods."""

    def test_session_to_record_without_pending_request(self, sample_session: UserSession) -> None:
        """Test converting a UserSession without pending request to a SessionRecord."""
        service = DatabaseService()
        record = service._session_to_record(sample_session)

        assert record.phone_number == sample_session.phone_number
        assert record.state == sample_session.state
        assert record.pending_request_json is None
        assert record.last_interaction == sample_session.last_interaction

    def test_session_to_record_with_pending_request(
        self, sample_session_with_request: UserSession
    ) -> None:
        """Test converting a UserSession with pending request to a SessionRecord."""
        service = DatabaseService()
        record = service._session_to_record(sample_session_with_request)

        assert record.phone_number == sample_session_with_request.phone_number
        assert record.state == sample_session_with_request.state
        assert record.pending_request_json is not None
        assert "2025-12-20" in record.pending_request_json

    def test_record_to_session_without_pending_request(self) -> None:
        """Test converting a SessionRecord without pending request to a UserSession."""
        service = DatabaseService()
        record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.IDLE,
            pending_request_json=None,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        session = service._record_to_session(record)

        assert session.phone_number == record.phone_number
        assert session.state == record.state
        assert session.pending_request is None
        assert session.last_interaction == record.last_interaction

    def test_record_to_session_with_pending_request(self, sample_request: TeeTimeRequest) -> None:
        """Test converting a SessionRecord with pending request to a UserSession."""
        service = DatabaseService()
        pending_json = sample_request.model_dump_json()
        record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request_json=pending_json,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        session = service._record_to_session(record)

        assert session.phone_number == record.phone_number
        assert session.state == record.state
        assert session.pending_request is not None
        assert session.pending_request.requested_date == sample_request.requested_date
        assert session.pending_request.requested_time == sample_request.requested_time

    def test_session_roundtrip(self, sample_session_with_request: UserSession) -> None:
        """Test that session conversion is reversible."""
        service = DatabaseService()
        record = service._session_to_record(sample_session_with_request)
        converted_session = service._record_to_session(record)

        assert converted_session.phone_number == sample_session_with_request.phone_number
        assert converted_session.state == sample_session_with_request.state
        assert converted_session.pending_request is not None
        assert (
            converted_session.pending_request.requested_date
            == sample_session_with_request.pending_request.requested_date
        )


class TestBookingCRUD:
    """Tests for booking CRUD operations with real database."""

    @pytest.mark.asyncio
    async def test_create_booking(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test creating a new booking in the database."""
        result = await database_service.create_booking(sample_booking)

        assert result.id == sample_booking.id
        assert result.phone_number == sample_booking.phone_number
        assert result.status == sample_booking.status

    @pytest.mark.asyncio
    async def test_create_and_get_booking(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test creating and then retrieving a booking."""
        await database_service.create_booking(sample_booking)
        result = await database_service.get_booking(sample_booking.id)

        assert result is not None
        assert result.id == sample_booking.id
        assert result.phone_number == sample_booking.phone_number
        assert result.request.requested_date == sample_booking.request.requested_date
        assert result.request.requested_time == sample_booking.request.requested_time

    @pytest.mark.asyncio
    async def test_get_booking_not_exists(self, database_service: DatabaseService) -> None:
        """Test getting a non-existent booking returns None."""
        result = await database_service.get_booking("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_bookings_all(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test getting all bookings from the database."""
        booking1 = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="test5678",
            phone_number="+15559876543",
            request=sample_request,
            status=BookingStatus.SUCCESS,
        )

        await database_service.create_booking(booking1)
        await database_service.create_booking(booking2)

        result = await database_service.get_bookings()

        assert len(result) == 2
        booking_ids = {b.id for b in result}
        assert "test1234" in booking_ids
        assert "test5678" in booking_ids

    @pytest.mark.asyncio
    async def test_get_bookings_filter_by_phone(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by phone number actually filters in SQL."""
        booking1 = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="test5678",
            phone_number="+15559876543",
            request=sample_request,
            status=BookingStatus.SUCCESS,
        )

        await database_service.create_booking(booking1)
        await database_service.create_booking(booking2)

        result = await database_service.get_bookings(phone_number="+15551234567")

        assert len(result) == 1
        assert result[0].id == "test1234"
        assert result[0].phone_number == "+15551234567"

    @pytest.mark.asyncio
    async def test_get_bookings_filter_by_status(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by status actually filters in SQL."""
        booking1 = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="test5678",
            phone_number="+15559876543",
            request=sample_request,
            status=BookingStatus.SUCCESS,
        )

        await database_service.create_booking(booking1)
        await database_service.create_booking(booking2)

        scheduled = await database_service.get_bookings(status=BookingStatus.SCHEDULED)
        success = await database_service.get_bookings(status=BookingStatus.SUCCESS)

        assert len(scheduled) == 1
        assert scheduled[0].id == "test1234"
        assert len(success) == 1
        assert success[0].id == "test5678"

    @pytest.mark.asyncio
    async def test_get_bookings_filter_by_phone_and_status(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by both phone and status."""
        booking1 = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="test5678",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SUCCESS,
        )
        booking3 = TeeTimeBooking(
            id="test9999",
            phone_number="+15559876543",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )

        await database_service.create_booking(booking1)
        await database_service.create_booking(booking2)
        await database_service.create_booking(booking3)

        result = await database_service.get_bookings(
            phone_number="+15551234567", status=BookingStatus.SCHEDULED
        )

        assert len(result) == 1
        assert result[0].id == "test1234"

    @pytest.mark.asyncio
    async def test_update_booking_success(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test updating an existing booking."""
        await database_service.create_booking(sample_booking)

        sample_booking.status = BookingStatus.SUCCESS
        sample_booking.actual_booked_time = time(8, 8)
        sample_booking.confirmation_number = "CONF123"

        result = await database_service.update_booking(sample_booking)

        assert result.status == BookingStatus.SUCCESS
        assert result.actual_booked_time == time(8, 8)
        assert result.confirmation_number == "CONF123"

        retrieved = await database_service.get_booking(sample_booking.id)
        assert retrieved.status == BookingStatus.SUCCESS
        assert retrieved.confirmation_number == "CONF123"

    @pytest.mark.asyncio
    async def test_update_booking_updates_timestamp(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test that update_booking updates the updated_at timestamp."""
        created = await database_service.create_booking(sample_booking)
        original_updated_at = created.updated_at

        sample_booking.status = BookingStatus.SUCCESS
        updated = await database_service.update_booking(sample_booking)

        assert updated.updated_at is not None
        assert updated.updated_at >= original_updated_at

    @pytest.mark.asyncio
    async def test_update_booking_not_found(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test updating a non-existent booking raises ValueError."""
        with pytest.raises(ValueError, match="Booking test1234 not found"):
            await database_service.update_booking(sample_booking)


class TestSessionCRUD:
    """Tests for session CRUD operations with real database."""

    @pytest.mark.asyncio
    async def test_create_session(
        self, database_service: DatabaseService, sample_session: UserSession
    ) -> None:
        """Test creating a new session in the database."""
        result = await database_service.create_session(sample_session)

        assert result.phone_number == sample_session.phone_number
        assert result.state == ConversationState.IDLE

    @pytest.mark.asyncio
    async def test_create_and_get_session(
        self, database_service: DatabaseService, sample_session: UserSession
    ) -> None:
        """Test creating and then retrieving a session."""
        await database_service.create_session(sample_session)
        result = await database_service.get_session(sample_session.phone_number)

        assert result is not None
        assert result.phone_number == sample_session.phone_number
        assert result.state == ConversationState.IDLE

    @pytest.mark.asyncio
    async def test_get_session_not_exists(self, database_service: DatabaseService) -> None:
        """Test getting a non-existent session returns None."""
        result = await database_service.get_session("+15559999999")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_session_success(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test updating an existing session."""
        session = UserSession(phone_number="+15551234567")
        await database_service.create_session(session)

        session.state = ConversationState.AWAITING_CONFIRMATION
        session.pending_request = sample_request

        result = await database_service.update_session(session)

        assert result.state == ConversationState.AWAITING_CONFIRMATION
        assert result.pending_request is not None
        assert result.pending_request.requested_date == sample_request.requested_date

        retrieved = await database_service.get_session(session.phone_number)
        assert retrieved.state == ConversationState.AWAITING_CONFIRMATION

    @pytest.mark.asyncio
    async def test_update_session_clears_pending_request(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that updating a session can clear the pending request."""
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=sample_request,
        )
        await database_service.create_session(session)

        session.state = ConversationState.IDLE
        session.pending_request = None

        result = await database_service.update_session(session)

        assert result.state == ConversationState.IDLE
        assert result.pending_request is None

        retrieved = await database_service.get_session(session.phone_number)
        assert retrieved.pending_request is None

    @pytest.mark.asyncio
    async def test_update_session_not_found(
        self, database_service: DatabaseService, sample_session: UserSession
    ) -> None:
        """Test updating a non-existent session raises ValueError."""
        with pytest.raises(ValueError, match="Session for .* not found"):
            await database_service.update_session(sample_session)

    @pytest.mark.asyncio
    async def test_get_or_create_session_creates_new(
        self, database_service: DatabaseService
    ) -> None:
        """Test get_or_create_session creates a new session when none exists."""
        result = await database_service.get_or_create_session("+15559999999")

        assert result.phone_number == "+15559999999"
        assert result.state == ConversationState.IDLE
        assert result.pending_request is None

    @pytest.mark.asyncio
    async def test_get_or_create_session_returns_existing(
        self, database_service: DatabaseService
    ) -> None:
        """Test get_or_create_session returns existing session."""
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_DATE,
        )
        await database_service.create_session(session)

        result = await database_service.get_or_create_session("+15551234567")

        assert result.phone_number == "+15551234567"
        assert result.state == ConversationState.AWAITING_DATE


class TestNullHandling:
    """Tests for NULL handling in database operations."""

    @pytest.mark.asyncio
    async def test_booking_with_null_optional_fields(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that optional NULL fields are handled correctly."""
        booking = TeeTimeBooking(
            id="testnull",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.PENDING,
            actual_booked_time=None,
            confirmation_number=None,
            error_message=None,
        )

        await database_service.create_booking(booking)
        result = await database_service.get_booking("testnull")

        assert result is not None
        assert result.actual_booked_time is None
        assert result.confirmation_number is None
        assert result.error_message is None

    @pytest.mark.asyncio
    async def test_booking_with_error_message(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test storing and retrieving error messages."""
        booking = TeeTimeBooking(
            id="testerror",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.FAILED,
            error_message="Time slot not available",
        )

        await database_service.create_booking(booking)
        result = await database_service.get_booking("testerror")

        assert result is not None
        assert result.status == BookingStatus.FAILED
        assert result.error_message == "Time slot not available"

    @pytest.mark.asyncio
    async def test_session_with_null_pending_request(
        self, database_service: DatabaseService
    ) -> None:
        """Test that NULL pending_request is handled correctly."""
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.IDLE,
            pending_request=None,
        )

        await database_service.create_session(session)
        result = await database_service.get_session("+15551234567")

        assert result is not None
        assert result.pending_request is None


class TestConstraints:
    """Tests for database constraints."""

    @pytest.mark.asyncio
    async def test_unique_booking_id_constraint(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that duplicate booking_id raises IntegrityError."""
        booking1 = TeeTimeBooking(
            id="duplicate",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )
        booking2 = TeeTimeBooking(
            id="duplicate",
            phone_number="+15559876543",
            request=sample_request,
            status=BookingStatus.PENDING,
        )

        await database_service.create_booking(booking1)

        with pytest.raises(IntegrityError):
            await database_service.create_booking(booking2)

    @pytest.mark.asyncio
    async def test_unique_session_phone_constraint(self, database_service: DatabaseService) -> None:
        """Test that duplicate phone_number in sessions raises IntegrityError."""
        session1 = UserSession(phone_number="+15551234567")
        session2 = UserSession(phone_number="+15551234567")

        await database_service.create_session(session1)

        with pytest.raises(IntegrityError):
            await database_service.create_session(session2)


class TestBookingStatusTransitions:
    """Tests for booking status transitions."""

    @pytest.mark.asyncio
    async def test_status_transition_scheduled_to_success(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test transitioning booking from SCHEDULED to SUCCESS."""
        booking = TeeTimeBooking(
            id="transition1",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )

        await database_service.create_booking(booking)

        booking.status = BookingStatus.SUCCESS
        booking.confirmation_number = "CONF123"
        booking.actual_booked_time = time(8, 0)

        result = await database_service.update_booking(booking)

        assert result.status == BookingStatus.SUCCESS
        assert result.confirmation_number == "CONF123"

    @pytest.mark.asyncio
    async def test_status_transition_scheduled_to_failed(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test transitioning booking from SCHEDULED to FAILED."""
        booking = TeeTimeBooking(
            id="transition2",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )

        await database_service.create_booking(booking)

        booking.status = BookingStatus.FAILED
        booking.error_message = "No available slots"

        result = await database_service.update_booking(booking)

        assert result.status == BookingStatus.FAILED
        assert result.error_message == "No available slots"

    @pytest.mark.asyncio
    async def test_status_transition_scheduled_to_cancelled(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test transitioning booking from SCHEDULED to CANCELLED."""
        booking = TeeTimeBooking(
            id="transition3",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
        )

        await database_service.create_booking(booking)

        booking.status = BookingStatus.CANCELLED

        result = await database_service.update_booking(booking)

        assert result.status == BookingStatus.CANCELLED


class TestConversationStateTransitions:
    """Tests for session conversation state transitions."""

    @pytest.mark.asyncio
    async def test_state_transition_idle_to_awaiting_confirmation(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test transitioning session from IDLE to AWAITING_CONFIRMATION."""
        session = UserSession(phone_number="+15551234567")
        await database_service.create_session(session)

        session.state = ConversationState.AWAITING_CONFIRMATION
        session.pending_request = sample_request

        result = await database_service.update_session(session)

        assert result.state == ConversationState.AWAITING_CONFIRMATION
        assert result.pending_request is not None

    @pytest.mark.asyncio
    async def test_state_transition_awaiting_confirmation_to_idle(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test transitioning session from AWAITING_CONFIRMATION back to IDLE."""
        session = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=sample_request,
        )
        await database_service.create_session(session)

        session.state = ConversationState.IDLE
        session.pending_request = None

        result = await database_service.update_session(session)

        assert result.state == ConversationState.IDLE
        assert result.pending_request is None


class TestGetDueBookings:
    """Tests for get_due_bookings method with database-level filtering."""

    @pytest.mark.asyncio
    async def test_get_due_bookings_returns_due_bookings(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that get_due_bookings returns bookings with scheduled_execution_time <= due_before."""
        due_booking = TeeTimeBooking(
            id="due1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        await database_service.create_booking(due_booking)

        due_before = datetime(2025, 12, 13, 6, 31)
        result = await database_service.get_due_bookings(due_before)

        assert len(result) == 1
        assert result[0].id == "due1234"

    @pytest.mark.asyncio
    async def test_get_due_bookings_excludes_future_bookings(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that get_due_bookings excludes bookings scheduled for the future."""
        future_booking = TeeTimeBooking(
            id="future1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 20, 6, 30),
        )
        await database_service.create_booking(future_booking)

        due_before = datetime(2025, 12, 13, 6, 30)
        result = await database_service.get_due_bookings(due_before)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_due_bookings_excludes_non_scheduled_status(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that get_due_bookings only returns SCHEDULED bookings."""
        success_booking = TeeTimeBooking(
            id="success1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        failed_booking = TeeTimeBooking(
            id="failed1234",
            phone_number="+15552222222",
            request=sample_request,
            status=BookingStatus.FAILED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        cancelled_booking = TeeTimeBooking(
            id="cancelled1234",
            phone_number="+15553333333",
            request=sample_request,
            status=BookingStatus.CANCELLED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )

        await database_service.create_booking(success_booking)
        await database_service.create_booking(failed_booking)
        await database_service.create_booking(cancelled_booking)

        due_before = datetime(2025, 12, 13, 6, 31)
        result = await database_service.get_due_bookings(due_before)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_due_bookings_mixed_bookings(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test get_due_bookings with a mix of due, future, and non-scheduled bookings."""
        due_scheduled = TeeTimeBooking(
            id="due_scheduled",
            phone_number="+15551111111",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        future_scheduled = TeeTimeBooking(
            id="future_scheduled",
            phone_number="+15552222222",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 20, 6, 30),
        )
        due_success = TeeTimeBooking(
            id="due_success",
            phone_number="+15553333333",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        another_due_scheduled = TeeTimeBooking(
            id="another_due_scheduled",
            phone_number="+15554444444",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 29),
        )

        await database_service.create_booking(due_scheduled)
        await database_service.create_booking(future_scheduled)
        await database_service.create_booking(due_success)
        await database_service.create_booking(another_due_scheduled)

        due_before = datetime(2025, 12, 13, 6, 30)
        result = await database_service.get_due_bookings(due_before)

        assert len(result) == 2
        result_ids = {b.id for b in result}
        assert "due_scheduled" in result_ids
        assert "another_due_scheduled" in result_ids
        assert "future_scheduled" not in result_ids
        assert "due_success" not in result_ids

    @pytest.mark.asyncio
    async def test_get_due_bookings_exact_time_match(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that bookings with exact scheduled_execution_time == due_before are included."""
        exact_booking = TeeTimeBooking(
            id="exact1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        await database_service.create_booking(exact_booking)

        due_before = datetime(2025, 12, 13, 6, 30)
        result = await database_service.get_due_bookings(due_before)

        assert len(result) == 1
        assert result[0].id == "exact1234"

    @pytest.mark.asyncio
    async def test_get_due_bookings_empty_database(self, database_service: DatabaseService) -> None:
        """Test get_due_bookings returns empty list when no bookings exist."""
        due_before = datetime(2025, 12, 13, 6, 30)
        result = await database_service.get_due_bookings(due_before)

        assert result == []

    @pytest.mark.asyncio
    async def test_get_due_bookings_excludes_null_execution_time(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test that bookings with null scheduled_execution_time are excluded."""
        null_time_booking = TeeTimeBooking(
            id="null1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=None,
        )
        await database_service.create_booking(null_time_booking)

        due_before = datetime(2025, 12, 13, 6, 30)
        result = await database_service.get_due_bookings(due_before)

        assert len(result) == 0
