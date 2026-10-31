"""
Tests for GeminiService in app/services/gemini_service.py.

These tests verify the LLM-powered message parsing and intent extraction
functionality, including the mock fallback when the API is not configured.
"""

import asyncio
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.config import settings
from app.services.gemini_service import (
    BILLING_ALERT_INTERVAL,
    GEMINI_MAX_ATTEMPTS,
    GEMINI_REQUEST_TIMEOUT,
    SYSTEM_PROMPT,
    GeminiService,
    is_billing_exhausted,
    is_transient,
)


def api_error(code: int, message: str) -> genai_errors.APIError:
    """A google-genai APIError as the SDK raises it for an HTTP failure."""
    return genai_errors.APIError(code, {"error": {"code": code, "message": message}})


def _response(args: dict) -> genai_types.GenerateContentResponse:
    """A google-genai response whose one part calls parse_tee_time_request."""
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(
                                name="parse_tee_time_request", args=args
                            )
                        )
                    ],
                )
            )
        ]
    )


# The error text Gemini returned on 2026-10-28, from the service's logs.
DEPLETED = (
    "Your prepayment credits are depleted. Please go to AI Studio at "
    "https://ai.studio/projects to manage your project and billing. Learn more at "
    "https://ai.google.dev/gemini-api/docs/billing#prepay."
)


@pytest.fixture
def gemini_service() -> GeminiService:
    """Create a fresh GeminiService instance for each test."""
    return GeminiService()


class TestGeminiServiceResolveRelativeDate:
    """Tests for the _resolve_relative_date method."""

    def test_resolve_today(self, gemini_service: GeminiService) -> None:
        """Test resolving 'today'."""
        # Use a fixed date to avoid flakiness at midnight
        fixed_now = datetime(2025, 6, 15, 12, 0, 0)  # A Sunday
        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._resolve_relative_date("today")
            assert result == date(2025, 6, 15)

    def test_resolve_tomorrow(self, gemini_service: GeminiService) -> None:
        """Test resolving 'tomorrow'."""
        # Use a fixed date to avoid flakiness at midnight
        fixed_now = datetime(2025, 6, 15, 12, 0, 0)  # A Sunday
        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._resolve_relative_date("tomorrow")
            assert result == date(2025, 6, 16)

    def test_resolve_day_of_week(self, gemini_service: GeminiService) -> None:
        """Test resolving day of week names."""
        # Use a fixed date (Sunday June 15, 2025) to make tests deterministic
        fixed_now = datetime(2025, 6, 15, 12, 0, 0)  # A Sunday
        days_expected = {
            "monday": date(2025, 6, 16),
            "tuesday": date(2025, 6, 17),
            "wednesday": date(2025, 6, 18),
            "thursday": date(2025, 6, 19),
            "friday": date(2025, 6, 20),
            "saturday": date(2025, 6, 21),
            "sunday": date(2025, 6, 22),  # Next Sunday, not today
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            for day_name, expected_date in days_expected.items():
                result = gemini_service._resolve_relative_date(day_name)
                assert result is not None
                assert (
                    result == expected_date
                ), f"Expected {expected_date} for {day_name}, got {result}"
                assert result.strftime("%A").lower() == day_name

    def test_resolve_day_of_week_case_insensitive(self, gemini_service: GeminiService) -> None:
        """Test that day of week resolution is case insensitive."""
        # Use a fixed date to avoid flakiness
        fixed_now = datetime(2025, 6, 15, 12, 0, 0)
        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result_lower = gemini_service._resolve_relative_date("saturday")
            result_upper = gemini_service._resolve_relative_date("Saturday")
            result_mixed = gemini_service._resolve_relative_date("SATURDAY")

            assert result_lower == result_upper == result_mixed

    def test_resolve_iso_date(self, gemini_service: GeminiService) -> None:
        """Test resolving ISO format date."""
        result = gemini_service._resolve_relative_date("2025-12-20")
        assert result == date(2025, 12, 20)

    def test_resolve_invalid_date(self, gemini_service: GeminiService) -> None:
        """Test that invalid date strings return None."""
        result = gemini_service._resolve_relative_date("invalid")
        assert result is None

    def test_resolve_empty_string(self, gemini_service: GeminiService) -> None:
        """Test that empty string returns None."""
        result = gemini_service._resolve_relative_date("")
        assert result is None

    def test_resolve_mm_dd_format(self, gemini_service: GeminiService) -> None:
        """Test resolving MM/DD format dates."""
        fixed_now = datetime(2026, 1, 24, 12, 0, 0)
        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._resolve_relative_date("2/1")
            assert result == date(2026, 2, 1)

    def test_resolve_mm_dd_yyyy_format(self, gemini_service: GeminiService) -> None:
        """Test resolving MM/DD/YYYY format dates."""
        result = gemini_service._resolve_relative_date("02/01/2026")
        assert result == date(2026, 2, 1)

    def test_resolve_month_day_format(self, gemini_service: GeminiService) -> None:
        """Test resolving 'Month DD' format dates."""
        fixed_now = datetime(2026, 1, 24, 12, 0, 0)
        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._resolve_relative_date("February 1")
            assert result == date(2026, 2, 1)

    def test_resolve_month_day_year_format(self, gemini_service: GeminiService) -> None:
        """Test resolving 'Month DD, YYYY' format dates."""
        result = gemini_service._resolve_relative_date("February 1, 2026")
        assert result == date(2026, 2, 1)

    def test_resolve_abbreviated_month_format(self, gemini_service: GeminiService) -> None:
        """Test resolving 'Mon DD' format dates (abbreviated month)."""
        fixed_now = datetime(2026, 1, 24, 12, 0, 0)
        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._resolve_relative_date("Feb 1")
            assert result == date(2026, 2, 1)


class TestGeminiServiceParseTime:
    """Tests for the _parse_time method."""

    def test_parse_24h_format(self, gemini_service: GeminiService) -> None:
        """Test parsing 24-hour time format."""
        result = gemini_service._parse_time("14:30")
        assert result == time(14, 30)

    def test_parse_24h_format_with_seconds(self, gemini_service: GeminiService) -> None:
        """Test parsing 24-hour time format with seconds."""
        result = gemini_service._parse_time("14:30:00")
        assert result == time(14, 30, 0)

    def test_parse_morning_time(self, gemini_service: GeminiService) -> None:
        """Test parsing morning time."""
        result = gemini_service._parse_time("08:00")
        assert result == time(8, 0)

    def test_parse_single_digit_hour(self, gemini_service: GeminiService) -> None:
        """Test parsing time with single digit hour."""
        result = gemini_service._parse_time("8:58")
        assert result == time(8, 58)

    def test_parse_12h_format_with_am(self, gemini_service: GeminiService) -> None:
        """Test parsing 12-hour time with AM."""
        result = gemini_service._parse_time("8:58 AM")
        assert result == time(8, 58)

    def test_parse_12h_format_with_pm(self, gemini_service: GeminiService) -> None:
        """Test parsing 12-hour time with PM."""
        result = gemini_service._parse_time("2:30 PM")
        assert result == time(14, 30)

    def test_parse_12h_format_no_space(self, gemini_service: GeminiService) -> None:
        """Test parsing 12-hour time without space before AM/PM."""
        assert gemini_service._parse_time("8:58AM") == time(8, 58)
        assert gemini_service._parse_time("2:30PM") == time(14, 30)

    def test_parse_12h_format_single_letter_suffix(self, gemini_service: GeminiService) -> None:
        """Test parsing 12-hour time with single letter suffix (8:58a, 9:06p)."""
        assert gemini_service._parse_time("8:58a") == time(8, 58)
        assert gemini_service._parse_time("9:06a") == time(9, 6)
        assert gemini_service._parse_time("2:30p") == time(14, 30)

    def test_parse_12h_format_lowercase(self, gemini_service: GeminiService) -> None:
        """Test parsing 12-hour time with lowercase am/pm."""
        assert gemini_service._parse_time("8:58 am") == time(8, 58)
        assert gemini_service._parse_time("2:30 pm") == time(14, 30)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1:15", time(13, 15)),  # no am/pm: only the afternoon is a tee time
            ("01:15", time(13, 15)),  # padded too (maintainer, 2026-10-29)
            ("6:00", time(18, 0)),
            ("12:08", time(12, 8)),
            ("07:00", time(7, 0)),
        ],
    )
    def test_parse_time_without_am_pm_is_read_within_tee_sheet_hours(
        self, gemini_service: GeminiService, raw: str, expected: time
    ) -> None:
        assert gemini_service._parse_time(raw) == expected

    @pytest.mark.parametrize("raw", ["12:08 am", "00:08", "6:45 am", "6:30 pm", "19:00", "6:30"])
    def test_parse_time_outside_tee_sheet_hours_is_none(
        self, gemini_service: GeminiService, raw: str
    ) -> None:
        """Holds Gemini to the 7am-6pm rule whatever time the model returns."""
        assert gemini_service._parse_time(raw) is None

    def test_parse_invalid_time(self, gemini_service: GeminiService) -> None:
        """Test that invalid time strings return None."""
        result = gemini_service._parse_time("invalid")
        assert result is None

    def test_parse_empty_string(self, gemini_service: GeminiService) -> None:
        """Test that empty string returns None."""
        result = gemini_service._parse_time("")
        assert result is None


class TestGeminiServiceBuildParsedIntent:
    """Tests for the _build_parsed_intent method."""

    def test_build_book_intent_complete(self, gemini_service: GeminiService) -> None:
        """Test building a complete book intent."""
        # Use a fixed date in the past relative to a mocked "today" to test the safety check
        # doesn't incorrectly adjust future dates
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)  # Mock today as Dec 15, 2025
        args = {
            "intent": "book",
            "requested_date": "2025-12-20",
            "requested_time": "08:00",
            "num_players": 4,
            "response_message": "I'll book that for you!",
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is not None
        assert result.tee_time_request.requested_date == date(2025, 12, 20)
        assert result.tee_time_request.requested_time == time(8, 0)
        assert result.tee_time_request.num_players == 4
        assert result.response_message == "I'll book that for you!"

    def test_build_book_intent_with_relative_date(self, gemini_service: GeminiService) -> None:
        """Test building a book intent with relative date."""
        args = {
            "intent": "book",
            "requested_date": "saturday",
            "requested_time": "09:30",
            "response_message": "Booking for Saturday!",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is not None
        assert result.tee_time_request.requested_date.strftime("%A") == "Saturday"

    def test_build_book_intent_missing_date(self, gemini_service: GeminiService) -> None:
        """Test building a book intent without date."""
        args = {
            "intent": "book",
            "requested_time": "08:00",
            "response_message": "What date?",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is None

    def test_build_book_intent_missing_time(self, gemini_service: GeminiService) -> None:
        """Test building a book intent without time."""
        args = {
            "intent": "book",
            "requested_date": "2025-12-20",
            "response_message": "What time?",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is None

    def test_build_status_intent(self, gemini_service: GeminiService) -> None:
        """Test building a status intent."""
        args = {
            "intent": "status",
            "response_message": "Checking your bookings...",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.intent == "status"
        assert result.tee_time_request is None
        assert result.response_message == "Checking your bookings..."

    def test_build_intent_with_clarification(self, gemini_service: GeminiService) -> None:
        """Test building an intent with clarification needed."""
        args = {
            "intent": "unclear",
            "clarification_needed": "What date would you like?",
            "response_message": "I need more info.",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.intent == "unclear"
        assert result.clarification_needed == "What date would you like?"

    def test_build_intent_default_num_players(self, gemini_service: GeminiService) -> None:
        """Test that num_players defaults to 4."""
        args = {
            "intent": "book",
            "requested_date": "2025-12-20",
            "requested_time": "08:00",
            "response_message": "Booking!",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.tee_time_request is not None
        assert result.tee_time_request.num_players == 4

    def test_build_book_intent_multiple_bookings(self, gemini_service: GeminiService) -> None:
        """Test building a book intent with multiple bookings in the bookings array."""
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)
        args = {
            "intent": "book",
            "bookings": [
                {"requested_date": "2025-12-20", "requested_time": "08:00", "num_players": 4},
                {"requested_date": "2025-12-21", "requested_time": "09:00", "num_players": 2},
            ],
            "response_message": "I'll book both tee times for you!",
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is None
        assert result.tee_time_requests is not None
        assert len(result.tee_time_requests) == 2
        assert result.tee_time_requests[0].requested_date == date(2025, 12, 20)
        assert result.tee_time_requests[0].requested_time == time(8, 0)
        assert result.tee_time_requests[0].num_players == 4
        assert result.tee_time_requests[1].requested_date == date(2025, 12, 21)
        assert result.tee_time_requests[1].requested_time == time(9, 0)
        assert result.tee_time_requests[1].num_players == 2

    def test_build_book_intent_single_booking_in_array(self, gemini_service: GeminiService) -> None:
        """Test that a single booking in the bookings array uses tee_time_request field."""
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)
        args = {
            "intent": "book",
            "bookings": [
                {"requested_date": "2025-12-20", "requested_time": "08:00", "num_players": 4},
            ],
            "response_message": "I'll book that for you!",
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is not None
        assert result.tee_time_requests is None
        assert result.tee_time_request.requested_date == date(2025, 12, 20)

    def test_build_book_intent_empty_bookings_array(self, gemini_service: GeminiService) -> None:
        """Test that an empty bookings array falls back to legacy fields."""
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)
        args = {
            "intent": "book",
            "bookings": [],
            "requested_date": "2025-12-20",
            "requested_time": "08:00",
            "response_message": "I'll book that for you!",
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_request is not None
        assert result.tee_time_requests is None

    def test_build_book_intent_multiple_bookings_with_invalid_entry(
        self, gemini_service: GeminiService
    ) -> None:
        """Test that invalid entries in bookings array are skipped."""
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)
        args = {
            "intent": "book",
            "bookings": [
                {"requested_date": "2025-12-20", "requested_time": "08:00", "num_players": 4},
                {"requested_date": "invalid", "requested_time": "invalid"},
                {"requested_date": "2025-12-21", "requested_time": "09:00", "num_players": 2},
            ],
            "response_message": "I'll book the valid tee times!",
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_requests is not None
        assert len(result.tee_time_requests) == 2

    def test_build_book_intent_multiple_bookings_default_num_players(
        self, gemini_service: GeminiService
    ) -> None:
        """Test that num_players defaults to 4 in bookings array."""
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)
        args = {
            "intent": "book",
            "bookings": [
                {"requested_date": "2025-12-20", "requested_time": "08:00"},
            ],
            "response_message": "Booking!",
        }

        with patch("app.services.gemini_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            result = gemini_service._build_parsed_intent(args)

        assert result.tee_time_request is not None
        assert result.tee_time_request.num_players == 4

    def test_build_book_intent_multiple_bookings_with_relative_dates(
        self, gemini_service: GeminiService
    ) -> None:
        """Test building multiple bookings with relative dates like 'saturday'."""
        args = {
            "intent": "book",
            "bookings": [
                {"requested_date": "saturday", "requested_time": "08:00", "num_players": 4},
                {"requested_date": "sunday", "requested_time": "09:00", "num_players": 4},
            ],
            "response_message": "I'll book both weekend tee times!",
        }

        result = gemini_service._build_parsed_intent(args)

        assert result.intent == "book"
        assert result.tee_time_requests is not None
        assert len(result.tee_time_requests) == 2
        assert result.tee_time_requests[0].requested_date.strftime("%A") == "Saturday"
        assert result.tee_time_requests[1].requested_date.strftime("%A") == "Sunday"


class TestGeminiServiceMockParse:
    """Tests for the _mock_parse fallback method."""

    def test_mock_parse_book_saturday(self, gemini_service: GeminiService) -> None:
        """Test mock parsing a Saturday booking request."""
        result = gemini_service._mock_parse("Book Saturday 8am for 4 players")

        assert result.intent == "book"
        assert result.tee_time_request is not None
        assert result.tee_time_request.requested_date.strftime("%A") == "Saturday"

    def test_mock_parse_book_generic(self, gemini_service: GeminiService) -> None:
        """Test mock parsing a generic booking request."""
        result = gemini_service._mock_parse("I want to reserve a tee time")

        assert result.intent == "book"
        assert result.tee_time_request is not None

    def test_mock_parse_status(self, gemini_service: GeminiService) -> None:
        """Test mock parsing a status request."""
        result = gemini_service._mock_parse("What is my status")

        assert result.intent == "status"

    def test_mock_parse_cancel(self, gemini_service: GeminiService) -> None:
        """Test mock parsing a cancel request."""
        result = gemini_service._mock_parse("Cancel my reservation")

        assert result.intent == "cancel"

    def test_mock_parse_help(self, gemini_service: GeminiService) -> None:
        """Test mock parsing a help request."""
        result = gemini_service._mock_parse("How do I use this?")

        assert result.intent == "help"

    def test_mock_parse_confirm(self, gemini_service: GeminiService) -> None:
        """Test mock parsing confirmation responses."""
        for confirm_word in ["yes", "confirm", "ok", "sure", "yeah"]:
            result = gemini_service._mock_parse(confirm_word)
            assert result.intent == "confirm"

    def test_mock_parse_unclear(self, gemini_service: GeminiService) -> None:
        """Test mock parsing an unclear message."""
        result = gemini_service._mock_parse("asdfghjkl")

        assert result.intent == "unclear"

    def test_mock_parse_extracts_player_count(self, gemini_service: GeminiService) -> None:
        """Test that mock parse extracts player count."""
        result = gemini_service._mock_parse("Book Saturday for 2 players")

        assert result.tee_time_request is not None
        assert result.tee_time_request.num_players == 2

    def test_mock_parse_case_insensitive(self, gemini_service: GeminiService) -> None:
        """Test that mock parse is case insensitive."""
        result1 = gemini_service._mock_parse("BOOK SATURDAY")
        result2 = gemini_service._mock_parse("book saturday")

        assert result1.intent == result2.intent == "book"


class TestGeminiServiceParseMessage:
    """Tests for the parse_message method."""

    @pytest.mark.asyncio
    async def test_parse_message_no_api_key(self) -> None:
        """Test that parse_message uses mock when no API key is configured."""
        with patch("app.services.gemini_service.settings") as mock_settings:
            mock_settings.gemini_api_key = ""
            service = GeminiService()
            result = await service.parse_message("Book Saturday 8am")

            assert result.intent == "book"
            assert result.tee_time_request is not None

    @pytest.mark.asyncio
    async def test_parse_message_with_context(self, gemini_service: GeminiService) -> None:
        """Test parse_message with context."""
        result = await gemini_service.parse_message(
            "tomorrow", context="User is booking a tee time"
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_parse_message_api_error_reports_failure(
        self, gemini_service: GeminiService
    ) -> None:
        """An API error must surface, never a guessed booking.

        The mock parser ignores the date/time in the message and defaults to
        "7 days out at 8:00 AM", so falling back to it on an API error confirms
        a booking the user never asked for (what a retired model did in prod).
        """
        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=Exception("API Error")
            )

            result = await gemini_service.parse_message("Book Saturday at 5:06p")

            assert result.intent == "unclear"
            assert result.tee_time_request is None
            assert result.tee_time_requests is None
            assert "nothing was booked" in result.response_message

    @pytest.mark.asyncio
    async def test_transient_error_is_retried_and_can_succeed(
        self, gemini_service: GeminiService
    ) -> None:
        """A 503 on the first call must not reach the user - retry it.

        This is the failure that told a user "my language model is unavailable"
        one minute after the same model had parsed their booking fine.
        """
        mock_function_call = MagicMock()
        mock_function_call.args = {
            "intent": "confirm",
            "response_message": "Confirmed.",
        }
        mock_part = MagicMock()
        mock_part.function_call = mock_function_call
        mock_content = MagicMock()
        mock_content.parts = [mock_part]
        mock_candidate = MagicMock()
        mock_candidate.content = mock_content
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=[
                    api_error(503, "model overloaded"),
                    mock_response,
                ]
            )

            with patch("app.services.gemini_service.asyncio.sleep", AsyncMock()) as mock_sleep:
                result = await gemini_service.parse_message("Yes")

            assert result.intent == "confirm"
            assert gemini_service._client.aio.models.generate_content.call_count == 2
            mock_sleep.assert_awaited_once_with(0.5)

    @pytest.mark.asyncio
    async def test_transient_error_gives_up_after_max_attempts(
        self, gemini_service: GeminiService
    ) -> None:
        """A sustained outage still fails honestly, after exhausting retries."""
        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, "quota exceeded")
            )

            with patch("app.services.gemini_service.asyncio.sleep", AsyncMock()) as mock_sleep:
                result = await gemini_service.parse_message("Book Saturday at 5:06p")

            assert result.intent == "unclear"
            assert result.tee_time_request is None
            assert "nothing was booked" in result.response_message
            assert (
                gemini_service._client.aio.models.generate_content.call_count == GEMINI_MAX_ATTEMPTS
            )
            # Exponential backoff, and no sleep after the final attempt.
            assert [call.args[0] for call in mock_sleep.await_args_list] == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_each_attempt_carries_a_provider_deadline(
        self, gemini_service: GeminiService
    ) -> None:
        """A hung request must not sit there forever holding up the webhook."""
        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(504, "timed out")
            )

            with patch("app.services.gemini_service.asyncio.sleep", AsyncMock()):
                await gemini_service.parse_message("Book Saturday at 5:06p")

            first_call = gemini_service._client.aio.models.generate_content.await_args_list[0]
            # google-genai takes the deadline in milliseconds, per call.
            config = first_call.kwargs["config"]
            assert config.http_options.timeout == int(GEMINI_REQUEST_TIMEOUT * 1000)

    @pytest.mark.asyncio
    async def test_retries_stop_once_the_total_budget_is_spent(
        self, gemini_service: GeminiService
    ) -> None:
        """Slow attempts eat the budget too, not just the backoff sleeps.

        Twilio drops the webhook at ~15s, so the retry loop has to account for
        how long the calls themselves took - three slow-but-successful attempts
        would otherwise blow the deadline while every attempt "worked".
        """
        # monotonic() readings: budget start, then the remaining-time check and
        # the post-failure check for attempt 1. The second reading lands past
        # the deadline, so there is no second attempt.
        with patch("app.services.gemini_service.monotonic", side_effect=[0.0, 1.0, 9.6]):
            with patch.object(gemini_service, "_client", MagicMock()):
                gemini_service._client.aio.models.generate_content = AsyncMock(
                    side_effect=api_error(503, "model overloaded")
                )

                with patch("app.services.gemini_service.asyncio.sleep", AsyncMock()) as mock_sleep:
                    result = await gemini_service.parse_message("Book Saturday at 5:06p")

                assert result.intent == "unclear"
                assert "nothing was booked" in result.response_message
                assert gemini_service._client.aio.models.generate_content.call_count == 1
                mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_transient_error_is_not_retried(self, gemini_service: GeminiService) -> None:
        """A bad key or retired model fails identically every time - fail fast."""
        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(403, "API key not valid")
            )

            result = await gemini_service.parse_message("Book Saturday at 5:06p")

            assert result.intent == "unclear"
            assert gemini_service._client.aio.models.generate_content.call_count == 1

    @pytest.mark.asyncio
    async def test_depleted_credit_is_not_retried_and_alerts_admin(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty prepaid balance is not a blip: say so, and tell the admin.

        On 2026-10-28 this 429 was retried three times per message and members
        were told to "try again in a minute" while every request kept failing.
        """
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        send = AsyncMock(return_value="sid")
        with (
            patch.object(gemini_service, "_client", MagicMock()),
            patch("app.services.gemini_service.sms_service.send_sms", send),
            patch("app.services.gemini_service.asyncio.sleep", AsyncMock()) as mock_sleep,
        ):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, DEPLETED)
            )

            result = await gemini_service.parse_message("Book Saturday at 9:38a for 4")

            assert result.intent == "unclear"
            assert result.tee_time_request is None
            assert "nothing was booked" in result.response_message
            assert "run out of credit" in result.response_message
            assert "admin has been told" in result.response_message
            assert "try again in a minute" not in result.response_message
            assert result.parser_failed
            assert gemini_service._client.aio.models.generate_content.call_count == 1
            mock_sleep.assert_not_awaited()
            send.assert_awaited_once()
            assert send.await_args.args[0] == "111"
            assert send.await_args.kwargs["channel"] == "telegram"

    @pytest.mark.asyncio
    async def test_depleted_credit_mid_conversation_alerts_and_refuses(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Out of credit mid-conversation: the admin is told, the member gets
        the outage reply, and nothing pending is replaced."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        send = AsyncMock(return_value="sid")
        with (
            patch.object(gemini_service, "_client", MagicMock()),
            patch("app.services.gemini_service.sms_service.send_sms", send),
        ):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, DEPLETED)
            )

            result = await gemini_service.parse_message(
                "book 11/6 at 9:38am", context="Current state: awaiting_slot_choice"
            )

        assert result.intent == "unclear"
        assert result.parser_failed
        assert "run out of credit" in (result.response_message or "")
        send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_depleted_credit_alert_is_throttled(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every member message during the outage must not ping the admin again."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        send = AsyncMock(return_value="sid")
        with (
            patch.object(gemini_service, "_client", MagicMock()),
            patch("app.services.gemini_service.sms_service.send_sms", send),
        ):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, DEPLETED)
            )

            with patch("app.services.gemini_service.monotonic", return_value=1000.0):
                await gemini_service.parse_message("Book Saturday at 9:38a")
                second = await gemini_service.parse_message("Book Saturday at 9:38a")
            assert send.await_count == 1
            assert "admin has been told" in second.response_message

            later = 1000.0 + BILLING_ALERT_INTERVAL + 1
            with patch("app.services.gemini_service.monotonic", return_value=later):
                await gemini_service.parse_message("Book Saturday at 9:38a")
            assert send.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_depleted_credit_requests_alert_once(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second request arriving while the first alert is in flight sends none."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        release = asyncio.Event()

        async def slow_send(*args: object, **kwargs: object) -> str:
            await release.wait()
            return "sid"

        send = AsyncMock(side_effect=slow_send)
        with (
            patch.object(gemini_service, "_client", MagicMock()),
            patch("app.services.gemini_service.sms_service.send_sms", send),
        ):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, DEPLETED)
            )

            first = asyncio.create_task(gemini_service.parse_message("Book Saturday at 9:38a"))
            await asyncio.sleep(0)
            while send.await_count == 0:
                await asyncio.sleep(0)
            # Without the claim the second send also blocks on release: fail, don't hang.
            second = await asyncio.wait_for(
                gemini_service.parse_message("Book Saturday at 9:38a"), timeout=2
            )
            release.set()
            await first

            assert send.await_count == 1
            assert second.parser_failed

    @pytest.mark.asyncio
    async def test_an_alert_that_raises_is_not_counted(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram failing too must not silence the next try or claim the admin knows."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        send = AsyncMock(side_effect=RuntimeError("telegram down"))
        with patch("app.services.gemini_service.sms_service.send_sms", send):
            told = await gemini_service._alert_admin_billing_exhausted()

        assert told is False
        assert gemini_service._last_billing_alert is None

    @pytest.mark.asyncio
    async def test_a_cancelled_alert_releases_its_claim(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A send cut off mid-flight must not silence the alert for an hour."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        send = AsyncMock(side_effect=asyncio.CancelledError)
        with patch("app.services.gemini_service.sms_service.send_sms", send):
            with pytest.raises(asyncio.CancelledError):
                await gemini_service._alert_admin_billing_exhausted()

        assert gemini_service._last_billing_alert is None

    @pytest.mark.asyncio
    async def test_depleted_credit_alert_that_fails_to_send_is_retried(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed send must not claim the admin knows, nor silence the next try."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        send = AsyncMock(return_value=None)
        with (
            patch.object(gemini_service, "_client", MagicMock()),
            patch("app.services.gemini_service.sms_service.send_sms", send),
        ):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, DEPLETED)
            )

            first = await gemini_service.parse_message("Book Saturday at 9:38a")
            await gemini_service.parse_message("Book Saturday at 9:38a")

            assert "admin has been told" not in first.response_message
            assert "nothing was booked" in first.response_message
            assert send.await_count == 2

    @pytest.mark.asyncio
    async def test_depleted_credit_without_an_admin_does_not_claim_one_was_told(
        self, gemini_service: GeminiService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "telegram_admin_user_id", "")
        send = AsyncMock(return_value="sid")
        with (
            patch.object(gemini_service, "_client", MagicMock()),
            patch("app.services.gemini_service.sms_service.send_sms", send),
        ):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                side_effect=api_error(429, DEPLETED)
            )

            result = await gemini_service.parse_message("Book Saturday at 9:38a")

            assert "admin has been told" not in result.response_message
            send.assert_not_awaited()

    def test_a_plain_rate_limit_is_not_mistaken_for_depleted_credit(self) -> None:
        """Only the depleted-credit 429 skips retries; a quota 429 still retries."""
        assert is_billing_exhausted(api_error(429, DEPLETED))
        assert not is_billing_exhausted(api_error(429, "quota exceeded"))
        assert not is_billing_exhausted(api_error(503, DEPLETED))

    @pytest.mark.asyncio
    async def test_model_name_comes_from_settings(self, gemini_service: GeminiService) -> None:
        """The model is configurable so a retirement can be worked around by env."""
        with (
            patch("app.services.gemini_service.settings") as mock_settings,
            patch.object(gemini_service, "_client", MagicMock()),
        ):
            mock_settings.gemini_model = "gemini-test-model"
            gemini_service._client.aio.models.generate_content = AsyncMock(
                return_value=_response({"intent": "help", "response_message": "Hi."})
            )
            await gemini_service.parse_message("what can you do?", context="x")

            call = gemini_service._client.aio.models.generate_content.await_args
            assert call.kwargs["model"] == "gemini-test-model"

    def test_request_config_carries_instructions_and_the_tool(self) -> None:
        """The system prompt and the function declaration go with every call.

        The old SDK bound them to a GenerativeModel once; google-genai takes them
        per request, so a call without them would parse free text unguided.
        """
        config = GeminiService._request_config(GEMINI_REQUEST_TIMEOUT)
        assert config.system_instruction == SYSTEM_PROMPT
        assert config.tools is not None
        declarations = config.tools[0].function_declarations
        assert declarations is not None
        assert [d.name for d in declarations] == ["parse_tee_time_request"]
        params = declarations[0].parameters
        assert params is not None and params.properties is not None
        assert params.required == ["intent", "response_message"]
        bookings = params.properties["bookings"]
        assert bookings.items is not None and bookings.items.properties is not None
        assert set(bookings.items.properties) == {
            "requested_date",
            "requested_time",
            "num_players",
        }

    def test_transient_is_decided_by_status_code(self) -> None:
        """google-genai raises one APIError for every status; only some are retried."""
        for code in (429, 500, 503, 504):
            assert is_transient(api_error(code, "x")), code
        for code in (400, 401, 403, 404):
            assert not is_transient(api_error(code, "x")), code
        assert is_transient(httpx.ReadTimeout("slow"))
        assert is_transient(httpx.ConnectError("down"))
        assert not is_transient(ValueError("bug"))

    @pytest.mark.asyncio
    async def test_parse_message_successful_api_response(
        self, gemini_service: GeminiService
    ) -> None:
        """Test that parse_message correctly processes a successful Gemini API response."""
        # Mock today as Dec 15, 2025 so Dec 20, 2025 is in the future
        fixed_now = datetime(2025, 12, 15, 12, 0, 0)

        mock_function_call = MagicMock()
        mock_function_call.args = {
            "intent": "book",
            "requested_date": "2025-12-20",
            "requested_time": "08:00",
            "num_players": 4,
            "response_message": "I'll book Saturday December 20 at 8am for 4 players.",
        }

        mock_part = MagicMock()
        mock_part.function_call = mock_function_call

        mock_content = MagicMock()
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                return_value=mock_response
            )

            with patch("app.services.gemini_service.datetime") as mock_datetime:
                mock_datetime.now.return_value = fixed_now
                mock_datetime.strptime = datetime.strptime
                result = await gemini_service.parse_message("Book Saturday 8am for 4 players")

            assert result.intent == "book"
            assert result.tee_time_request is not None
            assert result.tee_time_request.requested_date == date(2025, 12, 20)
            assert result.tee_time_request.requested_time == time(8, 0)
            assert result.tee_time_request.num_players == 4
            assert "I'll book" in result.response_message

    @pytest.mark.asyncio
    async def test_parse_message_no_function_call_in_response(
        self, gemini_service: GeminiService
    ) -> None:
        """Test that parse_message returns unclear intent when no function call in response.

        A google-genai text part still has a function_call attribute, set to
        None, so the check must be on the value (#133).
        """
        mock_part = genai_types.Part(text="I'm not sure what you mean.")

        mock_content = MagicMock()
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                return_value=mock_response
            )

            result = await gemini_service.parse_message("random gibberish")

            assert result.intent == "unclear"
            assert "not sure I understood" in result.response_message

    @pytest.mark.asyncio
    async def test_parse_message_empty_candidates(self, gemini_service: GeminiService) -> None:
        """Test that parse_message returns unclear intent when response has no candidates."""
        mock_response = MagicMock()
        mock_response.candidates = []

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                return_value=mock_response
            )

            result = await gemini_service.parse_message("test message")

            assert result.intent == "unclear"

    @pytest.mark.asyncio
    async def test_parse_message_status_intent(self, gemini_service: GeminiService) -> None:
        """Test that parse_message correctly processes a status intent from API."""
        mock_function_call = MagicMock()
        mock_function_call.args = {
            "intent": "status",
            "response_message": "Let me check your bookings.",
        }

        mock_part = MagicMock()
        mock_part.function_call = mock_function_call

        mock_content = MagicMock()
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                return_value=mock_response
            )

            result = await gemini_service.parse_message("What are my bookings?")

            assert result.intent == "status"
            assert result.tee_time_request is None
            assert "check your bookings" in result.response_message

    @pytest.mark.asyncio
    async def test_parse_message_with_clarification_needed(
        self, gemini_service: GeminiService
    ) -> None:
        """Test that parse_message correctly handles clarification_needed from API."""
        mock_function_call = MagicMock()
        mock_function_call.args = {
            "intent": "book",
            "clarification_needed": "What time would you like to play?",
            "response_message": "I need more details about your booking.",
        }

        mock_part = MagicMock()
        mock_part.function_call = mock_function_call

        mock_content = MagicMock()
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(
                return_value=mock_response
            )

            result = await gemini_service.parse_message("Book Saturday")

            assert result.intent == "book"
            assert result.tee_time_request is None
            assert result.clarification_needed == "What time would you like to play?"


class TestGeminiServiceRealResponseShape:
    """Responses built by the google-genai SDK itself, not MagicMocks (#133).

    These replace the protobuf-conversion tests of the old SDK. They build the
    response from the REST API's JSON the way the SDK does, so they pin that
    nested bookings arrive as plain dicts and that the declared snake_case
    argument names are not converted to camelCase.
    """

    @staticmethod
    def _api_response(args: dict) -> genai_types.GenerateContentResponse:
        return genai_types.GenerateContentResponse.model_validate(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "parse_tee_time_request",
                                        "args": args,
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        )

    @pytest.mark.asyncio
    async def test_multiple_bookings_arrive_as_plain_dicts(
        self, gemini_service: GeminiService
    ) -> None:
        future_date = date.today() + timedelta(days=7)
        response = self._api_response(
            {
                "intent": "book",
                "bookings": [
                    {
                        "requested_date": future_date.isoformat(),
                        "requested_time": "08:58",
                        "num_players": 4,
                    },
                    {
                        "requested_date": future_date.isoformat(),
                        "requested_time": "09:06",
                        "num_players": 4,
                    },
                ],
                "response_message": "I'll book 2 tee times.",
            }
        )
        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(return_value=response)
            result = await gemini_service.parse_message("Book tee time at 8:58a and 9:06a")

        assert result.intent == "book"
        assert result.tee_time_requests is not None
        assert [r.requested_time for r in result.tee_time_requests] == [time(8, 58), time(9, 6)]
        assert all(r.requested_date == future_date for r in result.tee_time_requests)

    @pytest.mark.asyncio
    async def test_single_booking_and_snake_case_names_survive(
        self, gemini_service: GeminiService
    ) -> None:
        future_date = date.today() + timedelta(days=7)
        response = self._api_response(
            {
                "intent": "book",
                "bookings": [
                    {
                        "requested_date": future_date.isoformat(),
                        "requested_time": "08:58",
                        "num_players": 2,
                    }
                ],
                "response_message": "I'll book that tee time for you.",
            }
        )
        args = response.candidates[0].content.parts[0].function_call.args  # type: ignore[index,union-attr]
        assert "num_players" in args["bookings"][0]
        assert "numPlayers" not in args["bookings"][0]

        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(return_value=response)
            result = await gemini_service.parse_message("Book next week at 08:58 for 2")

        assert result.tee_time_request is not None
        assert result.tee_time_requests is None
        assert result.tee_time_request.requested_time == time(8, 58)
        assert result.tee_time_request.num_players == 2

    @pytest.mark.asyncio
    async def test_a_text_part_before_the_call_is_skipped(
        self, gemini_service: GeminiService
    ) -> None:
        """The model may say something before calling the function."""
        call = self._api_response({"intent": "status", "response_message": "Checking."})
        call.candidates[0].content.parts.insert(  # type: ignore[index,union-attr]
            0, genai_types.Part(text="Let me look.")
        )
        with patch.object(gemini_service, "_client", MagicMock()):
            gemini_service._client.aio.models.generate_content = AsyncMock(return_value=call)
            result = await gemini_service.parse_message("what do I have booked?", context="x")

        assert result.intent == "status"


class TestGeminiServiceClient:
    """Tests for the client property."""

    def test_client_none_without_api_key(self) -> None:
        """Test that client is None when no API key is configured."""
        with patch("app.services.gemini_service.settings") as mock_settings:
            mock_settings.gemini_api_key = ""
            service = GeminiService()
            assert service.client is None

    def test_client_built_once_with_the_key(self) -> None:
        """One client per service, constructed with the configured key."""
        with (
            patch("app.services.gemini_service.settings") as mock_settings,
            patch("app.services.gemini_service.genai.Client") as mock_client,
        ):
            mock_settings.gemini_api_key = "test-key"
            service = GeminiService()
            assert service.client is service.client
            mock_client.assert_called_once_with(api_key="test-key")
