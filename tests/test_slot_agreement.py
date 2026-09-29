"""Tests for agreeing a real tee time before the race (issue #216).

The grids below are the two shapes the observer photographed on eight mornings
in September 2026: an 8-minute step from 07:30 (Tuesdays) and an alternating
7/8-minute step from 07:15 (Wednesday to Friday, and Sunday). A request for
08:00 has an exact answer on the second and none on the first.
"""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import date, time
from typing import Any

import pytest

from app.providers.base import BatchBookingRequest
from app.providers.walden_sheet_grid import GridSlot, SheetGrid
from app.services import slot_agreement
from app.services.slot_agreement import (
    check_requested_time,
    choice_question,
    fallback_ladder,
    ladder_note,
    nearest_open,
    parse_pick,
    unchecked_note,
)


def _clock_range(start: time, steps: list[int], until: time) -> list[time]:
    """Times from ``start`` stepping by ``steps`` in turn, up to ``until``."""
    times = [start]
    minutes = start.hour * 60 + start.minute
    i = 0
    while True:
        minutes += steps[i % len(steps)]
        i += 1
        if minutes > until.hour * 60 + until.minute:
            return times
        times.append(time(minutes // 60, minutes % 60))


TUESDAY = _clock_range(time(7, 30), [8], time(17, 54))
ALTERNATING = _clock_range(time(7, 15), [8, 7], time(18, 0))


def _grid(times: list[time], **states: str) -> SheetGrid:
    """A grid whose rows are open unless named in ``states`` ("HHMM" -> state)."""
    slots = tuple(GridSlot(t, states.get(t.strftime("t%H%M"), "empty")) for t in times)
    return SheetGrid(sheet_date=date(2026, 10, 3), slots=slots)


class TestTheObservedGrids:
    def test_the_fixtures_match_what_the_observer_saw(self) -> None:
        assert TUESDAY[:4] == [time(7, 30), time(7, 38), time(7, 46), time(7, 54)]
        assert ALTERNATING[:8] == [
            time(7, 15),
            time(7, 23),
            time(7, 30),
            time(7, 38),
            time(7, 45),
            time(7, 53),
            time(8, 0),
            time(8, 8),
        ]
        assert ALTERNATING[-1] == time(18, 0)


class TestCheckRequestedTime:
    def test_a_real_open_slot_is_exact(self) -> None:
        check = check_requested_time(time(8, 0), _grid(ALTERNATING))
        assert check.kind == "exact"

    def test_a_time_the_sheet_does_not_have_offers_the_nearest(self) -> None:
        """The issue's own example: no 8:00 on a Tuesday."""
        check = check_requested_time(time(8, 0), _grid(TUESDAY))

        assert check.kind == "choose"
        assert check.reason == "absent"
        assert check.options == (time(7, 54), time(8, 2), time(8, 10))

    def test_a_taken_slot_is_not_offered_back(self) -> None:
        check = check_requested_time(time(8, 8), _grid(ALTERNATING, t0808="reserved"))

        assert check.kind == "choose"
        assert check.reason == "reserved"
        assert check.options == (time(7, 53), time(8, 0), time(8, 15))

    def test_a_time_inside_an_event_says_so(self) -> None:
        slots = (
            GridSlot(time(8, 18), "empty"),
            GridSlot(time(8, 26), "event", end=time(10, 42)),
            GridSlot(time(10, 50), "empty"),
            GridSlot(time(10, 58), "empty"),
        )
        grid = SheetGrid(sheet_date=date(2026, 9, 29), slots=slots)

        check = check_requested_time(time(9, 0), grid)

        assert check.kind == "choose"
        assert check.reason == "event"
        assert (check.covering_start, check.covering_end) == (time(8, 26), time(10, 42))
        assert check.options == (time(8, 18), time(10, 50), time(10, 58))

    def test_no_reading_of_the_date_is_unknown(self) -> None:
        check = check_requested_time(time(8, 0), None)
        assert (check.kind, check.reason) == ("unknown", "no_sheet")

    def test_a_sheet_with_nothing_open_is_unknown_not_a_question(self) -> None:
        """There is nothing to offer, so asking "which one?" would be a dead end."""
        grid = _grid([time(8, 0), time(8, 8)], t0800="blocked", t0808="reserved")
        check = check_requested_time(time(8, 0), grid)
        assert (check.kind, check.reason) == ("unknown", "nothing_open")


class TestNearestOpen:
    def test_ties_go_to_the_earlier_time(self) -> None:
        """The racer breaks ties the same way."""
        assert nearest_open(time(8, 0), [time(7, 56), time(8, 4), time(7, 48), time(8, 12)], 3) == (
            time(7, 48),
            time(7, 56),
            time(8, 4),
        )

    def test_the_edge_of_the_day_still_offers_three(self) -> None:
        assert nearest_open(time(6, 30), ALTERNATING, 3) == (time(7, 15), time(7, 23), time(7, 30))


class TestParsePick:
    OPTIONS = (time(7, 53), time(8, 8), time(8, 15))

    @pytest.mark.parametrize(("reply", "index"), [("1", 0), ("2", 1), (" 3 ", 2), ("#2", 1)])
    def test_a_number_picks_an_option(self, reply: str, index: int) -> None:
        assert parse_pick(reply, self.OPTIONS) == index

    @pytest.mark.parametrize("reply", ["0", "4", "9"])
    def test_a_number_off_the_list_picks_nothing(self, reply: str) -> None:
        assert parse_pick(reply, self.OPTIONS) is None

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("8:08", time(8, 8)),
            ("8:08am", time(8, 8)),
            ("8:08 AM", time(8, 8)),
            ("08:08 a.m.", time(8, 8)),
            ("8.08", time(8, 8)),
            ("at 8:15", time(8, 15)),
            ("8:23", time(8, 23)),
            ("1:30", time(13, 30)),
            ("1:30 am", time(1, 30)),
            ("12:08", time(12, 8)),
            ("12:08 pm", time(12, 8)),
            ("14:00", time(14, 0)),
        ],
    )
    def test_a_time_is_read_the_way_a_golfer_means_it(self, reply: str, expected: time) -> None:
        assert parse_pick(reply, self.OPTIONS) == expected

    def test_an_offered_afternoon_time_matches_without_pm(self) -> None:
        assert parse_pick("3:08", (time(15, 0), time(15, 8))) == time(15, 8)

    @pytest.mark.parametrize("reply", ["the second one", "8", "yes", "8am", "25:00", "8:61", ""])
    def test_anything_else_is_left_for_the_parser(self, reply: str) -> None:
        assert parse_pick(reply, self.OPTIONS) is None


class TestFallbackLadder:
    def test_aligned_first_then_nearest_then_earlier(self) -> None:
        ladder = fallback_ladder(time(8, 8), ALTERNATING, window_minutes=32)
        assert ladder == [
            time(8, 0),
            time(8, 15),
            time(7, 53),
            time(8, 23),
            time(8, 30),
            time(7, 45),
            time(7, 38),
            time(8, 38),
        ]

    def test_only_open_times_within_the_window(self) -> None:
        ladder = fallback_ladder(time(7, 30), [time(7, 30), time(7, 38), time(8, 10)], 32)
        assert ladder == [time(7, 38)]

    def test_the_interval_is_the_racers(self) -> None:
        """The alignment key has to be the one the racer is actually handed."""
        request = BatchBookingRequest(booking_id="x", target_time=time(8, 0), num_players=4)
        assert slot_agreement.RACER_INTERVAL_MINUTES == request.tee_time_interval_minutes


# The racer's own scan, run over the same rows: the ladder a member is told
# about has to be the order the racer will actually walk.
_NODE = shutil.which("node")


def _racer_ranking(open_times: list[time], target: time, window: int) -> list[time]:
    from app.providers.walden_provider import _SLOT_FINDER_JS
    from tests.test_walden_provider import _DOM_SHIM

    rows = [[t.strftime("%I:%M %p"), 4] for t in open_times]
    args = [target.hour, target.minute, 4, window, slot_agreement.RACER_INTERVAL_MINUTES]
    args += [[], "0", 4]
    program = (
        f"const SCRIPT = {json.dumps(_SLOT_FINDER_JS)};\n"
        f"const ROWS = {json.dumps(rows)};\n"
        f"const ARGS = {json.dumps(args)};\n" + _DOM_SHIM
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(program)
        path = fh.name
    try:
        proc = subprocess.run(
            [str(_NODE), path], capture_output=True, text=True, timeout=30, check=True
        )
    finally:
        os.unlink(path)
    out: dict[str, Any] = json.loads(proc.stdout)
    return [time(c["hours"], c["minutes"]) for c in out["candidates"]]


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH")
class TestLadderMatchesTheRacer:
    @pytest.mark.parametrize("grid", [TUESDAY, ALTERNATING], ids=["tuesday", "alternating"])
    def test_every_target_on_the_observed_grids(self, grid: list[time]) -> None:
        """Every slot of both grids as a target, 32 minutes either side."""
        for target in grid:
            racer = _racer_ranking(grid, target, 32)
            assert racer[0] == target, target
            assert fallback_ladder(target, grid, 32) == racer[1:], target

    def test_a_narrower_window_too(self) -> None:
        for target in ALTERNATING[10:20]:
            racer = _racer_ranking(ALTERNATING, target, 16)
            assert fallback_ladder(target, ALTERNATING, 16) == racer[1:], target


class TestWording:
    def test_the_question_names_the_request_and_numbers_the_options(self) -> None:
        check = check_requested_time(time(8, 0), _grid(TUESDAY))

        text = choice_question(check, "Tuesday, October 06")

        assert text.startswith("There's no 08:00 AM tee time on Tuesday, October 06.")
        assert "1. 07:54 AM\n2. 08:02 AM\n3. 08:10 AM" in text
        assert "Which one?" in text

    def test_a_re_ask_lists_the_offer_without_restating_the_reason(self) -> None:
        """The options may by then be the ones nearest a second time the member tried."""
        check = check_requested_time(time(8, 0), _grid(TUESDAY))
        text = choice_question(check, "Tuesday, October 06", first_ask=False)
        assert text == (
            "The open tee times on offer for Tuesday, October 06 are:\n"
            "1. 07:54 AM\n2. 08:02 AM\n3. 08:10 AM\n"
            "Which one? Reply with the number or the time."
        )

    def test_the_ladder_note_shows_the_first_rungs_and_counts_the_rest(self) -> None:
        ladder = fallback_ladder(time(8, 8), ALTERNATING, 32)
        text = ladder_note(time(8, 8), ladder, 32)
        assert text == (
            "08:08 AM is on the club's sheet. If it's gone, I'll try 08:00 AM, then "
            "08:15 AM, then 07:53 AM (and 5 more within 32 minutes)."
        )

    def test_a_ladder_with_nothing_on_it_says_so(self) -> None:
        text = ladder_note(time(7, 15), [], 32)
        assert "nothing to fall back to" in text

    def test_an_unchecked_time_says_what_happens_instead(self) -> None:
        check = check_requested_time(time(8, 0), None)
        text = unchecked_note(check, "Saturday, October 10", 32)
        assert text == (
            "I haven't seen Saturday, October 10's tee sheet yet, so I can't check that "
            "08:00 AM is a real tee time. If it isn't, or it's gone, I'll take the closest "
            "one within 32 minutes."
        )
