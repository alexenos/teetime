"""
Tests for scheduled job endpoints in app/api/jobs.py.

These tests verify the Cloud Scheduler integration endpoint including
authentication, booking execution, timeout handling, and error cases.
"""

from datetime import date, datetime, time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.jobs import JobExecutionItem, JobExecutionResult, JobExecutionStatus
from app.models.schemas import BookingStatus, TeeTimeBooking, TeeTimeRequest


@pytest.fixture
def test_client() -> TestClient:
    """Create a TestClient for the FastAPI app."""
    from app.main import app

    return TestClient(app)


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
def successful_booking(sample_request: TeeTimeRequest) -> TeeTimeBooking:
    """Create a sample successful TeeTimeBooking for testing."""
    return TeeTimeBooking(
        id="test1234",
        phone_number="+15551234567",
        request=sample_request,
        status=BookingStatus.SUCCESS,
        scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        confirmation_number="CONF123",
        actual_booked_time=time(8, 0),
        created_at=datetime(2025, 12, 6, 10, 0),
        updated_at=datetime(2025, 12, 6, 10, 0),
    )


@pytest.fixture
def failed_booking(sample_request: TeeTimeRequest) -> TeeTimeBooking:
    """Create a sample failed TeeTimeBooking for testing."""
    return TeeTimeBooking(
        id="test1234",
        phone_number="+15551234567",
        request=sample_request,
        status=BookingStatus.FAILED,
        scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        error_message="No available slots",
        created_at=datetime(2025, 12, 6, 10, 0),
        updated_at=datetime(2025, 12, 6, 10, 0),
    )


class TestJobsAuthentication:
    """Tests for API key authentication on the jobs endpoint."""

    def test_missing_api_key_returns_422(self, test_client: TestClient) -> None:
        """Test that missing API key returns 422 (Field required)."""
        response = test_client.post("/jobs/execute-due-bookings")

        assert response.status_code == 422

    def test_invalid_api_key_returns_401(self, test_client: TestClient) -> None:
        """Test that invalid API key returns 401."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "correct-key"
            mock_settings.timezone = "America/Chicago"

            response = test_client.post(
                "/jobs/execute-due-bookings",
                headers={"X-Scheduler-API-Key": "wrong-key"},
            )

            assert response.status_code == 401
            assert "Invalid scheduler API key" in response.json()["detail"]

    def test_unconfigured_api_key_returns_500(self, test_client: TestClient) -> None:
        """Test that unconfigured API key on server returns 500."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = None
            mock_settings.timezone = "America/Chicago"

            response = test_client.post(
                "/jobs/execute-due-bookings",
                headers={"X-Scheduler-API-Key": "any-key"},
            )

            assert response.status_code == 500
            assert "Scheduler API key not configured" in response.json()["detail"]

    def test_valid_api_key_succeeds(self, test_client: TestClient) -> None:
        """Test that valid API key allows access."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[])

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"X-Scheduler-API-Key": "test-api-key"},
                )

                assert response.status_code == 200


class TestJobsExecuteDueBookings:
    """Tests for the execute-due-bookings endpoint."""

    def test_no_due_bookings_returns_empty_results(self, test_client: TestClient) -> None:
        """Test that no due bookings returns empty results."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[])

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"X-Scheduler-API-Key": "test-api-key"},
                )

                assert response.status_code == 200
                data = response.json()
                assert data["total_due"] == 0
                assert data["succeeded"] == 0
                assert data["failed"] == 0
                assert data["results"] == []
                assert "executed_at" in data

    def test_successful_booking_execution(
        self,
        test_client: TestClient,
        sample_booking: TeeTimeBooking,
        successful_booking: TeeTimeBooking,
    ) -> None:
        """Test successful execution of a due booking."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[sample_booking])
                mock_service.execute_booking = AsyncMock(return_value=True)
                mock_service.get_booking = AsyncMock(return_value=successful_booking)

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"X-Scheduler-API-Key": "test-api-key"},
                )

                assert response.status_code == 200
                data = response.json()
                assert data["total_due"] == 1
                assert data["succeeded"] == 1
                assert data["failed"] == 0
                assert len(data["results"]) == 1
                assert data["results"][0]["booking_id"] == "test1234"
                assert data["results"][0]["status"] == "success"
                assert data["results"][0]["confirmation_number"] == "CONF123"
                assert data["results"][0]["requested_date"] == "2025-12-20"
                assert data["results"][0]["requested_time"] == "08:00:00"

    def test_failed_booking_execution(
        self,
        test_client: TestClient,
        sample_booking: TeeTimeBooking,
        failed_booking: TeeTimeBooking,
    ) -> None:
        """Test failed execution of a due booking."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[sample_booking])
                mock_service.execute_booking = AsyncMock(return_value=False)
                mock_service.get_booking = AsyncMock(return_value=failed_booking)

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"X-Scheduler-API-Key": "test-api-key"},
                )

                assert response.status_code == 200
                data = response.json()
                assert data["total_due"] == 1
                assert data["succeeded"] == 0
                assert data["failed"] == 1
                assert len(data["results"]) == 1
                assert data["results"][0]["booking_id"] == "test1234"
                assert data["results"][0]["status"] == "failed"
                assert data["results"][0]["error"] == "No available slots"

    def test_booking_execution_exception(
        self,
        test_client: TestClient,
        sample_booking: TeeTimeBooking,
    ) -> None:
        """Test exception during booking execution."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[sample_booking])
                mock_service.execute_booking = AsyncMock(side_effect=Exception("Selenium crashed"))

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"X-Scheduler-API-Key": "test-api-key"},
                )

                assert response.status_code == 200
                data = response.json()
                assert data["total_due"] == 1
                assert data["succeeded"] == 0
                assert data["failed"] == 1
                assert len(data["results"]) == 1
                assert data["results"][0]["booking_id"] == "test1234"
                assert data["results"][0]["status"] == "error"
                assert "Selenium crashed" in data["results"][0]["error"]

    def test_multiple_bookings_mixed_results(
        self,
        test_client: TestClient,
        sample_request: TeeTimeRequest,
    ) -> None:
        """Test execution of multiple bookings with mixed results."""
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
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )
        booking3 = TeeTimeBooking(
            id="booking3",
            phone_number="+15553333333",
            request=sample_request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
        )

        successful_booking1 = TeeTimeBooking(
            id="booking1",
            phone_number="+15551111111",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            confirmation_number="CONF001",
        )
        failed_booking2 = TeeTimeBooking(
            id="booking2",
            phone_number="+15552222222",
            request=sample_request,
            status=BookingStatus.FAILED,
            error_message="Slot taken",
        )
        successful_booking3 = TeeTimeBooking(
            id="booking3",
            phone_number="+15553333333",
            request=sample_request,
            status=BookingStatus.SUCCESS,
            confirmation_number="CONF003",
        )

        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(
                    return_value=[booking1, booking2, booking3]
                )
                mock_service.execute_booking = AsyncMock(side_effect=[True, False, True])
                mock_service.get_booking = AsyncMock(
                    side_effect=[
                        successful_booking1,
                        failed_booking2,
                        successful_booking3,
                    ]
                )

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"X-Scheduler-API-Key": "test-api-key"},
                )

                assert response.status_code == 200
                data = response.json()
                assert data["total_due"] == 3
                assert data["succeeded"] == 2
                assert data["failed"] == 1
                assert len(data["results"]) == 3

                results_by_id = {r["booking_id"]: r for r in data["results"]}
                assert results_by_id["booking1"]["status"] == "success"
                assert results_by_id["booking2"]["status"] == "failed"
                assert results_by_id["booking3"]["status"] == "success"


class TestJobsTimeout:
    """Tests for booking execution timeout handling."""

    def test_booking_timeout_handled(
        self,
        test_client: TestClient,
        sample_booking: TeeTimeBooking,
    ) -> None:
        """Test that booking timeout is properly handled."""
        import asyncio

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)
            return True

        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[sample_booking])
                mock_service.execute_booking = slow_execute

                with patch("app.api.jobs.BOOKING_EXECUTION_TIMEOUT_SECONDS", 0.1):
                    response = test_client.post(
                        "/jobs/execute-due-bookings",
                        headers={"X-Scheduler-API-Key": "test-api-key"},
                    )

                    assert response.status_code == 200
                    data = response.json()
                    assert data["total_due"] == 1
                    assert data["succeeded"] == 0
                    assert data["failed"] == 1
                    assert len(data["results"]) == 1
                    assert data["results"][0]["status"] == "timeout"
                    assert "timed out" in data["results"][0]["error"]


class TestJobExecutionModels:
    """Tests for Pydantic models used in job execution."""

    def test_job_execution_status_enum_values(self) -> None:
        """Test JobExecutionStatus enum has expected values."""
        assert JobExecutionStatus.SUCCESS == "success"
        assert JobExecutionStatus.FAILED == "failed"
        assert JobExecutionStatus.TIMEOUT == "timeout"
        assert JobExecutionStatus.ERROR == "error"

    def test_job_execution_item_success(self) -> None:
        """Test JobExecutionItem for successful booking."""
        item = JobExecutionItem(
            booking_id="test123",
            status=JobExecutionStatus.SUCCESS,
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
            confirmation_number="CONF123",
        )

        assert item.booking_id == "test123"
        assert item.status == JobExecutionStatus.SUCCESS
        assert item.requested_date == date(2025, 12, 20)
        assert item.requested_time == time(8, 0)
        assert item.confirmation_number == "CONF123"
        assert item.error is None

    def test_job_execution_item_failure(self) -> None:
        """Test JobExecutionItem for failed booking."""
        item = JobExecutionItem(
            booking_id="test123",
            status=JobExecutionStatus.FAILED,
            requested_date=date(2025, 12, 20),
            requested_time=time(8, 0),
            error="No slots available",
        )

        assert item.status == JobExecutionStatus.FAILED
        assert item.error == "No slots available"
        assert item.confirmation_number is None

    def test_job_execution_result(self) -> None:
        """Test JobExecutionResult model."""
        now = datetime(2025, 12, 13, 6, 30)
        items = [
            JobExecutionItem(
                booking_id="test1",
                status=JobExecutionStatus.SUCCESS,
                requested_date=date(2025, 12, 20),
                requested_time=time(8, 0),
                confirmation_number="CONF1",
            ),
            JobExecutionItem(
                booking_id="test2",
                status=JobExecutionStatus.FAILED,
                requested_date=date(2025, 12, 21),
                requested_time=time(9, 0),
                error="Slot taken",
            ),
        ]

        result = JobExecutionResult(
            executed_at=now,
            total_due=2,
            succeeded=1,
            failed=1,
            results=items,
        )

        assert result.executed_at == now
        assert result.total_due == 2
        assert result.succeeded == 1
        assert result.failed == 1
        assert len(result.results) == 2
