"""
Tests for API endpoints in app/api/.

These tests verify the FastAPI endpoints for health checks, bookings,
and Twilio webhooks using the TestClient.
"""

from datetime import date, time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import BookingStatus, TeeTimeBooking, TeeTimeRequest


@pytest.fixture
def test_client() -> TestClient:
    """Create a TestClient for the FastAPI app."""
    from app.main import app

    return TestClient(app)


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, test_client: TestClient) -> None:
        """Test the /health endpoint."""
        response = test_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "teetime"

    def test_root_endpoint(self, test_client: TestClient) -> None:
        """Test the root / endpoint."""
        response = test_client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "TeeTime - Golf Reservation Assistant"
        assert data["version"] == "0.1.0"
        assert "endpoints" in data
        assert data["endpoints"]["health"] == "/health"
        assert data["endpoints"]["webhooks"] == "/webhooks/twilio/sms"
        assert data["endpoints"]["bookings"] == "/bookings"


class TestBookingsEndpoints:
    """Tests for booking CRUD endpoints."""

    def test_create_booking(self, test_client: TestClient) -> None:
        """Test creating a new booking via POST /bookings/."""
        from datetime import datetime

        import pytz

        request_data = {
            "phone_number": "+15551234567",
            "requested_date": "2025-12-30",
            "requested_time": "08:00:00",
            "num_players": 4,
            "fallback_window_minutes": 30,
        }

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(
                booking: TeeTimeBooking,
            ) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch("app.services.booking_service.datetime") as mock_datetime:
                tz = pytz.timezone("America/Chicago")
                mock_now = datetime(2025, 12, 22, 10, 0)
                mock_datetime.now.return_value = tz.localize(mock_now)
                mock_datetime.combine = datetime.combine
                mock_datetime.min = datetime.min

                response = test_client.post("/bookings/", json=request_data)

            assert response.status_code == 200
            data = response.json()
            assert data["phone_number"] == "+15551234567"
            assert data["requested_date"] == "2025-12-30"
            assert data["requested_time"] == "08:00:00"
            assert data["num_players"] == 4
            assert data["status"] == "scheduled"
            assert data["id"] is not None

    def test_create_booking_default_players(self, test_client: TestClient) -> None:
        """Test creating a booking with default num_players."""
        from datetime import datetime

        import pytz

        request_data = {
            "phone_number": "+15551234567",
            "requested_date": "2025-12-30",
            "requested_time": "09:00:00",
        }

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(
                booking: TeeTimeBooking,
            ) -> TeeTimeBooking:
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch("app.services.booking_service.datetime") as mock_datetime:
                tz = pytz.timezone("America/Chicago")
                mock_now = datetime(2025, 12, 22, 10, 0)
                mock_datetime.now.return_value = tz.localize(mock_now)
                mock_datetime.combine = datetime.combine
                mock_datetime.min = datetime.min

                response = test_client.post("/bookings/", json=request_data)

            assert response.status_code == 200
            data = response.json()
            assert data["num_players"] == 4

    def test_list_bookings_empty(self, test_client: TestClient) -> None:
        """Test listing bookings when none exist."""
        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_bookings = AsyncMock(return_value=[])

            response = test_client.get("/bookings/")

            assert response.status_code == 200
            assert response.json() == []

    def test_list_bookings_with_filter(self, test_client: TestClient) -> None:
        """Test listing bookings with phone_number filter."""
        sample_booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
            status=BookingStatus.SCHEDULED,
        )

        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_bookings = AsyncMock(return_value=[sample_booking])

            response = test_client.get("/bookings/?phone_number=%2B15551234567")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["phone_number"] == "+15551234567"

    def test_get_booking_exists(self, test_client: TestClient) -> None:
        """Test getting an existing booking."""
        sample_booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
            status=BookingStatus.SCHEDULED,
        )

        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=sample_booking)

            response = test_client.get("/bookings/abc12345")

            assert response.status_code == 200
            data = response.json()
            assert data["id"] == "abc12345"
            assert data["phone_number"] == "+15551234567"

    def test_get_booking_not_found(self, test_client: TestClient) -> None:
        """Test getting a non-existent booking."""
        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=None)

            response = test_client.get("/bookings/nonexistent")

            assert response.status_code == 404
            assert response.json()["detail"] == "Booking not found"

    def test_cancel_booking_success(self, test_client: TestClient) -> None:
        """Test cancelling a booking."""
        sample_booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
            status=BookingStatus.CANCELLED,
        )

        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=sample_booking)
            mock_service.cancel_booking = AsyncMock(return_value=sample_booking)

            response = test_client.delete("/bookings/abc12345?phone_number=%2B15551234567")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "cancelled"
            assert data["booking_id"] == "abc12345"

    def test_cancel_booking_not_found(self, test_client: TestClient) -> None:
        """Test cancelling a non-existent booking."""
        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=None)

            response = test_client.delete("/bookings/nonexistent?phone_number=%2B15551234567")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"]

    def test_cancel_booking_unauthorized(self, test_client: TestClient) -> None:
        """Test cancelling a booking with wrong phone number."""
        sample_booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
            status=BookingStatus.SCHEDULED,
        )

        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=sample_booking)

            response = test_client.delete("/bookings/abc12345?phone_number=%2B15559999999")

            assert response.status_code == 403
            assert "Unauthorized" in response.json()["detail"]

    def test_cancel_booking_cannot_cancel(self, test_client: TestClient) -> None:
        """Test 404 when booking exists and phone matches but status is not cancellable."""
        failed_booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
            status=BookingStatus.FAILED,
        )

        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=failed_booking)
            mock_service.cancel_booking = AsyncMock(return_value=None)

            response = test_client.delete("/bookings/abc12345?phone_number=%2B15551234567")

            assert response.status_code == 404
            assert "cannot be cancelled" in response.json()["detail"]

    def test_execute_booking_success(self, test_client: TestClient) -> None:
        """Test executing a booking."""
        sample_booking = TeeTimeBooking(
            id="abc12345",
            phone_number="+15551234567",
            request=TeeTimeRequest(
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
            ),
            status=BookingStatus.SUCCESS,
            confirmation_number="CONF123",
        )

        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=sample_booking)
            mock_service.execute_booking = AsyncMock(return_value=True)

            response = test_client.post("/bookings/abc12345/execute")

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["booking_id"] == "abc12345"

    def test_execute_booking_not_found(self, test_client: TestClient) -> None:
        """Test executing a non-existent booking."""
        with patch("app.api.bookings.booking_service") as mock_service:
            mock_service.get_booking = AsyncMock(return_value=None)

            response = test_client.post("/bookings/nonexistent/execute")

            assert response.status_code == 404
            assert response.json()["detail"] == "Booking not found"


class TestGetExternalUrl:
    """Tests for the get_external_url helper function."""

    def test_get_external_url_with_forwarded_headers(self) -> None:
        """Test URL reconstruction with X-Forwarded-* headers (Cloud Run scenario)."""
        from unittest.mock import MagicMock

        from app.api.webhooks import get_external_url

        mock_request = MagicMock()
        mock_request.headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "teetime-123.us-central1.run.app",
            "host": "localhost:8080",
        }
        mock_request.url.scheme = "http"
        mock_request.url.netloc = "localhost:8080"
        mock_request.url.path = "/webhooks/twilio/sms"
        mock_request.url.query = ""

        result = get_external_url(mock_request)

        assert result == "https://teetime-123.us-central1.run.app/webhooks/twilio/sms"

    def test_get_external_url_without_forwarded_headers(self) -> None:
        """Test URL reconstruction without forwarded headers (local dev)."""
        from unittest.mock import MagicMock

        from app.api.webhooks import get_external_url

        mock_request = MagicMock()
        mock_request.headers = {"host": "localhost:8000"}
        mock_request.url.scheme = "http"
        mock_request.url.netloc = "localhost:8000"
        mock_request.url.path = "/webhooks/twilio/sms"
        mock_request.url.query = ""

        result = get_external_url(mock_request)

        assert result == "http://localhost:8000/webhooks/twilio/sms"

    def test_get_external_url_with_query_string(self) -> None:
        """Test URL reconstruction preserves query string."""
        from unittest.mock import MagicMock

        from app.api.webhooks import get_external_url

        mock_request = MagicMock()
        mock_request.headers = {"host": "example.com"}
        mock_request.url.scheme = "https"
        mock_request.url.netloc = "example.com"
        mock_request.url.path = "/webhooks/twilio/sms"
        mock_request.url.query = "foo=bar"

        result = get_external_url(mock_request)

        assert result == "https://example.com/webhooks/twilio/sms?foo=bar"


class TestWebhookEndpoints:
    """Tests for Twilio webhook endpoints."""

    def test_incoming_sms_valid(self, test_client: TestClient) -> None:
        """Test handling a valid incoming SMS."""
        with patch("app.api.webhooks.sms_service") as mock_sms:
            mock_sms.validate_request.return_value = True
            mock_sms.send_sms = AsyncMock(return_value="mock_sid")

            with patch("app.api.webhooks.booking_service") as mock_booking:
                mock_booking.handle_incoming_message = AsyncMock(
                    return_value="I'll book that for you!"
                )

                response = test_client.post(
                    "/webhooks/twilio/sms",
                    data={
                        "From": "+15551234567",
                        "To": "+15559999999",
                        "Body": "Book Saturday 8am",
                    },
                )

                assert response.status_code == 200
                mock_booking.handle_incoming_message.assert_called_once_with(
                    "+15551234567", "Book Saturday 8am"
                )
                mock_sms.send_sms.assert_called_once()

    def test_incoming_sms_invalid_signature(self, test_client: TestClient) -> None:
        """Test rejecting SMS with invalid signature."""
        with patch("app.api.webhooks.sms_service") as mock_sms:
            mock_sms.validate_request.return_value = False

            response = test_client.post(
                "/webhooks/twilio/sms",
                data={
                    "From": "+15551234567",
                    "To": "+15559999999",
                    "Body": "Book Saturday 8am",
                },
            )

            assert response.status_code == 403
            assert "Invalid or missing Twilio signature" in response.json()["detail"]

    def test_sms_status_webhook(self, test_client: TestClient) -> None:
        """Test handling SMS status webhook."""
        response = test_client.post(
            "/webhooks/twilio/status",
            data={
                "MessageSid": "SM123456",
                "MessageStatus": "delivered",
                "To": "+15551234567",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "received"

    def test_sms_status_webhook_with_error(self, test_client: TestClient) -> None:
        """Test handling SMS status webhook with error code."""
        response = test_client.post(
            "/webhooks/twilio/status",
            data={
                "MessageSid": "SM123456",
                "MessageStatus": "failed",
                "To": "+15551234567",
                "ErrorCode": "30003",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "received"


class TestBookingsEndpointsIntegration:
    """Integration tests for booking endpoints using mocked database service."""

    def test_create_and_get_booking(self, test_client: TestClient) -> None:
        """Test creating and then retrieving a booking."""
        from datetime import datetime

        import pytz

        request_data = {
            "phone_number": "+15559999999",
            "requested_date": "2025-12-30",
            "requested_time": "10:00:00",
            "num_players": 2,
        }

        created_booking = None

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                nonlocal created_booking
                created_booking = booking
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch("app.services.booking_service.datetime") as mock_datetime:
                tz = pytz.timezone("America/Chicago")
                mock_now = datetime(2025, 12, 22, 10, 0)
                mock_datetime.now.return_value = tz.localize(mock_now)
                mock_datetime.combine = datetime.combine
                mock_datetime.min = datetime.min

                create_response = test_client.post("/bookings/", json=request_data)
                assert create_response.status_code == 200
                booking_id = create_response.json()["id"]

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=created_booking)

            get_response = test_client.get(f"/bookings/{booking_id}")
            assert get_response.status_code == 200
            data = get_response.json()
            assert data["id"] == booking_id
            assert data["phone_number"] == "+15559999999"
            assert data["num_players"] == 2

    def test_create_and_cancel_booking(self, test_client: TestClient) -> None:
        """Test creating and then cancelling a booking."""
        from datetime import datetime

        import pytz

        phone_number = "+15558888888"
        request_data = {
            "phone_number": phone_number,
            "requested_date": "2025-12-30",
            "requested_time": "11:00:00",
        }

        created_booking = None

        with patch("app.services.booking_service.database_service") as mock_db:

            async def create_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                nonlocal created_booking
                created_booking = booking
                return booking

            mock_db.create_booking = AsyncMock(side_effect=create_booking_side_effect)

            with patch("app.services.booking_service.datetime") as mock_datetime:
                tz = pytz.timezone("America/Chicago")
                mock_now = datetime(2025, 12, 22, 10, 0)
                mock_datetime.now.return_value = tz.localize(mock_now)
                mock_datetime.combine = datetime.combine
                mock_datetime.min = datetime.min

                create_response = test_client.post("/bookings/", json=request_data)
                assert create_response.status_code == 200
                booking_id = create_response.json()["id"]

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=created_booking)

            async def update_booking_side_effect(booking: TeeTimeBooking) -> TeeTimeBooking:
                return booking

            mock_db.update_booking = AsyncMock(side_effect=update_booking_side_effect)

            cancel_response = test_client.delete(
                f"/bookings/{booking_id}?phone_number=%2B15558888888"
            )
            assert cancel_response.status_code == 200
            assert cancel_response.json()["status"] == "cancelled"

        with patch("app.services.booking_service.database_service") as mock_db:
            mock_db.get_booking = AsyncMock(return_value=created_booking)

            get_response = test_client.get(f"/bookings/{booking_id}")
            assert get_response.status_code == 200
            assert get_response.json()["status"] == "cancelled"
