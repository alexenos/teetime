"""Read a booking request without the LLM.

Most of what members send is a plain command with an explicit date, time and
player count, which needs no language model. So GeminiService.parse_message
tries this parser first and calls Gemini only for what it refuses (the
maintainer's decision, 2026-10-29). That also keeps plain bookings working when
Gemini is down: on 2026-10-28 its prepaid credit ran out and every member
request was refused.

The 30 member booking requests logged from 2026-10-01 to 2026-10-29 used 13
different phrasings: the date as "11/6/26", "10-11-2026", "October 23",
"Friday, November 6, 2026"; the time before or after it; "at", "@", "for" or
nothing between. So this parser does not match one sentence. It takes out a
date, a time and a player count wherever they appear, with the small words
that introduce them ("on", "at", "for"), and accepts the message only if what
is left is a booking verb and filler ("a", "tee time"). Anything else left
over - a name, a second date, "eighth" spelled out, a question - and it
returns None and the message goes to Gemini.

The fallback it replaces, _mock_parse, guessed a date and time from any
message and once confirmed a booking nobody asked for; this one never guesses
beyond two rules the maintainer set:

- Tee times run from 7:00 am to 6:00 pm, so a time without am/pm has only one
  reading: 7-11 is morning, 12 and 1-6 are afternoon ("9:30" is 9:30 am). A
  time outside those hours is refused, even with am/pm: "12:08 am" was a typo
  for pm (2026-10-17), and booking it as written is not ours to decide.
- A missing player count is 4, as it is for Gemini (SYSTEM_PROMPT in
  gemini_service.py). Every logged request said 4.

A weekday that does not match the date is refused rather than resolved
either way. A year that has passed is refused rather than moved to next year,
since the member wrote that year.

The parsed request goes through the same echo-and-confirm flow as a Gemini
parse, so the member sees what was read before anything books.
"""

import re
from datetime import date, datetime, time

from app.models.schemas import ParsedIntent, TeeTimeRequest

EARLIEST_TEE_TIME = time(7, 0)
LATEST_TEE_TIME = time(18, 0)

_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_WEEKDAYS = {
    name: number
    for number, names in enumerate(
        [
            ("monday", "mon"),
            ("tuesday", "tue", "tues"),
            ("wednesday", "wed"),
            ("thursday", "thu", "thur", "thurs"),
            ("friday", "fri"),
            ("saturday", "sat"),
            ("sunday", "sun"),
        ]
    )
    for name in names
}

_VERB = re.compile(r"^(?:please\s+)?(?:book|schedule|make|reserve)\b")
_FILLER = {"a", "an", "me", "tee", "time", "teetime", "please"}

_PLAYERS = re.compile(r"(?:\bfor\s+)?\b(?P<players>[1-4])\s+(?:players?|people|golfers)\b")

_WEEKDAY = r"(?:(?P<weekday>" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\.?,?\s+)?"
_NUMERIC_DATE = re.compile(
    r"(?:\b(?:on|for)\s+)?\b"
    + _WEEKDAY
    + r"(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?:[/-](?P<year>\d{4}|\d{2}))?(?![\d/-])"
)
_NAMED_DATE = re.compile(
    r"(?:\b(?:on|for)\s+)?\b"
    + _WEEKDAY
    + r"(?P<month_name>"
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{4})\b)?"
)

# "a"/"p" alone only when attached ("8:08a"), so the article in "9:30 a tee
# time" is not read as am.
_MERIDIEM = r"(?:\s*(?P<m1>[ap])\.?m\b\.?|(?P<m2>[ap])\b)"
_TIME = re.compile(
    r"(?:(?:\bat|\bfor)\s+|@\s*)?(?<![\d/:-])"
    r"(?:(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:" + _MERIDIEM + r")?"
    r"|(?P<bare_hour>\d{1,2})" + _MERIDIEM.replace("m1", "m3").replace("m2", "m4") + r")"
)


def strict_parse(message: str, today: date | None = None) -> ParsedIntent | None:
    """Return a single-booking intent if the message is unambiguous, else None."""
    text = " ".join(message.lower().split()).rstrip(".!")
    verb = _VERB.match(text)
    if not verb:
        return None
    rest = text[verb.end() :]

    try:
        players_match, rest = _take_one(_PLAYERS, rest)
        numeric, after_numeric = _take_one(_NUMERIC_DATE, rest)
        named, after_named = _take_one(_NAMED_DATE, rest)
        if bool(numeric) == bool(named):
            return None  # no date, or one of each
        date_match, rest = (numeric, after_numeric) if numeric else (named, after_named)
        time_match, rest = _take_one(_TIME, rest)
    except _AmbiguousError:
        return None
    if date_match is None or time_match is None:
        return None

    leftover = re.sub(r"[,]", " ", rest).split()
    if any(word not in _FILLER for word in leftover):
        return None

    today = today or datetime.now().date()
    requested_date = _resolve_date(date_match, today)
    requested_time = _resolve_time(time_match)
    if requested_date is None or requested_time is None:
        return None

    players = int(players_match["players"]) if players_match else 4
    return ParsedIntent(
        intent="book",
        raw_message=message,
        tee_time_request=TeeTimeRequest(
            requested_date=requested_date,
            requested_time=requested_time,
            num_players=players,
        ),
    )


class _AmbiguousError(Exception):
    """The message names the same thing twice: two dates, two times."""


def _take_one(pattern: re.Pattern[str], text: str) -> tuple[re.Match[str] | None, str]:
    """The single match of pattern (or None) and text without it."""
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise _AmbiguousError
    if not matches:
        return None, text
    match = matches[0]
    return match, text[: match.start()] + " " + text[match.end() :]


def _resolve_date(match: re.Match[str], today: date) -> date | None:
    groups = match.groupdict()
    month = _MONTHS[groups["month_name"]] if groups.get("month_name") else int(groups["month"])
    day = int(groups["day"])
    raw_year = groups["year"]

    if raw_year:
        year = int(raw_year) + (2000 if len(raw_year) == 2 else 0)
        try:
            resolved = date(year, month, day)
        except ValueError:
            return None
        if resolved < today:
            return None
    else:
        # No year: this year, or next year if the date has passed - the rule
        # Gemini is given for the same input.
        try:
            resolved = date(today.year, month, day)
            if resolved < today:
                resolved = date(today.year + 1, month, day)
        except ValueError:
            return None

    weekday = groups["weekday"]
    if weekday and _WEEKDAYS[weekday] != resolved.weekday():
        return None
    return resolved


def _resolve_time(match: re.Match[str]) -> time | None:
    groups = match.groupdict()
    if groups["hour"] is not None:
        hour, minute = int(groups["hour"]), int(groups["minute"])
        meridiem = groups["m1"] or groups["m2"]
    else:
        hour, minute = int(groups["bare_hour"]), 0
        meridiem = groups["m3"] or groups["m4"]
    if minute > 59:
        return None

    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif 1 <= hour <= 6:
        # No am/pm: only the afternoon reading falls within tee-sheet hours.
        hour += 12
    elif hour > 23:
        return None

    resolved = time(hour, minute)
    if not EARLIEST_TEE_TIME <= resolved <= LATEST_TEE_TIME:
        return None
    return resolved
