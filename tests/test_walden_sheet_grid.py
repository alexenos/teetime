"""Tests for reading a date's slot grid off a rendered tee sheet (issue #216).

The grid is what a member's requested time is checked against, so the parser
has to agree with the markup the club actually sends. The first class pins it
to the post-mortem parser (``scripts/observer_observations.py``) on a real
sheet; the rest cover the row shapes that sheet does not happen to contain.
"""

import json
import pathlib
import re
from datetime import date, datetime, time

import pytest

from app.providers import walden_sheet_grid
from app.providers.walden_sheet_grid import (
    GridSlot,
    SheetGrid,
    parse_clock,
    parse_rows,
    row_state,
    slots_from_json,
    slots_to_json,
)
from scripts.observer_observations import parse_sheet

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "walden_tee_time_final.html"
FORM = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:"


def _row(course: int, index: int, css: str, label: str, inner: str = "") -> str:
    """One slot row in the club's markup, trimmed to the elements that matter."""
    return (
        '<li class="ui-datascroller-item">'
        f'<div id="{FORM}teeTimeCourses:{course}:teeTimeSlots:{index}:slotTee:0:slotTeeDIV" '
        f'class="{css}">'
        '<div class="block-available ui-controlgroup">'
        '<div class="ui-grid-a full-width display-flex">'
        '<div class="time-div custom-time-div2">'
        f'<label style="color: #ffffff" class="custom-time-label">{label}</label>'
        "</div>"
        f"{inner}"
        "</div></div></div></li>"
    )


def _sheet(*rows: str) -> str:
    return "<html><body><ul>" + "".join(rows) + "</ul></body></html>"


class TestAgainstARealSheet:
    """The club's own markup, read by both parsers."""

    def test_agrees_with_the_post_mortem_parser(self) -> None:
        """Row for row: the same times, the same states, the same ranges."""
        html = FIXTURE.read_bytes()
        expected = []
        for row in parse_sheet(html):
            if row.course_index != walden_sheet_grid.NORTHGATE_COURSE_INDEX:
                continue
            times = parse_clock(row.slot_time)
            expected.append((times[0], row.state, times[1] if len(times) > 1 else None))

        slots, unparsed = parse_rows(html)

        assert unparsed == 0
        assert [(s.start, s.state, s.end) for s in slots] == expected

    def test_reads_the_grid_the_sheet_actually_has(self) -> None:
        """That morning ran an 8-minute step from 07:30 - and skipped 09:30."""
        slots, _ = parse_rows(FIXTURE.read_bytes())
        grid = SheetGrid(sheet_date=date(2026, 2, 1), slots=tuple(slots))

        assert len(slots) == 78
        assert slots[0].start == time(7, 30)
        assert slots[-1].start == time(17, 54)
        assert grid.slot_at(time(9, 22)) is not None
        assert grid.slot_at(time(9, 30)) is None
        assert grid.slot_at(time(9, 38)) is not None

    def test_nothing_it_stores_can_carry_a_name(self) -> None:
        """Reserved rows hold members' names in the markup; none may come out.

        Checked on the serialised form, because that is what reaches the
        database: every value is a clock time, null, or a state word.
        """
        slots, _ = parse_rows(FIXTURE.read_bytes())
        assert any(slot.state == "reserved" for slot in slots)

        states = {"empty", "reserved", "disabled", "event", "blocked"}
        for row in json.loads(slots_to_json(slots)):
            assert set(row) == {"start", "end", "state"}
            assert re.fullmatch(r"\d\d:\d\d", row["start"])
            assert row["end"] is None or re.fullmatch(r"\d\d:\d\d", row["end"])
            assert row["state"] in states


class TestRowShapes:
    """Rows the fixture does not contain, built from the same markup."""

    def test_an_event_row_carries_its_range(self) -> None:
        """One row standing in for every slot it covers - the times inside do not exist."""
        html = _sheet(
            _row(0, 6, "Empty", "08:18 AM"),
            _row(0, 7, "Event", "08:26 AM-10:42 AM", "<span>An event</span>"),
            _row(0, 8, "Empty", "10:50 AM"),
        )

        slots, unparsed = parse_rows(html)
        grid = SheetGrid(sheet_date=date(2026, 9, 29), slots=tuple(slots))

        assert unparsed == 0
        assert slots[1] == GridSlot(start=time(8, 26), state="event", end=time(10, 42))
        assert not slots[1].is_open
        assert grid.covering(time(9, 0)) == slots[1]
        assert grid.covering(time(10, 50)) is None
        assert grid.open_times() == [time(8, 18), time(10, 50)]

    @pytest.mark.parametrize(
        ("css", "state"),
        [
            ("Empty", "empty"),
            ("Reserved", "reserved"),
            ("Something ui-state-disabled", "disabled"),
            ("Event", "event"),
            ("Block", "blocked"),
            ("", "blocked"),
        ],
    )
    def test_states_use_the_ledgers_vocabulary(self, css: str, state: str) -> None:
        slots, _ = parse_rows(_sheet(_row(0, 0, css, "07:30 AM")))
        assert slots[0].state == state
        assert row_state(css.split()) == state

    def test_other_courses_are_left_out(self) -> None:
        """Walden on Lake Conroe shares the page and runs a 10-minute grid."""
        html = _sheet(
            _row(0, 0, "Empty", "07:30 AM"),
            _row(1, 0, "Empty", "07:00 AM"),
            _row(1, 1, "Empty", "07:10 AM"),
        )

        northgate, _ = parse_rows(html)
        walden, _ = parse_rows(html, course_index=1)

        assert [s.start for s in northgate] == [time(7, 30)]
        assert [s.start for s in walden] == [time(7, 0), time(7, 10)]

    def test_an_unreadable_label_is_counted_not_guessed(self) -> None:
        html = _sheet(_row(0, 0, "Empty", "07:30 AM"), _row(0, 1, "Empty", "Shotgun"))

        slots, unparsed = parse_rows(html)

        assert [s.start for s in slots] == [time(7, 30)]
        assert unparsed == 1

    def test_an_unclosed_row_does_not_swallow_the_next(self) -> None:
        """A row left open by bad markup ends where the next one starts."""
        broken = _row(0, 0, "Empty", "07:30 AM", "<div>never closed").replace("</li>", "")
        html = _sheet(broken, _row(0, 1, "Reserved", "07:38 AM"))

        slots, unparsed = parse_rows(html)

        assert [(s.start, s.state) for s in slots] == [
            (time(7, 30), "empty"),
            (time(7, 38), "reserved"),
        ]
        assert unparsed == 0

    def test_accepts_bytes_and_text_alike(self) -> None:
        html = _sheet(_row(0, 0, "Empty", "12:08 PM"))
        assert parse_rows(html) == parse_rows(html.encode("utf-8"))

    def test_only_the_rows_own_label_is_read(self) -> None:
        """A second label further down the row - a player's time, say - is not the row's."""
        inner = '<label class="custom-time-label">11:11 AM</label>'
        slots, _ = parse_rows(_sheet(_row(0, 0, "Reserved", "07:30 AM", inner)))
        assert slots[0].start == time(7, 30)
        assert slots[0].end is None


class TestParseClock:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("07:30 AM", [time(7, 30)]),
            ("12:08 PM", [time(12, 8)]),
            ("12:15 AM", [time(0, 15)]),
            ("5:06 pm", [time(17, 6)]),
            ("08:26 AM-10:42 AM", [time(8, 26), time(10, 42)]),
            ("13:00 PM", []),
            ("Shotgun", []),
        ],
    )
    def test_reads_the_clubs_clock(self, text: str, expected: list[time]) -> None:
        assert parse_clock(text) == expected


class TestSheetGrid:
    def test_open_times_are_sorted_and_distinct(self) -> None:
        grid = SheetGrid(
            sheet_date=date(2026, 10, 3),
            slots=(
                GridSlot(time(8, 15), "empty"),
                GridSlot(time(8, 0), "empty"),
                GridSlot(time(8, 8), "reserved"),
                GridSlot(time(8, 0), "empty"),
            ),
        )
        assert grid.open_times() == [time(8, 0), time(8, 15)]

    def test_round_trips_through_json(self) -> None:
        slots = (
            GridSlot(time(7, 15), "empty"),
            GridSlot(time(8, 26), "event", end=time(10, 42)),
            GridSlot(time(18, 0), "blocked"),
        )
        assert slots_from_json(slots_to_json(slots)) == slots


def test_imports_nothing_that_can_reserve() -> None:
    """The observer imports this module, so it must stay on the harmless side.

    tests/test_observer.py checks the observer's whole import graph; this keeps
    the module itself to the standard library, so it cannot start pulling the
    booking path in through a convenience import.
    """
    source = pathlib.Path(walden_sheet_grid.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert imports
    for line in imports:
        module = line.split()[1]
        assert not module.startswith(("app", "selenium", "bs4")), line


def test_captured_at_is_carried_as_given() -> None:
    stamp = datetime(2026, 9, 26, 11, 30, 40)
    grid = SheetGrid(sheet_date=date(2026, 10, 3), slots=(), captured_at=stamp)
    assert grid.captured_at == stamp
