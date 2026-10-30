"""Tests for app/services/strict_parser.py, the no-LLM fallback."""

from datetime import date, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core import exceptions as google_exceptions

from app.services.gemini_service import GeminiService
from app.services.strict_parser import strict_parse

TODAY = date(2026, 10, 28)

# Every member booking request logged from 2026-10-01 to 2026-10-29 that the
# parser should read, with the date it was sent and what the member meant.
# Thirteen phrasings in all; the admin's messages are not included.
MEMBER_REQUESTS = [
    (
        "book tee time for Sunday, October 11 at 12:00 PM for 4 players",
        date(2026, 10, 1),
        date(2026, 10, 11),
        time(12, 0),
    ),
    (
        "book a tee time Friday October 16 at 9:23 AM for 4 players.",
        date(2026, 10, 1),
        date(2026, 10, 16),
        time(9, 23),
    ),
    (
        "book a 12:08 pm tee time for 4 players on 10-11-2026",
        date(2026, 10, 2),
        date(2026, 10, 11),
        time(12, 8),
    ),
    (
        "book a 8:08 am tee time on October 15 for 4 players",
        date(2026, 10, 8),
        date(2026, 10, 15),
        time(8, 8),
    ),
    (
        "book tee time on 10/16/26 @ 9:23 a.m for 4 players",
        date(2026, 10, 8),
        date(2026, 10, 16),
        time(9, 23),
    ),
    (
        "book a Friday October 16 8:38 am tee time for 4 players",
        date(2026, 10, 8),
        date(2026, 10, 16),
        time(8, 38),
    ),
    (
        "book a tee time for Thursday October 22 at 8:08 am for 4 players",
        date(2026, 10, 14),
        date(2026, 10, 22),
        time(8, 8),
    ),
    (
        "book a tee time on October 23 at 8:38 a.m. for 4 players",
        date(2026, 10, 15),
        date(2026, 10, 23),
        time(8, 38),
    ),
    (
        "make tee time for 10/25/26 for 12:00p for 4 players.",
        date(2026, 10, 17),
        date(2026, 10, 25),
        time(12, 0),
    ),
    (
        "book a 8:38 am tee time for Friday October 30 for 4 players",
        date(2026, 10, 22),
        date(2026, 10, 30),
        time(8, 38),
    ),
    (
        "book 9:30 tee time on 10/30/26 for 4 players.",
        date(2026, 10, 23),
        date(2026, 10, 30),
        time(9, 30),
    ),
    (
        "book tee time 11/1/26 at 12:08pm for 4 players",
        date(2026, 10, 23),
        date(2026, 11, 1),
        time(12, 8),
    ),
    (
        "book a Sunday November 1 2026 tee time at 12:15 pm for 4 players",
        date(2026, 10, 23),
        date(2026, 11, 1),
        time(12, 15),
    ),
    (
        "schedule tee time on 11/6/26 at 9:38 am for 4 players",
        date(2026, 10, 28),
        date(2026, 11, 6),
        time(9, 38),
    ),
    (
        "make tee time on 11/6/26 at 9:38 AM for 4 players",
        date(2026, 10, 28),
        date(2026, 11, 6),
        time(9, 38),
    ),
    (
        "book a Friday November 6 2026 8:38 am tee time for 4 players",
        date(2026, 10, 29),
        date(2026, 11, 6),
        time(8, 38),
    ),
    (
        "book a 8:38 am tee time on Friday, November 6, 2026 for 4 players.",
        date(2026, 10, 29),
        date(2026, 11, 6),
        time(8, 38),
    ),
]


@pytest.mark.parametrize(("message", "sent", "expected_date", "expected_time"), MEMBER_REQUESTS)
def test_reads_what_members_actually_send(
    message: str, sent: date, expected_date: date, expected_time: time
) -> None:
    parsed = strict_parse(message, today=sent)

    assert parsed is not None
    assert parsed.intent == "book"
    assert parsed.tee_time_request is not None
    assert parsed.tee_time_request.requested_date == expected_date
    assert parsed.tee_time_request.requested_time == expected_time
    assert parsed.tee_time_request.num_players == 4


@pytest.mark.parametrize(
    ("message", "expected_date", "expected_time", "players"),
    [
        ("Book 8/2 at 5:06p", date(2027, 8, 2), time(17, 6), 4),
        ("book a tee time 11/6/2026 at 2pm for 2 players", date(2026, 11, 6), time(14, 0), 2),
        ("Please reserve 11/7 at 12:15 p.m. for 1 player.", date(2026, 11, 7), time(12, 15), 1),
        ("book   11/7  at 8:00 a", date(2026, 11, 7), time(8, 0), 4),
        ("book Nov 7th at 7am", date(2026, 11, 7), time(7, 0), 4),
        ("book 11/7 at 6:00 pm", date(2026, 11, 7), time(18, 0), 4),
        ("book 11/7 at 14:30", date(2026, 11, 7), time(14, 30), 4),
    ],
)
def test_reads_other_explicit_forms(
    message: str, expected_date: date, expected_time: time, players: int
) -> None:
    parsed = strict_parse(message, today=TODAY)

    assert parsed is not None
    assert parsed.tee_time_request is not None
    assert parsed.tee_time_request.requested_date == expected_date
    assert parsed.tee_time_request.requested_time == expected_time
    assert parsed.tee_time_request.num_players == players


@pytest.mark.parametrize(
    ("message", "expected_time"),
    [
        # Tee times run 7:00 am to 6:00 pm, so a time without am/pm has one reading.
        ("book 11/7 at 7:00", time(7, 0)),
        ("book 11/7 at 9:30", time(9, 30)),
        ("book 11/7 at 11:52", time(11, 52)),
        ("book 11/7 at 12:08", time(12, 8)),
        ("book 11/7 at 1:15", time(13, 15)),
        ("book 11/7 at 5:54", time(17, 54)),
        ("book 11/7 at 6:00", time(18, 0)),
    ],
)
def test_a_time_without_am_or_pm_is_read_within_tee_sheet_hours(
    message: str, expected_time: time
) -> None:
    parsed = strict_parse(message, today=TODAY)

    assert parsed is not None and parsed.tee_time_request is not None
    assert parsed.tee_time_request.requested_time == expected_time


@pytest.mark.parametrize(
    "message",
    [
        # Member requests from the log it must not read.
        "book a tee time for Thursday October eighth at 8:08 am for 4 players",
        "book a 12:08 pm tee time for 4 players.",  # no date
        "book a October 25 12:08 am tee time for 4 players",  # meant pm; outside hours
        # Outside tee-sheet hours, with or without am/pm.
        "book 11/7 at 6:30",
        "book 11/7 at 6:30 pm",
        "book 11/7 at 6:45am",
        "book 11/7 at 19:00",
        # Ambiguous or not a single explicit booking.
        "book Saturday at 9:38am",
        "book tomorrow at 9am",
        "book 11/6 at 9:38am and 11/7 at 9:38am",
        "book 11/6 at 9:38am at 10:38am",
        "book for Member B 11/6 at 9:38am",
        "book Thursday November 6 at 9:38am",  # 11/6/2026 is a Friday
        "book 11/6 at 9:38am for 5 players",
        "book 11/6 at 9:75am",
        "book 13/6 at 9:38am",
        "book 2/30 at 9:38am",
        "book 2/30/2027 at 9:38am",  # impossible date with a year
        "book 11/7 at 13pm",  # an hour past 12 with am/pm
        "book 11/7 at 25:00",  # an hour past 23
        "book 10/1/26 at 9:38am",  # a year the member wrote, already past
        "book 11/6 at 9",  # a bare number is not a time
        "cancel 11/6 at 9:38am",
        "what do I have on 11/6 at 9:38am?",
        "book 11/6 at 9:38am for 4 players please send",
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
            # The message names 11/6/26; on a later real date it is in the past.
            patch("app.services.strict_parser.datetime") as mock_datetime,
        ):
            mock_datetime.now.return_value = datetime(2026, 10, 28, 12, 0)
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
    async def test_a_plain_request_never_reaches_gemini(self) -> None:
        """Strict parsing goes first; Gemini is not called for what it reads."""
        service = GeminiService()
        with (
            patch.object(service, "_model", MagicMock()),
            patch("app.services.strict_parser.datetime") as mock_datetime,
        ):
            mock_datetime.now.return_value = datetime(2026, 10, 28, 12, 0)
            service._model.generate_content_async = AsyncMock()

            parsed = await service.parse_message("book 11/6 at 9:38am")
            service._model.generate_content_async.assert_not_awaited()

        assert parsed.intent == "book"
        assert parsed.tee_time_request is not None
        assert parsed.tee_time_request.requested_time == time(9, 38)

    @pytest.mark.asyncio
    async def test_in_a_conversation_gemini_reads_it_with_the_context(self) -> None:
        """Mid-conversation a reply is relative to what is pending, which only
        Gemini sees: the strict parser would default a slot-choice reply to 4
        players and turn it into a new request."""
        service = GeminiService()
        function_call = MagicMock()
        function_call.args = {"intent": "book", "response_message": "ok"}
        part = MagicMock(function_call=function_call)
        response = MagicMock()
        response.candidates = [MagicMock(content=MagicMock(parts=[part]))]
        with patch.object(service, "_model", MagicMock()):
            service._model.generate_content_async = AsyncMock(return_value=response)

            await service.parse_message(
                "book 11/6 at 9:38am", context="Current state: awaiting_slot_choice"
            )
            service._model.generate_content_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_in_a_conversation_gemini_failing_is_not_read_strictly(self) -> None:
        """With Gemini down mid-conversation, the reply is refused, not misread.

        The strict parser would default an omitted party size to 4, turning a
        slot pick into a new request that replaces what is pending.
        """
        service = GeminiService()
        with (
            patch.object(service, "_model", MagicMock()),
            patch("app.services.gemini_service.asyncio.sleep", AsyncMock()),
        ):
            service._model.generate_content_async = AsyncMock(
                side_effect=google_exceptions.ServiceUnavailable("overloaded")
            )

            parsed = await service.parse_message(
                "book 11/6 at 9:38am", context="Current state: awaiting_slot_choice"
            )

        assert parsed.intent == "unclear"
        assert parsed.parser_failed
        assert parsed.tee_time_request is None

    @pytest.mark.asyncio
    async def test_anything_else_still_goes_to_gemini(self) -> None:
        """A message the strict parser refuses is parsed by Gemini as before."""
        service = GeminiService()
        function_call = MagicMock()
        function_call.args = {"intent": "help", "response_message": "Here's how."}
        part = MagicMock(function_call=function_call)
        response = MagicMock()
        response.candidates = [MagicMock(content=MagicMock(parts=[part]))]
        with patch.object(service, "_model", MagicMock()):
            service._model.generate_content_async = AsyncMock(return_value=response)

            parsed = await service.parse_message("how do I book?")
            service._model.generate_content_async.assert_awaited_once()

        assert parsed.intent == "help"
