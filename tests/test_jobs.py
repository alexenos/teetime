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


class TestOIDCAuthentication:
    """Tests for OIDC token authentication on the jobs endpoint."""

    def test_oidc_auth_fails_when_audience_not_configured(self, test_client: TestClient) -> None:
        """Test that OIDC auth fails when OIDC_AUDIENCE is not configured."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = ""
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = ""

            response = test_client.post(
                "/jobs/execute-due-bookings",
                headers={"Authorization": "Bearer fake-token"},
            )

            assert response.status_code == 401
            assert "Authentication failed" in response.json()["detail"]

    def test_oidc_auth_fails_with_invalid_bearer_format(self, test_client: TestClient) -> None:
        """Test that OIDC auth fails when Authorization header doesn't start with Bearer."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = "https://test.run.app"
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = ""

            response = test_client.post(
                "/jobs/execute-due-bookings",
                headers={"Authorization": "Basic fake-token"},
            )

            assert response.status_code == 401

    def test_oidc_auth_fails_with_invalid_token(self, test_client: TestClient) -> None:
        """Test that OIDC auth fails when token verification fails."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = "https://test.run.app"
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = ""

            with patch("app.api.jobs.id_token.verify_oauth2_token") as mock_verify:
                from google.auth import exceptions as google_auth_exceptions

                mock_verify.side_effect = google_auth_exceptions.GoogleAuthError("Invalid token")

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"Authorization": "Bearer invalid-token"},
                )

                assert response.status_code == 401

    def test_oidc_auth_fails_with_wrong_service_account(self, test_client: TestClient) -> None:
        """Test that OIDC auth fails when service account doesn't match."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = "https://test.run.app"
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = "expected@project.iam.gserviceaccount.com"

            with patch("app.api.jobs.id_token.verify_oauth2_token") as mock_verify:
                mock_verify.return_value = {"email": "wrong@project.iam.gserviceaccount.com"}

                response = test_client.post(
                    "/jobs/execute-due-bookings",
                    headers={"Authorization": "Bearer valid-token"},
                )

                assert response.status_code == 401

    def test_oidc_auth_succeeds_with_valid_token(self, test_client: TestClient) -> None:
        """Test that OIDC auth succeeds with valid token and matching service account."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = "https://test.run.app"
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = "scheduler@project.iam.gserviceaccount.com"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.id_token.verify_oauth2_token") as mock_verify:
                mock_verify.return_value = {"email": "scheduler@project.iam.gserviceaccount.com"}

                with patch("app.api.jobs.booking_service") as mock_service:
                    mock_service.get_due_bookings = AsyncMock(return_value=[])

                    response = test_client.post(
                        "/jobs/execute-due-bookings",
                        headers={"Authorization": "Bearer valid-token"},
                    )

                    assert response.status_code == 200
                    mock_verify.assert_called_once()
                    call_args = mock_verify.call_args
                    assert call_args[0][0] == "valid-token"
                    assert call_args[1]["audience"] == "https://test.run.app"

    def test_oidc_auth_succeeds_without_service_account_check(
        self, test_client: TestClient
    ) -> None:
        """Test that OIDC auth succeeds when scheduler_service_account is not configured."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = "https://test.run.app"
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = ""
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.id_token.verify_oauth2_token") as mock_verify:
                mock_verify.return_value = {"email": "any@project.iam.gserviceaccount.com"}

                with patch("app.api.jobs.booking_service") as mock_service:
                    mock_service.get_due_bookings = AsyncMock(return_value=[])

                    response = test_client.post(
                        "/jobs/execute-due-bookings",
                        headers={"Authorization": "Bearer valid-token"},
                    )

                    assert response.status_code == 200

    def test_oidc_audience_trailing_slash_stripped(self, test_client: TestClient) -> None:
        """Test that trailing slash is stripped from OIDC_AUDIENCE."""
        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.oidc_audience = "https://test.run.app/"
            mock_settings.scheduler_api_key = ""
            mock_settings.scheduler_service_account = ""
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.id_token.verify_oauth2_token") as mock_verify:
                mock_verify.return_value = {"email": "scheduler@project.iam.gserviceaccount.com"}

                with patch("app.api.jobs.booking_service") as mock_service:
                    mock_service.get_due_bookings = AsyncMock(return_value=[])

                    response = test_client.post(
                        "/jobs/execute-due-bookings",
                        headers={"Authorization": "Bearer valid-token"},
                    )

                    assert response.status_code == 200
                    call_args = mock_verify.call_args
                    assert call_args[1]["audience"] == "https://test.run.app"


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
                mock_service.execute_bookings_batch = AsyncMock(return_value=[("test1234", True)])
                mock_service.get_booking = AsyncMock(return_value=successful_booking)

                with patch("app.api.jobs.sms_service") as mock_sms:
                    mock_sms.send_booking_confirmation = AsyncMock()

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
                mock_service.execute_bookings_batch = AsyncMock(return_value=[("test1234", False)])
                mock_service.get_booking = AsyncMock(return_value=failed_booking)

                with patch("app.api.jobs.sms_service") as mock_sms:
                    mock_sms.send_booking_failure = AsyncMock()

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
                mock_service.execute_bookings_batch = AsyncMock(
                    side_effect=Exception("Selenium crashed")
                )

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
                mock_service.execute_bookings_batch = AsyncMock(
                    return_value=[
                        ("booking1", True),
                        ("booking2", False),
                        ("booking3", True),
                    ]
                )
                mock_service.get_booking = AsyncMock(
                    side_effect=[
                        successful_booking1,
                        failed_booking2,
                        successful_booking3,
                    ]
                )

                with patch("app.api.jobs.sms_service") as mock_sms:
                    mock_sms.send_booking_confirmation = AsyncMock()
                    mock_sms.send_booking_failure = AsyncMock()

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
        """Test that booking timeout is properly handled.

        Uses deterministic mocking of asyncio.wait_for to raise TimeoutError
        immediately, avoiding flaky timing-dependent tests.
        """

        async def mock_wait_for(coro, timeout):
            coro.close()
            raise TimeoutError()

        with patch("app.api.jobs.settings") as mock_settings:
            mock_settings.scheduler_api_key = "test-api-key"
            mock_settings.timezone = "America/Chicago"

            with patch("app.api.jobs.booking_service") as mock_service:
                mock_service.get_due_bookings = AsyncMock(return_value=[sample_booking])
                mock_service.execute_bookings_batch = AsyncMock(return_value=[("test1234", True)])

                with patch("asyncio.wait_for", mock_wait_for):
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


class TestJobsIntegration:
    """Integration-style tests that exercise the real booking_service -> database_service chain.

    These tests verify the actual call chain from jobs.py through to the database layer,
    without mocking the intermediate services. Only the reservation provider is mocked
    since we don't want to make real HTTP calls to the golf booking website.
    """

    @pytest.mark.asyncio
    async def test_get_due_bookings_integration(self) -> None:
        """Test that jobs.py correctly passes timezone-aware time through the service chain.

        This test exercises the real path:
        jobs.py (creates tz-aware now) -> booking_service.get_due_bookings(now)
        -> database_service.get_due_bookings(naive_time)

        Verifies that timezone stripping happens correctly and the database query works.
        """
        import pytz
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from app.models.database import Base
        from app.services.booking_service import BookingService
        from app.services.database_service import DatabaseService

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        test_session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.services.database_service.AsyncSessionLocal", test_session_local):
            db_service = DatabaseService()

            due_booking = TeeTimeBooking(
                id="integration-due",
                phone_number="+15551234567",
                request=TeeTimeRequest(
                    requested_date=date(2025, 12, 20),
                    requested_time=time(8, 0),
                    num_players=4,
                ),
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
            )
            future_booking = TeeTimeBooking(
                id="integration-future",
                phone_number="+15559999999",
                request=TeeTimeRequest(
                    requested_date=date(2025, 12, 25),
                    requested_time=time(9, 0),
                    num_players=2,
                ),
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 18, 6, 30),
            )

            await db_service.create_booking(due_booking)
            await db_service.create_booking(future_booking)

            with patch("app.services.booking_service.database_service", db_service):
                booking_svc = BookingService()

                tz = pytz.timezone("America/Chicago")
                current_time = tz.localize(datetime(2025, 12, 13, 6, 31))

                result = await booking_svc.get_due_bookings(current_time)

                assert len(result) == 1
                assert result[0].id == "integration-due"

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_execute_booking_transitions_to_in_progress_before_provider(
        self,
    ) -> None:
        """Test that execute_booking sets status to IN_PROGRESS before calling provider.

        This verifies the idempotency claim in the docstring: bookings are transitioned
        to IN_PROGRESS before execution, so retries won't re-execute started bookings.
        """
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from app.models.database import Base
        from app.providers.base import BookingResult
        from app.services.booking_service import BookingService
        from app.services.database_service import DatabaseService

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        test_session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        status_during_provider_call = None

        async def capture_status_provider(*args, **kwargs):
            nonlocal status_during_provider_call
            booking = await db_service.get_booking("test-idempotency")
            status_during_provider_call = booking.status if booking else None
            return BookingResult(
                success=True,
                booked_time=time(8, 0),
                confirmation_number="CONF123",
            )

        with patch("app.services.database_service.AsyncSessionLocal", test_session_local):
            db_service = DatabaseService()

            booking = TeeTimeBooking(
                id="test-idempotency",
                phone_number="+15551234567",
                request=TeeTimeRequest(
                    requested_date=date(2025, 12, 20),
                    requested_time=time(8, 0),
                    num_players=4,
                ),
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
            )
            await db_service.create_booking(booking)

            with patch("app.services.booking_service.database_service", db_service):
                with patch("app.services.booking_service.sms_service") as mock_sms:
                    mock_sms.send_booking_confirmation = AsyncMock()

                    booking_svc = BookingService()

                    mock_provider = AsyncMock()
                    mock_provider.book_tee_time = capture_status_provider
                    booking_svc.set_reservation_provider(mock_provider)

                    result = await booking_svc.execute_booking("test-idempotency")

                    assert result is True
                    assert status_during_provider_call == BookingStatus.IN_PROGRESS

                    final_booking = await db_service.get_booking("test-idempotency")
                    assert final_booking is not None
                    assert final_booking.status == BookingStatus.SUCCESS

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_get_due_bookings_excludes_in_progress(self) -> None:
        """Test that get_due_bookings excludes IN_PROGRESS bookings.

        This verifies that if a booking is already being executed (IN_PROGRESS),
        it won't be returned by get_due_bookings, preventing duplicate execution
        on Cloud Scheduler retries.
        """
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from app.models.database import Base
        from app.services.database_service import DatabaseService

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        test_session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.services.database_service.AsyncSessionLocal", test_session_local):
            db_service = DatabaseService()

            scheduled_booking = TeeTimeBooking(
                id="scheduled-booking",
                phone_number="+15551234567",
                request=TeeTimeRequest(
                    requested_date=date(2025, 12, 20),
                    requested_time=time(8, 0),
                    num_players=4,
                ),
                status=BookingStatus.SCHEDULED,
                scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
            )
            in_progress_booking = TeeTimeBooking(
                id="in-progress-booking",
                phone_number="+15559999999",
                request=TeeTimeRequest(
                    requested_date=date(2025, 12, 20),
                    requested_time=time(9, 0),
                    num_players=2,
                ),
                status=BookingStatus.IN_PROGRESS,
                scheduled_execution_time=datetime(2025, 12, 13, 6, 30),
            )

            await db_service.create_booking(scheduled_booking)
            await db_service.create_booking(in_progress_booking)

            due_before = datetime(2025, 12, 13, 6, 31)
            result = await db_service.get_due_bookings(due_before)

            assert len(result) == 1
            assert result[0].id == "scheduled-booking"

        await engine.dispose()
