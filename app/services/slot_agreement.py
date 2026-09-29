"""
Agreeing a real tee time with the member before the race (issue #216).

A request used to be stored as whatever time the member typed, and the first
contact with the club's sheet came at 06:28 on race morning, when the racer
took anything within the fallback window of it. Asking for 08:00 on a sheet
that runs 07:53 / 08:08 therefore never had an "exact" answer, and nothing
afterwards could tell a member bumped off a real 08:00 from one who got the
best slot there was. The scoreboard's Exact/Fallback split is unmeasurable for
exactly that reason (operations/scoreboard.md).

This module holds the parts with no I/O: checking a requested time against a
date's grid, choosing the nearest alternatives to offer, reading the member's
pick, and describing the fallback ladder the racer will walk. The grid itself
comes from the database (the observer records it - see
app/providers/walden_sheet_grid.py), and the conversation lives in
app/services/booking_service.py.

The ladder is a description, not a new behaviour: ``fallback_ladder`` ranks
the way the racer's own slot scan does (``_SLOT_FINDER_JS``: grid-aligned
first, then nearest, then earlier), and ``tests/test_slot_agreement.py`` runs
that JavaScript against the same rows to hold the two together. The race
itself is unchanged.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time
from typing import Literal

from app.providers.walden_sheet_grid import SheetGrid

# How many alternatives to offer when the requested time is not an open slot.
# Three is enough to straddle the request - one either side and the next
# nearest - without turning a question into a list to read.
NEAREST_OPTIONS = 3

# How many rungs of the ladder a confirmation spells out. The whole ladder is
# stored on the booking; the message only needs the first few to say what
# "the next best" means.
LADDER_PREVIEW = 3

# The racer's alignment step: BatchBookingRequest.tee_time_interval_minutes,
# which nothing overrides. A slot a multiple of this from the target ranks ahead
# of a nearer one that is not. Pinned to that default by a test.
RACER_INTERVAL_MINUTES = 8

# Replies that abandon the "which one?" question instead of answering it. As
# with the proxy-target question, every other short reply is read as an answer,
# so these have to be recognised first or "never mind" comes back as "that's
# not one of the times".
SLOT_CHOICE_ABORTS = frozenset(
    {
        "cancel",
        "nevermind",
        "never mind",
        "stop",
        "forget it",
        "no",
        "nope",
        "nah",
        "no thanks",
        "nvm",
        "abort",
    }
)

_PICK_NUMBER = re.compile(r"^#?\s*(\d)\s*$")
_PICK_CLOCK = re.compile(r"^(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))\s*([ap])?\.?\s*m?\.?$")


@dataclass(frozen=True)
class SlotCheck:
    """What a date's sheet says about the time a member asked for.

    ``kind`` is:

    * ``"exact"`` - the requested time is an open row on the sheet;
    * ``"choose"`` - the sheet was read and the time is not an open row, so
      ``options`` holds the nearest open ones and ``reason`` says why the
      request is not one of them (``"absent"``, or the row's state);
    * ``"unknown"`` - there is no usable reading of that date's sheet, so
      nothing can be checked. ``reason`` is ``"no_sheet"`` or, for a sheet that
      was read and had nothing open, ``"nothing_open"``.
    """

    kind: Literal["exact", "choose", "unknown"]
    requested: time
    options: tuple[time, ...] = ()
    reason: str | None = None
    covering_start: time | None = None
    covering_end: time | None = None


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def nearest_open(requested: time, open_times: Sequence[time], count: int) -> tuple[time, ...]:
    """The ``count`` open times nearest ``requested``, earliest first.

    Nearest by distance, the earlier one winning a tie - so 08:00 on a sheet
    with 07:53 and 08:08 offers 07:53 before 08:08, and the list reads in clock
    order either way.
    """
    ranked = sorted(open_times, key=lambda t: (abs(_minutes(t) - _minutes(requested)), _minutes(t)))
    return tuple(sorted(ranked[:count]))


def check_requested_time(requested: time, grid: SheetGrid | None) -> SlotCheck:
    """Check ``requested`` against the last reading of its date's sheet."""
    if grid is None or not grid.slots:
        return SlotCheck(kind="unknown", requested=requested, reason="no_sheet")

    open_times = grid.open_times()
    if not open_times:
        return SlotCheck(kind="unknown", requested=requested, reason="nothing_open")

    if requested in open_times:
        return SlotCheck(kind="exact", requested=requested)

    row = grid.slot_at(requested)
    covering = grid.covering(requested) if row is None else None
    if row is not None:
        reason = row.state
    elif covering is not None:
        reason = covering.state
    else:
        reason = "absent"
    return SlotCheck(
        kind="choose",
        requested=requested,
        options=nearest_open(requested, open_times, NEAREST_OPTIONS),
        reason=reason,
        covering_start=covering.start if covering else None,
        covering_end=covering.end if covering else None,
    )


def fallback_ladder(
    target: time,
    open_times: Sequence[time],
    window_minutes: int,
    interval_minutes: int = RACER_INTERVAL_MINUTES,
) -> list[time]:
    """The tee times the racer would fall back to from ``target``, in its order.

    Every open time within ``window_minutes`` of the target except the target
    itself, ranked the way ``_SLOT_FINDER_JS`` ranks candidates: a multiple of
    the interval away first, then nearest, then earlier. It describes the race
    as it stands; it does not change what the racer does, which still re-reads
    the live sheet at 06:28 and skips whatever is gone by then.
    """
    ranked = []
    for slot in set(open_times):
        diff = abs(_minutes(slot) - _minutes(target))
        if slot == target or diff > window_minutes:
            continue
        aligned = diff % interval_minutes == 0
        ranked.append((0 if aligned else 1, diff, _minutes(slot), slot))
    return [slot for *_, slot in sorted(ranked)]


def parse_pick(reply: str, options: Sequence[time]) -> int | time | None:
    """What a reply to "which one?" names.

    Returns the option's index for "2" or "#2", a clock time for "8:08",
    "8:08am" or "8.08 pm", and None for anything else - which the caller then
    hands to the language model rather than guessing at.

    A time without am/pm is matched against ``options`` on the twelve-hour
    face first, so "8:08" finds 08:08 AM when that is what was offered. One
    that matches no option is read the way a golfer means it: 1:00-6:59 is the
    afternoon, since nobody tees off at 3 in the morning, and anything else is
    taken as written. The caller checks the result against the sheet either way.
    """
    text = " ".join(reply.strip().lower().strip(".!?").split())

    number = _PICK_NUMBER.match(text)
    if number:
        index = int(number.group(1)) - 1
        return index if 0 <= index < len(options) else None

    clock = _PICK_CLOCK.match(text)
    if not clock:
        return None
    hour, minute = int(clock.group(1)), int(clock.group(2))
    meridiem = clock.group(3)
    if not (0 <= minute <= 59):
        return None

    if meridiem is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
        return time(hour, minute)

    if hour > 23:
        return None
    if hour > 12:
        return time(hour, minute)
    for option in options:
        if option.hour % 12 == hour % 12 and option.minute == minute:
            return option
    if 1 <= hour <= 6:
        return time(hour + 12, minute)
    return time(hour, minute)


def clock(value: time) -> str:
    """A tee time as every booking message already writes one: "08:08 AM"."""
    return value.strftime("%I:%M %p")


def choice_question(check: SlotCheck, date_str: str, first_ask: bool = True) -> str:
    """Ask which of the offered tee times the member wants.

    Asked again (``first_ask`` False), it only lists what is on offer. By then
    the options may be the ones nearest a second time the member tried rather
    than the first, and "the closest to 08:00" beside times chosen for 08:20
    would be wrong.
    """
    options = "\n".join(f"{i}. {clock(t)}" for i, t in enumerate(check.options, 1))
    if not first_ask:
        return (
            f"The open tee times on offer for {date_str} are:\n{options}\n"
            "Which one? Reply with the number or the time."
        )

    requested = clock(check.requested)
    if check.reason == "absent":
        lead = f"There's no {requested} tee time on {date_str}."
    elif check.reason == "event" and check.covering_start and check.covering_end:
        lead = (
            f"{requested} on {date_str} falls inside an event block "
            f"({clock(check.covering_start)}-{clock(check.covering_end)})."
        )
    elif check.reason == "reserved":
        lead = f"{requested} on {date_str} is already taken."
    else:
        lead = f"{requested} on {date_str} is blocked on the club's sheet."

    return (
        f"{lead} The closest open tee times are:\n{options}\n"
        "Which one? Reply with the number or the time."
    )


def ladder_note(target: time, ladder: Sequence[time], window_minutes: int) -> str:
    """Say, before anything is scheduled, what the bot takes if the pick is gone."""
    if not ladder:
        return (
            f"{clock(target)} is on the club's sheet. There's no other open tee time "
            f"within {window_minutes} minutes of it, so if it's gone I'll have nothing to "
            "fall back to."
        )
    shown = ", then ".join(clock(t) for t in ladder[:LADDER_PREVIEW])
    rest = len(ladder) - LADDER_PREVIEW
    more = f" (and {rest} more within {window_minutes} minutes)" if rest > 0 else ""
    return f"{clock(target)} is on the club's sheet. If it's gone, I'll try {shown}{more}."


def unchecked_note(check: SlotCheck, date_str: str, window_minutes: int) -> str:
    """Say plainly that the time could not be checked, and what happens then."""
    requested = clock(check.requested)
    if check.reason == "nothing_open":
        seen = f"{date_str}'s tee sheet showed nothing open when I last read it"
    else:
        seen = f"I haven't seen {date_str}'s tee sheet yet"
    return (
        f"{seen}, so I can't check that {requested} is a real tee time. If it isn't, "
        f"or it's gone, I'll take the closest one within {window_minutes} minutes."
    )
