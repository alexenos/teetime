"""Read a booking request without the LLM, when the LLM is unavailable.

Gemini parses every message. When it fails (on 2026-10-28 the prepaid credit
ran out and two requests were refused), most of what members send is still a
plain command with an explicit date, time and player count, such as
"schedule tee time on 11/6/26 at 9:38 am for 4 players".

This parser accepts only that shape and refuses everything else, returning
None so the caller gives its usual "nothing was booked" reply. The fallback it
replaces, _mock_parse, guessed a date and time from any message and once
confirmed a booking nobody asked for; this one never guesses. In particular:

- the whole message must match. Extra words, a second booking, a relative
  day ("Saturday") or a name ("for Member B") all fall through to None;
- the time must say am or pm. "9:38" alone could be morning or evening;
- a date with a year that has already passed is refused rather than moved to
  next year, since the member wrote that year.

A missing player count defaults to 4, as it does for Gemini (SYSTEM_PROMPT in
gemini_service.py). The parsed request goes through the same echo-and-confirm
flow as a Gemini parse, so the member sees what was read before anything books.
"""

import re
from datetime import date, datetime, time

from app.models.schemas import ParsedIntent, TeeTimeRequest

_REQUEST = re.compile(
    r"(?:please\s+)?(?:book|schedule|make|reserve)\s+"
    r"(?:(?:me\s+)?an?\s+)?(?:tee\s*time\s+)?(?:on\s+)?"
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2}|\d{4}))?\s+"
    r"at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap])\.?(?:m\.?)?"
    r"(?:\s+for\s+(?P<players>[1-4])(?:\s+players?)?)?",
    re.IGNORECASE,
)


def strict_parse(message: str, today: date | None = None) -> ParsedIntent | None:
    """Return a single-booking intent if the message is unambiguous, else None."""
    text = " ".join(message.split()).rstrip(".!")
    match = _REQUEST.fullmatch(text)
    if not match:
        return None

    today = today or datetime.now().date()
    requested_date = _resolve_date(match, today)
    requested_time = _resolve_time(match)
    if requested_date is None or requested_time is None:
        return None

    players = int(match["players"]) if match["players"] else 4
    return ParsedIntent(
        intent="book",
        raw_message=message,
        tee_time_request=TeeTimeRequest(
            requested_date=requested_date,
            requested_time=requested_time,
            num_players=players,
        ),
    )


def _resolve_date(match: re.Match[str], today: date) -> date | None:
    month, day = int(match["month"]), int(match["day"])
    raw_year = match["year"]
    if raw_year:
        year = int(raw_year) + (2000 if len(raw_year) == 2 else 0)
        try:
            resolved = date(year, month, day)
        except ValueError:
            return None
        return resolved if resolved >= today else None

    # No year: this year, or next year if the date has passed - the rule
    # Gemini is given for the same input.
    for year in (today.year, today.year + 1):
        try:
            resolved = date(year, month, day)
        except ValueError:
            return None
        if resolved >= today:
            return resolved
    return None


def _resolve_time(match: re.Match[str]) -> time | None:
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match["meridiem"].lower() == "p":
        hour = hour % 12 + 12
    else:
        hour = hour % 12
    return time(hour, minute)
