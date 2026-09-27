"""Which tee times a date actually has: the slot grid read off a rendered sheet.

Issue #216 turns on one fact the booking path never checked: whether the time a
member asks for exists on the club's sheet at all. It often does not. The grid
is not a fixed eight-minute ladder - on the eight mornings the observer
captured between 2026-09-15 and 2026-09-25 it ran 07:30-17:54 on an 8-minute
step on Tuesdays, 07:15-18:00 alternating 7 and 8 minutes on Wednesday,
Thursday, Friday and Sunday, and started at 09:08 on Saturday 09-26. Single
slots go missing (no 09:00 on Sunday 09-27) and an event can replace a block of
them with one row labelled with a range ("08:26 AM-10:42 AM"). The only honest
source for a date's grid is that date's own sheet.

This module reads it. Like ``walden_date_selection`` it sits on the harmless
side of the observer's read-only line: it imports the standard library and
nothing else, so ``app/observer`` can use it without linking in anything that
can send a Reserve (``tests/test_observer.py`` enforces that).

It reads times and states and never names. A reserved row carries its holders'
names in the markup; nothing here looks at them, so nothing derived from this
module can put a member's name in the database or a log.

The markup is the one ``scripts/observer_observations.py`` parses with
BeautifulSoup for post-mortems: a ``div`` whose id ends ``:slotTeeDIV`` per
row, its class naming the row's state, and a ``label.custom-time-label``
holding the time. That script is dev tooling and BeautifulSoup is not in the
image, so this is a stdlib parser of the same elements;
``tests/test_walden_sheet_grid.py`` checks the two agree on a real sheet.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from html.parser import HTMLParser

# Northgate is course 0 in every element id the club emits. The racer relies on
# the same fact (WaldenGolfProvider.NORTHGATE_COURSE_INDEX) and so does the
# observer's row count (sheet._NORTHGATE_ROW_ID_FRAGMENT).
NORTHGATE_COURSE_INDEX = 0

# The row state a member could be offered. Everything else - a row already
# reserved, one the club has blocked or disabled, or an event - is on the sheet
# but not a tee time anyone can take.
OPEN_STATE = "empty"

_SLOT_ID = re.compile(r"teeTimeCourses:(\d+):teeTimeSlots:(\d+):slotTee")
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?")


@dataclass(frozen=True)
class GridSlot:
    """One row of a date's sheet.

    ``start`` is the tee time. ``end`` is set only for a merged row the club
    labels with a range - an event holding "08:26 AM-10:42 AM" - and says the
    times inside it do not exist as rows of their own.
    """

    start: time
    state: str
    end: time | None = None

    @property
    def is_open(self) -> bool:
        """Whether this row is a tee time a member could be offered."""
        return self.state == OPEN_STATE


@dataclass(frozen=True)
class SheetGrid:
    """One date's Northgate rows, as last read from the club's own sheet.

    ``captured_at`` is naive UTC, the convention every timestamp column in this
    project uses. The states are as of that moment: a grid read a week ahead
    says which tee times exist, not which will still be free at 06:30.
    """

    sheet_date: date
    slots: tuple[GridSlot, ...]
    captured_at: datetime | None = None

    def open_times(self) -> list[time]:
        """The tee times a member could be offered, earliest first."""
        return sorted({slot.start for slot in self.slots if slot.is_open})

    def slot_at(self, tee_time: time) -> GridSlot | None:
        """The row starting at ``tee_time``, if the sheet has one."""
        for slot in self.slots:
            if slot.start == tee_time:
                return slot
        return None

    def covering(self, tee_time: time) -> GridSlot | None:
        """The merged row whose range swallows ``tee_time``, if any."""
        for slot in self.slots:
            if slot.end is not None and slot.start <= tee_time <= slot.end:
                return slot
        return None


def row_state(classes: Iterable[str]) -> str:
    """Normalise a row's class to the vocabulary the observations ledger uses.

    Kept identical to ``scripts/observer_observations._state`` so a grid stored
    by the observer and a flip table read from the same sheet by hand call the
    same row the same thing.
    """
    names = set(classes)
    if "Empty" in names:
        return "empty"
    if "Reserved" in names:
        return "reserved"
    if "ui-state-disabled" in names:
        return "disabled"
    if "Event" in names:
        return "event"
    return "blocked"


def parse_clock(text: str) -> list[time]:
    """Every "07:30 AM"-style time in ``text``, in order.

    A plain row label yields one; an event's "08:26 AM-10:42 AM" yields two.
    """
    times = []
    for hour_text, minute_text, meridiem in _CLOCK.findall(text):
        hour, minute = int(hour_text), int(minute_text)
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            continue
        hour %= 12
        if meridiem.lower() == "p":
            hour += 12
        times.append(time(hour, minute))
    return times


class _RowParser(HTMLParser):
    """Collects (course, classes, time label) for every slot row, in order.

    A row runs from its ``slotTeeDIV`` to the ``div`` that closes it; the depth
    counts ``div`` elements only, which is what keeps the void and unclosed
    tags elsewhere in the sheet from shifting it. A second row starting while
    one is still open closes the first rather than nesting inside it, so one
    unbalanced row cannot swallow the rest of the sheet.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[int, list[str], str]] = []
        self._course: int | None = None
        self._classes: list[str] = []
        self._depth = 0
        self._label: str | None = None
        self._label_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            attributes = dict(attrs)
            element_id = attributes.get("id") or ""
            match = _SLOT_ID.search(element_id) if element_id.endswith(":slotTeeDIV") else None
            if match:
                self._finish_row()
                self._course = int(match.group(1))
                self._classes = (attributes.get("class") or "").split()
                self._depth = 1
                return
            if self._course is not None:
                self._depth += 1
        elif (
            tag == "label"
            and self._course is not None
            and self._label is None
            and self._label_parts is None
            and "custom-time-label" in (dict(attrs).get("class") or "").split()
        ):
            self._label_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_parts is not None:
            self._label = "".join(self._label_parts)
            self._label_parts = None
        elif tag == "div" and self._course is not None:
            self._depth -= 1
            if self._depth == 0:
                self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._label_parts is not None:
            self._label_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_row()

    def _finish_row(self) -> None:
        if self._course is None:
            return
        if self._label is None and self._label_parts is not None:
            self._label = "".join(self._label_parts)
        self.rows.append((self._course, self._classes, self._label or ""))
        self._course = None
        self._classes = []
        self._depth = 0
        self._label = None
        self._label_parts = None


def parse_rows(
    html: str | bytes, course_index: int = NORTHGATE_COURSE_INDEX
) -> tuple[list[GridSlot], int]:
    """One course's rows from a rendered sheet, and how many could not be read.

    Returns ``(slots, unparsed)``: the rows whose label held a time, in document
    order, and the count of that course's rows whose label did not. A nonzero
    ``unparsed`` means the markup has moved under this parser, and a caller
    should say so rather than store a grid with holes in it that looks whole.
    """
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    parser = _RowParser()
    parser.feed(text)
    parser.close()

    slots: list[GridSlot] = []
    unparsed = 0
    for course, classes, label in parser.rows:
        if course != course_index:
            continue
        times = parse_clock(label)
        if not times:
            unparsed += 1
            continue
        slots.append(
            GridSlot(
                start=times[0],
                state=row_state(classes),
                end=times[1] if len(times) > 1 else None,
            )
        )
    return slots, unparsed


def slots_to_json(slots: Iterable[GridSlot]) -> str:
    """Serialise rows for storage: 24-hour "HH:MM" strings and the state."""
    return json.dumps(
        [
            {
                "start": slot.start.strftime("%H:%M"),
                "end": slot.end.strftime("%H:%M") if slot.end else None,
                "state": slot.state,
            }
            for slot in slots
        ],
        separators=(",", ":"),
    )


def slots_from_json(text: str) -> tuple[GridSlot, ...]:
    """The inverse of ``slots_to_json``."""
    return tuple(
        GridSlot(
            start=datetime.strptime(row["start"], "%H:%M").time(),
            state=row["state"],
            end=datetime.strptime(row["end"], "%H:%M").time() if row.get("end") else None,
        )
        for row in json.loads(text)
    )
