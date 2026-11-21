"""
Tests for API endpoints in app/api/.

These tests verify the FastAPI endpoints for health checks and Twilio
webhooks using the TestClient, and pin the set of routes the app exposes.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import __version__


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
        assert data["version"] == __version__
        assert "endpoints" in data
        assert data["endpoints"]["health"] == "/health"
        assert data["endpoints"]["webhooks"] == "/webhooks/twilio/sms"
        assert "bookings" not in data["endpoints"]


class TestRouteSurface:
    """The app exposes only the routes something actually calls (#127).

    The ``/bookings`` REST router was never authenticated, on a service whose
    invoker role is granted to ``allUsers``, and nothing in the bot called it:
    members book through the messaging webhook, and the race is started by
    Cloud Scheduler through ``/jobs``. Pinning the full set means a router
    added later is a deliberate, reviewed change rather than a quiet one.
    """

    def _routes(self) -> set[tuple[str, str]]:
        """Every (method, path) the app serves, including hidden routes.

        Established, by reading the locked fastapi 0.163.1 ``fastapi/routing.py``
        and by listing ``app.router.routes``: ``include_router`` no longer copies
        a router's routes into ``app.routes``. It adds one ``_IncludedRouter``
        per included router, holding the original as ``original_router`` and
        any ``include_router(prefix=...)`` on ``include_context.prefix``. 0.115.14
        did copy them. Not established: which release in between changed it.

        The walk carries inclusion prefixes down, so one router included twice
        under different prefixes does not collapse to one set of paths. It
        fails closed: any route that is neither an ``APIRoute``, an included
        router, nor one of FastAPI's own docs routes (``Mount``, a plain
        ``add_route``, a websocket) raises, rather than escaping the pin.

        The OpenAPI schema is not a substitute: it omits the
        ``include_in_schema=False`` onboarding routes.
        """
        from fastapi.routing import APIRoute
        from starlette.routing import BaseRoute, Route

        from app.main import app

        docs_paths = {
            app.openapi_url,
            app.docs_url,
            app.redoc_url,
            app.swagger_ui_oauth2_redirect_url,
        }

        def walk(routes: list[BaseRoute], prefix: str) -> set[tuple[str, str]]:
            found: set[tuple[str, str]] = set()
            for route in routes:
                if isinstance(route, APIRoute):
                    found.update((method, prefix + route.path) for method in route.methods)
                elif (included := getattr(route, "original_router", None)) is not None:
                    # Direct access: a change in the wrapper's shape fails here.
                    include_prefix = route.include_context.prefix  # type: ignore[attr-defined]
                    found |= walk(included.routes, prefix + include_prefix)
                elif type(route) is Route and not prefix and route.path in docs_paths:
                    continue
                else:
                    raise AssertionError(f"unrecognised route, not pinned: {route!r}")
            return found

        return walk(app.routes, "")

    def test_no_bookings_routes(self) -> None:
        assert not [path for _, path in self._routes() if path.startswith("/bookings")]

    def test_bookings_paths_are_not_found(self, test_client: TestClient) -> None:
        assert test_client.get("/bookings/").status_code == 404
        assert test_client.post("/bookings/", json={}).status_code == 404
        assert test_client.post("/bookings/abc12345/execute").status_code == 404
        assert test_client.delete("/bookings/abc12345").status_code == 404

    def test_the_route_set_is_pinned(self) -> None:
        assert self._routes() == {
            ("GET", "/health"),
            ("GET", "/"),
            ("POST", "/webhooks/twilio/sms"),
            ("POST", "/webhooks/twilio/status"),
            ("POST", "/webhooks/telegram"),
            ("POST", "/jobs/execute-due-bookings"),
            ("POST", "/jobs/purge-expired"),
            ("GET", "/onboarding/walden"),
            ("POST", "/onboarding/walden"),
        }


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
                    "+15551234567", "Book Saturday 8am", channel="twilio"
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


class TestStartupSettlesCancellations:
    """Issue #353: unrecorded cancellations are settled at startup."""

    @pytest.mark.asyncio
    async def test_settling_runs_even_if_reconciling_fails(self) -> None:
        from app.main import _reconcile_after_startup
        from app.services.booking_service import booking_service

        with (
            patch.object(
                booking_service,
                "reconcile_interrupted_bookings",
                AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch.object(booking_service, "settle_cancelling_bookings", AsyncMock()) as settle,
        ):
            await _reconcile_after_startup(None)
        settle.assert_awaited_once()
