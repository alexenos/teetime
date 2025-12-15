"""
Tests for GeminiService in app/services/gemini_service.py.

These tests verify the LLM-powered message parsing and intent extraction
functionality, including the mock fallback when the API is not configured.
"""

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import pytest

from app.services.gemini_service import GeminiService


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
        args = {
            "intent": "book",
            "requested_date": "2025-12-20",
            "requested_time": "08:00",
            "num_players": 4,
            "response_message": "I'll book that for you!",
        }

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
    async def test_parse_message_no_api_key(self, gemini_service: GeminiService) -> None:
        """Test that parse_message uses mock when no API key is configured."""
        result = await gemini_service.parse_message("Book Saturday 8am")

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
    async def test_parse_message_api_error_fallback(self, gemini_service: GeminiService) -> None:
        """Test that parse_message falls back to mock on API error."""
        with patch.object(gemini_service, "_model", MagicMock()):
            gemini_service._model.generate_content = MagicMock(side_effect=Exception("API Error"))

            result = await gemini_service.parse_message("Book Saturday")

            assert result.intent == "book"


class TestGeminiServiceModel:
    """Tests for the model property."""

    def test_model_none_without_api_key(self) -> None:
        """Test that model is None when no API key is configured."""
        with patch("app.services.gemini_service.settings") as mock_settings:
            mock_settings.gemini_api_key = ""
            service = GeminiService()
            assert service.model is None

    def test_model_cached(self, gemini_service: GeminiService) -> None:
        """Test that model is cached after first access."""
        model1 = gemini_service.model
        model2 = gemini_service.model
        assert model1 is model2
