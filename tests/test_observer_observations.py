"""Tests for the observations ledger (scripts/observer_observations.py, issue #190).

Parsed against tests/fixtures/walden_tee_time_final.html, a real seven-days-out
capture of the club's sheet, so the selectors are checked against the club's
markup rather than against what we believe it to be. Assertions count holders
rather than naming them: the fixture carries real members.
"""

import json
from collections import Counter
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts import observer_observations as obs

FIXTURE = Path(__file__).parent / "fixtures" / "walden_tee_time_final.html"
SLOT_ID = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:{c}:teeTimeSlots:{s}:slotTee:0:slotTeeDIV"


@pytest.fixture(scope="module")
def sheet_html() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def slots(sheet_html: bytes) -> dict[tuple[int, int], obs.Slot]:
    return {(s.course_index, s.slot_index): s for s in obs.parse_sheet(sheet_html)}


def _edit_slot(html: bytes, course: int, slot: int, edit) -> str:
    soup = BeautifulSoup(html, "html.parser")
    edit(soup, soup.find(id=SLOT_ID.format(c=course, s=slot)))
    return str(soup)


class TestParseSheet:
    def test_every_slot_row_is_read(self, slots) -> None:
        assert len(slots) == 145

    def test_courses_are_named_from_their_headings(self, slots) -> None:
        names = {s.course_index: s.course for s in slots.values()}
        assert names == {0: "Northgate", 1: "Walden on Lake Conroe"}

    def test_each_course_keeps_its_own_tee_interval(self, slots) -> None:
        assert [slots[(0, i)].slot_time for i in range(3)] == ["07:30 AM", "07:38 AM", "07:46 AM"]
        assert [slots[(1, i)].slot_time for i in range(3)] == ["07:00 AM", "07:10 AM", "07:20 AM"]

    def test_states_follow_the_club_classes(self, slots) -> None:
        counts = Counter((s.course_index, s.state) for s in slots.values())
        assert counts == {
            (0, "empty"): 11,
            (0, "reserved"): 39,
            (0, "disabled"): 11,
            (0, "blocked"): 17,
            (1, "empty"): 10,
            (1, "reserved"): 32,
            (1, "disabled"): 25,
        }

    def test_partly_filled_row_counts_members_and_open_seats(self, slots) -> None:
        row = slots[(0, 16)]
        assert (row.state, row.slot_time) == ("reserved", "09:46 AM")
        assert (len(row.holders), row.tbd, row.open_seats) == (1, (), 3)

    def test_empty_row_has_no_holders(self, slots) -> None:
        row = slots[(0, 67)]
        assert (row.state, row.raw_class, row.holders, row.open_seats) == ("empty", "Empty", (), 0)

    def test_blocked_rows_keep_their_reason(self, slots) -> None:
        assert (slots[(0, 0)].raw_class, slots[(0, 0)].label) == ("Weather delay", "Weather delay")
        assert (slots[(0, 24)].state, slots[(0, 24)].label) == ("blocked", "Symphony Select")


class TestTbdPlaceholders:
    """Member + 3 TBD must not read as a foursome - the 09-11 trap."""

    def test_bare_placeholder_spans_are_tbd_not_members(self, sheet_html: bytes) -> None:
        def to_placeholders(soup, div):
            for seat in div.select(".custom-free-slot-link"):
                span = soup.new_tag("span", attrs={"class": "res-own-name"})
                span.string = "(Guest)"
                seat.replace_with(span)

        row = next(
            s
            for s in obs.parse_sheet(_edit_slot(sheet_html, 0, 16, to_placeholders))
            if (s.course_index, s.slot_index) == (0, 16)
        )
        assert (len(row.holders), row.tbd, row.open_seats) == (1, ("(Guest)",) * 3, 0)

    def test_placeholder_nested_in_a_name_link_is_not_also_a_member(
        self, sheet_html: bytes
    ) -> None:
        def nest_placeholder(soup, div):
            anchor = div.select_one("a.custom-res-name-link")
            anchor.clear()
            span = soup.new_tag("span", attrs={"class": "res-own-name"})
            span.string = "(Guest)"
            anchor.append(span)

        row = next(
            s
            for s in obs.parse_sheet(_edit_slot(sheet_html, 0, 16, nest_placeholder))
            if (s.course_index, s.slot_index) == (0, 16)
        )
        assert (row.holders, row.tbd) == ((), ("(Guest)",))


@pytest.fixture
def run_dir(tmp_path: Path, sheet_html: bytes) -> Path:
    """A two-snapshot run: 04:34 PM Northgate is Empty at +0ms, Reserved at +1003ms."""

    def reserve(_soup, div):
        div["class"] = "Reserved"

    (tmp_path / "snapshot_+0000ms.html").write_bytes(sheet_html)
    (tmp_path / "snapshot_+1003ms.html").write_text(_edit_slot(sheet_html, 0, 67, reserve))
    manifest = [
        {"index": 0, "sentOffsetMs": 0, "refreshOk": True, "object": "snapshot_+0000ms.html"},
        {"index": 1, "sentOffsetMs": 1003, "refreshOk": True, "object": "snapshot_+1003ms.html"},
    ]
    (tmp_path / "manifest.jsonl").write_text("".join(json.dumps(m) + "\n" for m in manifest))
    return tmp_path


class TestLedger:
    def test_one_row_per_snapshot_and_slot_in_the_issue_schema(self, run_dir: Path) -> None:
        rows = obs.build_observations(run_dir)
        assert len(rows) == 290
        assert sorted({r["tMs"] for r in rows}) == [0, 1003]
        assert {"tMs", "slotIndex", "slotTime", "state", "holders", "course"} <= rows[0].keys()

    def test_written_next_to_the_snapshots(self, run_dir: Path) -> None:
        path = obs.write_observations(run_dir, obs.build_observations(run_dir))
        assert path == run_dir / "observations.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 290 and json.loads(lines[0])["course"] == "Northgate"

    def test_offset_falls_back_to_the_object_name(self, tmp_path: Path, sheet_html: bytes) -> None:
        (tmp_path / "snapshot_+-012ms.html").write_bytes(sheet_html)
        assert {r["tMs"] for r in obs.build_observations(tmp_path)} == {-12}


class TestFlipTable:
    def test_flip_is_bounded_by_the_last_empty_and_first_held_snapshot(self, run_dir: Path) -> None:
        flips = {f.slot_index: f for f in obs.flip_table(obs.build_observations(run_dir))}
        flipped = flips[67]
        assert (flipped.last_empty_ms, flipped.flipped_at_ms, flipped.state_after) == (
            0,
            1003,
            "reserved",
        )

    def test_open_and_never_empty_slots_are_told_apart(self, run_dir: Path) -> None:
        flips = {f.slot_index: f for f in obs.flip_table(obs.build_observations(run_dir))}
        assert (flips[15].first_state, flips[15].flipped_at_ms) == ("reserved", None)
        still_open = [
            f for f in flips.values() if f.first_state == "empty" and f.flipped_at_ms is None
        ]
        assert len(still_open) == 10 and all(f.last_empty_ms == 1003 for f in still_open)

    def test_course_and_tee_time_window_narrow_the_table(self, run_dir: Path) -> None:
        rows = obs.build_observations(run_dir)
        assert [f.slot_time for f in obs.flip_table(rows, start="16:30", end="16:40")] == [
            "04:34 PM"
        ]
        walden = obs.flip_table(rows, course="walden on lake conroe", start="16:30", end="16:40")
        assert [f.slot_time for f in walden] == ["04:30 PM", "04:40 PM"]
        assert len(obs.flip_table(rows, course=None)) == 145

    def test_rendered_table_shows_the_interval_and_flags_stale_snapshots(
        self, run_dir: Path
    ) -> None:
        rows = obs.build_observations(run_dir)
        for row in rows:
            if row["tMs"] == 1003:
                row["refreshOk"] = False
        text = obs.format_flip_table(rows, start="16:30", end="16:40")
        assert "04:34 PM" in text and "(+0, +1003]ms" in text
        assert "WARNING snapshot_+1003ms.html" in text

    def test_unknown_course_is_reported_not_rendered_as_an_empty_table(self, run_dir: Path) -> None:
        rows = obs.build_observations(run_dir)
        typo = obs.format_flip_table(rows, course="Northgat")
        assert "No course named 'Northgat'" in typo
        assert "Northgate, Walden on Lake Conroe" in typo
        # A real course with nothing in the tee-time range stays a plain empty table.
        empty_range = obs.format_flip_table(rows, course="northgate", start="05:00", end="05:30")
        assert "No course named" not in empty_range
