"""
Tests for DatabaseService in app/services/database_service.py.

These tests verify the database CRUD operations for bookings and sessions,
including conversion between Pydantic schemas and SQLAlchemy models.
"""

from datetime import date, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.database import BookingRecord, SessionRecord
from app.models.schemas import (
    BookingStatus,
    ConversationState,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)
from app.services.database_service import DatabaseService


@pytest.fixture
def database_service() -> DatabaseService:
    """Create a fresh DatabaseService instance for each test."""
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

    def test_booking_to_record(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test converting a TeeTimeBooking to a BookingRecord."""
        record = database_service._booking_to_record(sample_booking)

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

    def test_record_to_booking(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test converting a BookingRecord to a TeeTimeBooking."""
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

        booking = database_service._record_to_booking(record)

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

    def test_booking_roundtrip(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test that booking conversion is reversible."""
        record = database_service._booking_to_record(sample_booking)
        converted_booking = database_service._record_to_booking(record)

        assert converted_booking.id == sample_booking.id
        assert converted_booking.phone_number == sample_booking.phone_number
        assert converted_booking.request.requested_date == sample_booking.request.requested_date
        assert converted_booking.request.requested_time == sample_booking.request.requested_time
        assert converted_booking.status == sample_booking.status


class TestSessionConversion:
    """Tests for session conversion methods."""

    def test_session_to_record_without_pending_request(
        self, database_service: DatabaseService, sample_session: UserSession
    ) -> None:
        """Test converting a UserSession without pending request to a SessionRecord."""
        record = database_service._session_to_record(sample_session)

        assert record.phone_number == sample_session.phone_number
        assert record.state == sample_session.state
        assert record.pending_request_json is None
        assert record.last_interaction == sample_session.last_interaction

    def test_session_to_record_with_pending_request(
        self, database_service: DatabaseService, sample_session_with_request: UserSession
    ) -> None:
        """Test converting a UserSession with pending request to a SessionRecord."""
        record = database_service._session_to_record(sample_session_with_request)

        assert record.phone_number == sample_session_with_request.phone_number
        assert record.state == sample_session_with_request.state
        assert record.pending_request_json is not None
        assert "2025-12-20" in record.pending_request_json

    def test_record_to_session_without_pending_request(
        self, database_service: DatabaseService
    ) -> None:
        """Test converting a SessionRecord without pending request to a UserSession."""
        record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.IDLE,
            pending_request_json=None,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        session = database_service._record_to_session(record)

        assert session.phone_number == record.phone_number
        assert session.state == record.state
        assert session.pending_request is None
        assert session.last_interaction == record.last_interaction

    def test_record_to_session_with_pending_request(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test converting a SessionRecord with pending request to a UserSession."""
        pending_json = sample_request.model_dump_json()
        record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request_json=pending_json,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        session = database_service._record_to_session(record)

        assert session.phone_number == record.phone_number
        assert session.state == record.state
        assert session.pending_request is not None
        assert session.pending_request.requested_date == sample_request.requested_date
        assert session.pending_request.requested_time == sample_request.requested_time

    def test_session_roundtrip(
        self, database_service: DatabaseService, sample_session_with_request: UserSession
    ) -> None:
        """Test that session conversion is reversible."""
        record = database_service._session_to_record(sample_session_with_request)
        converted_session = database_service._record_to_session(record)

        assert converted_session.phone_number == sample_session_with_request.phone_number
        assert converted_session.state == sample_session_with_request.state
        assert converted_session.pending_request is not None
        assert (
            converted_session.pending_request.requested_date
            == sample_session_with_request.pending_request.requested_date
        )


class TestBookingCRUD:
    """Tests for booking CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_booking(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test creating a new booking."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.create_booking(sample_booking)

            mock_db.add.assert_called_once()
            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once()
            assert result.id == sample_booking.id

    @pytest.mark.asyncio
    async def test_get_booking_exists(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test getting an existing booking."""
        mock_record = BookingRecord(
            booking_id="test1234",
            phone_number="+15551234567",
            requested_date=sample_request.requested_date,
            requested_time=sample_request.requested_time,
            num_players=sample_request.num_players,
            fallback_window_minutes=sample_request.fallback_window_minutes,
            status=BookingStatus.SCHEDULED,
            created_at=datetime(2025, 12, 6, 10, 0),
            updated_at=datetime(2025, 12, 6, 10, 0),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_booking("test1234")

            assert result is not None
            assert result.id == "test1234"
            assert result.phone_number == "+15551234567"

    @pytest.mark.asyncio
    async def test_get_booking_not_exists(self, database_service: DatabaseService) -> None:
        """Test getting a non-existent booking."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_booking("nonexistent")

            assert result is None

    @pytest.mark.asyncio
    async def test_get_bookings_all(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test getting all bookings."""
        mock_records = [
            BookingRecord(
                booking_id="test1234",
                phone_number="+15551234567",
                requested_date=sample_request.requested_date,
                requested_time=sample_request.requested_time,
                num_players=sample_request.num_players,
                fallback_window_minutes=sample_request.fallback_window_minutes,
                status=BookingStatus.SCHEDULED,
                created_at=datetime(2025, 12, 6, 10, 0),
                updated_at=datetime(2025, 12, 6, 10, 0),
            ),
            BookingRecord(
                booking_id="test5678",
                phone_number="+15559876543",
                requested_date=sample_request.requested_date,
                requested_time=sample_request.requested_time,
                num_players=sample_request.num_players,
                fallback_window_minutes=sample_request.fallback_window_minutes,
                status=BookingStatus.SUCCESS,
                created_at=datetime(2025, 12, 6, 11, 0),
                updated_at=datetime(2025, 12, 6, 11, 0),
            ),
        ]

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = mock_records
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_bookings()

            assert len(result) == 2
            assert result[0].id == "test1234"
            assert result[1].id == "test5678"

    @pytest.mark.asyncio
    async def test_get_bookings_by_phone(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by phone number."""
        mock_record = BookingRecord(
            booking_id="test1234",
            phone_number="+15551234567",
            requested_date=sample_request.requested_date,
            requested_time=sample_request.requested_time,
            num_players=sample_request.num_players,
            fallback_window_minutes=sample_request.fallback_window_minutes,
            status=BookingStatus.SCHEDULED,
            created_at=datetime(2025, 12, 6, 10, 0),
            updated_at=datetime(2025, 12, 6, 10, 0),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_record]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_bookings(phone_number="+15551234567")

            assert len(result) == 1
            assert result[0].phone_number == "+15551234567"

    @pytest.mark.asyncio
    async def test_get_bookings_by_status(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test filtering bookings by status."""
        mock_record = BookingRecord(
            booking_id="test1234",
            phone_number="+15551234567",
            requested_date=sample_request.requested_date,
            requested_time=sample_request.requested_time,
            num_players=sample_request.num_players,
            fallback_window_minutes=sample_request.fallback_window_minutes,
            status=BookingStatus.SCHEDULED,
            created_at=datetime(2025, 12, 6, 10, 0),
            updated_at=datetime(2025, 12, 6, 10, 0),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_record]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_bookings(status=BookingStatus.SCHEDULED)

            assert len(result) == 1
            assert result[0].status == BookingStatus.SCHEDULED

    @pytest.mark.asyncio
    async def test_update_booking_success(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test updating an existing booking."""
        mock_record = BookingRecord(
            booking_id="test1234",
            phone_number="+15551234567",
            requested_date=sample_request.requested_date,
            requested_time=sample_request.requested_time,
            num_players=sample_request.num_players,
            fallback_window_minutes=sample_request.fallback_window_minutes,
            status=BookingStatus.SCHEDULED,
            created_at=datetime(2025, 12, 6, 10, 0),
            updated_at=datetime(2025, 12, 6, 10, 0),
        )

        booking_to_update = TeeTimeBooking(
            id="test1234",
            phone_number="+15551234567",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            actual_booked_time=time(8, 0),
            confirmation_number="CONF123",
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            await database_service.update_booking(booking_to_update)

            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once()
            assert mock_record.status == BookingStatus.SUCCESS
            assert mock_record.actual_booked_time == time(8, 0)
            assert mock_record.confirmation_number == "CONF123"

    @pytest.mark.asyncio
    async def test_update_booking_not_found(
        self, database_service: DatabaseService, sample_booking: TeeTimeBooking
    ) -> None:
        """Test updating a non-existent booking raises an error."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            with pytest.raises(ValueError, match="Booking test1234 not found"):
                await database_service.update_booking(sample_booking)


class TestSessionCRUD:
    """Tests for session CRUD operations."""

    @pytest.mark.asyncio
    async def test_get_session_exists(self, database_service: DatabaseService) -> None:
        """Test getting an existing session."""
        mock_record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.IDLE,
            pending_request_json=None,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_session("+15551234567")

            assert result is not None
            assert result.phone_number == "+15551234567"
            assert result.state == ConversationState.IDLE

    @pytest.mark.asyncio
    async def test_get_session_not_exists(self, database_service: DatabaseService) -> None:
        """Test getting a non-existent session."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_session("+15559999999")

            assert result is None

    @pytest.mark.asyncio
    async def test_create_session(
        self, database_service: DatabaseService, sample_session: UserSession
    ) -> None:
        """Test creating a new session."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.create_session(sample_session)

            mock_db.add.assert_called_once()
            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once()
            assert result.phone_number == sample_session.phone_number

    @pytest.mark.asyncio
    async def test_update_session_success(
        self, database_service: DatabaseService, sample_request: TeeTimeRequest
    ) -> None:
        """Test updating an existing session."""
        mock_record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.IDLE,
            pending_request_json=None,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        session_to_update = UserSession(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request=sample_request,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            await database_service.update_session(session_to_update)

            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once()
            assert mock_record.state == ConversationState.AWAITING_CONFIRMATION
            assert mock_record.pending_request_json is not None

    @pytest.mark.asyncio
    async def test_update_session_not_found(
        self, database_service: DatabaseService, sample_session: UserSession
    ) -> None:
        """Test updating a non-existent session raises an error."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            with pytest.raises(ValueError, match="Session for .* not found"):
                await database_service.update_session(sample_session)

    @pytest.mark.asyncio
    async def test_get_or_create_session_existing(self, database_service: DatabaseService) -> None:
        """Test get_or_create_session returns existing session."""
        mock_record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_DATE,
            pending_request_json=None,
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_or_create_session("+15551234567")

            assert result.phone_number == "+15551234567"
            assert result.state == ConversationState.AWAITING_DATE

    @pytest.mark.asyncio
    async def test_get_or_create_session_new(self, database_service: DatabaseService) -> None:
        """Test get_or_create_session creates new session when none exists."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            result = await database_service.get_or_create_session("+15559999999")

            assert result.phone_number == "+15559999999"
            assert result.state == ConversationState.IDLE
            mock_db.add.assert_called_once()


class TestUpdateSessionClearsPendingRequest:
    """Tests for session update clearing pending request."""

    @pytest.mark.asyncio
    async def test_update_session_clears_pending_request(
        self, database_service: DatabaseService
    ) -> None:
        """Test that updating a session can clear the pending request."""
        mock_record = SessionRecord(
            phone_number="+15551234567",
            state=ConversationState.AWAITING_CONFIRMATION,
            pending_request_json='{"requested_date": "2025-12-20", "requested_time": "08:00:00"}',
            last_interaction=datetime(2025, 12, 6, 10, 0),
        )

        session_to_update = UserSession(
            phone_number="+15551234567",
            state=ConversationState.IDLE,
            pending_request=None,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch("app.services.database_service.AsyncSessionLocal") as mock_session_local:
            mock_session_local.return_value.__aenter__.return_value = mock_db

            await database_service.update_session(session_to_update)

            assert mock_record.state == ConversationState.IDLE
            assert mock_record.pending_request_json is None
