"""Tests for app/services/strict_parser.py, the no-LLM fallback."""

from datetime import date, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core import exceptions as google_exceptions

from app.services.gemini_service import GeminiService
from app.services.strict_parser import strict_parse

TODAY = date(2026, 10, 28)


@pytest.mark.parametrize(
    ("message", "expected_date", "expected_time", "players"),
    [
        # The two messages Gemini refused on 2026-10-28.
        (
            "schedule tee time on 11/6/26 at 9:38 am for 4 players",
            date(2026, 11, 6),
            time(9, 38),
            4,
        ),
        ("make tee time on 11/6/26 at 9:38 AM for 4 players", date(2026, 11, 6), time(9, 38), 4),
        ("Book 8/2 at 5:06p", date(2027, 8, 2), time(17, 6), 4),
        ("book a tee time 11/6/2026 at 2pm for 2", date(2026, 11, 6), time(14, 0), 2),
        ("Please reserve 11/7 at 12:15 p.m. for 1 player.", date(2026, 11, 7), time(12, 15), 1),
        ("book 11/7 at 12:05am", date(2026, 11, 7), time(0, 5), 4),
        ("book   11/7  at 8:00 a", date(2026, 11, 7), time(8, 0), 4),
    ],
)
def test_reads_an_explicit_request(
    message: str, expected_date: date, expected_time: time, players: int
) -> None:
    parsed = strict_parse(message, today=TODAY)

    assert parsed is not None
    assert parsed.intent == "book"
    assert parsed.tee_time_request is not None
    assert parsed.tee_time_request.requested_date == expected_date
    assert parsed.tee_time_request.requested_time == expected_time
    assert parsed.tee_time_request.num_players == players


@pytest.mark.parametrize(
    "message",
    [
        "book 11/6 at 9:38",  # no am/pm: morning or evening?
        "book Saturday at 9:38am",  # relative day
        "book tomorrow at 9am",
        "book 11/6 at 9:38am and 11/7 at 9:38am",  # two bookings
        "book for Member B 11/6 at 9:38am",  # on someone's behalf
        "book 11/6 at 9:38am for 5 players",  # more than a foursome
        "book 11/6 at 13:38pm",
        "book 11/6 at 9:75am",
        "book 13/6 at 9:38am",
        "book 2/30 at 9:38am",
        "book 10/1/26 at 9:38am",  # a year the member wrote, already past
        "cancel 11/6 at 9:38am",
        "what do I have on 11/6 at 9:38am?",
        "book 11/6 at 9:38am for 4 players please",  # trailing words
        "yes",
        "",
    ],
)
def test_refuses_anything_else(message: str) -> None:
    assert strict_parse(message, today=TODAY) is None


def test_a_date_without_a_year_rolls_to_next_year_once_passed() -> None:
    parsed = strict_parse("book 10/27 at 9am", today=TODAY)

    assert parsed is not None and parsed.tee_time_request is not None
    assert parsed.tee_time_request.requested_date == date(2027, 10, 27)


def test_today_is_not_in_the_past() -> None:
    parsed = strict_parse("book 10/28/26 at 5pm", today=TODAY)

    assert parsed is not None and parsed.tee_time_request is not None
    assert parsed.tee_time_request.requested_date == TODAY


class TestGeminiFallback:
    @pytest.mark.asyncio
    async def test_an_explicit_request_is_read_when_gemini_fails(self) -> None:
        """The 2026-10-28 request would have reached the booking flow."""
        service = GeminiService()
        with (
            patch.object(service, "_model", MagicMock()),
            patch("app.services.gemini_service.asyncio.sleep", AsyncMock()),
        ):
            service._model.generate_content_async = AsyncMock(
                side_effect=google_exceptions.ResourceExhausted("credits are depleted")
            )

            parsed = await service.parse_message(
                "schedule tee time on 11/6/26 at 9:38 am for 4 players"
            )

        assert parsed.intent == "book"
        assert parsed.tee_time_request is not None
        assert parsed.tee_time_request.requested_date == date(2026, 11, 6)
        assert parsed.tee_time_request.requested_time == time(9, 38)
        assert parsed.tee_time_request.num_players == 4

    @pytest.mark.asyncio
    async def test_gemini_is_still_asked_first(self) -> None:
        """The fallback only runs on failure; a working model is never bypassed."""
        service = GeminiService()
        function_call = MagicMock()
        function_call.args = {"intent": "help", "response_message": "Here's how."}
        part = MagicMock(function_call=function_call)
        response = MagicMock()
        response.candidates = [MagicMock(content=MagicMock(parts=[part]))]
        with patch.object(service, "_model", MagicMock()):
            service._model.generate_content_async = AsyncMock(return_value=response)

            parsed = await service.parse_message("book 11/6 at 9:38am")

        assert parsed.intent == "help"
