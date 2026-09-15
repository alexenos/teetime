"""
Turn the observer's tee-sheet snapshots into an observations ledger (issue #190).

The observer (``app/observer/``) photographs the sheet once a second across the
window and uploads the raw bytes, deliberately unparsed: parsing a 670KB sheet
costs tens of milliseconds and none of that may land inside the window. This is
the other half, run afterwards against a downloaded run directory::

    walden/observer/<target date>/<run id>/
        snapshot_+NNNNms.html   one per tick
        manifest.jsonl          per snapshot: sentOffsetMs, refreshOk, ...
        run.json

It writes ``observations.jsonl`` into that directory - one row per (snapshot,
slot) - and renders the flip table a post-mortem actually wants: for each slot,
the interval on the window clock in which it stopped being ``Empty``.

Lives in ``scripts/`` rather than ``app/observer/`` on purpose. BeautifulSoup
is a dev dependency, and nothing here belongs in the job image.

Two properties of the club's markup are encoded because they were load-bearing
in the 2026-09-11 post-mortem (``docs/booking-post-mortem-2026-09-11.md`` §3):

* **Named members and TBD placeholders are counted separately.** Four
  ``custom-res-name-link`` anchors is a foursome of real members; one anchor
  plus three ``res-own-name`` spans is a member holding seats for guests to be
  named later. Those look identical if you only count "occupied seats", and
  telling them apart is what disproved the standing-booking theory on 09-11.
* **Every row is tagged with its course.** The sheet carries Northgate
  (``teeTimeCourses:0``, 7-8 minute intervals) and Walden on Lake Conroe
  (``:1``, 10 minutes). The second course being full while Northgate sat open
  was the control that proved the target was genuinely available pre-window.

Selectors are checked against ``tests/fixtures/walden_tee_time_final.html``, a
real seven-days-out capture.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag

OBSERVATIONS_FILE = "observations.jsonl"

_SLOT_ID = re.compile(r"teeTimeCourses:(\d+):teeTimeSlots:(\d+):slotTee")
_COURSE_ID = re.compile(r"teeTimeCourses:(\d+):")
# The observer formats names as f"snapshot_+{offset:04d}ms.html", so a request
# sent before the window reads "snapshot_+-012ms.html".
_SNAPSHOT_NAME = re.compile(r"^snapshot_\+(-?\d+)ms\.html$")


@dataclass(frozen=True)
class Slot:
    """One tee-time row as the sheet rendered it."""

    course_index: int
    course: str
    slot_index: int
    slot_time: str
    state: str
    raw_class: str
    holders: tuple[str, ...]
    tbd: tuple[str, ...]
    open_seats: int
    label: str


@dataclass(frozen=True)
class Flip:
    """When one slot stopped being ``Empty``, bounded by two snapshots."""

    course: str
    slot_index: int
    slot_time: str
    first_state: str
    last_empty_ms: int | None
    flipped_at_ms: int | None
    state_after: str | None
    holders: tuple[str, ...]
    tbd: tuple[str, ...]


def _classes(tag: Tag) -> list[str]:
    value = tag.get("class") or []
    return [value] if isinstance(value, str) else list(value)


def _state(classes: list[str]) -> str:
    """Normalise the slot div's class to a small vocabulary.

    ``rawClass`` keeps the original on every row, so nothing is lost here - but
    "Weather delay" and "Block" both mean "not reservable by anyone, for a
    reason printed on the row", and a flip table wants one word for that.
    """
    if "Empty" in classes:
        return "empty"
    if "Reserved" in classes:
        return "reserved"
    if "ui-state-disabled" in classes:
        return "disabled"
    if "Event" in classes:
        return "event"
    return "blocked"


def _text(tag: Tag | None) -> str:
    return tag.get_text(" ", strip=True) if tag is not None else ""


def parse_sheet(html: bytes | str) -> list[Slot]:
    """Every slot row on a rendered tee sheet, in document order."""
    soup = BeautifulSoup(html, "html.parser")

    courses: dict[int, str] = {}
    for heading in soup.select("label.course-slots-heading"):
        match = _COURSE_ID.search(str(heading.get("id") or ""))
        if match:
            courses[int(match.group(1))] = _text(heading)

    slots: list[Slot] = []
    for div in soup.select("div[id$=':slotTeeDIV']"):
        match = _SLOT_ID.search(str(div.get("id") or ""))
        if not match:
            continue
        course_index, slot_index = int(match.group(1)), int(match.group(2))
        classes = _classes(div)

        # A TBD placeholder is a res-own-name span. Whether the club nests it
        # inside a name anchor or renders it bare, it must not also be counted
        # as a named member - that conflation is the whole trap.
        tbd = tuple(_text(span) for span in div.select("span.res-own-name"))
        holders = tuple(
            _text(anchor)
            for anchor in div.select("a.custom-res-name-link")
            if anchor.select_one("span.res-own-name") is None
        )

        slots.append(
            Slot(
                course_index=course_index,
                course=courses.get(course_index, f"course {course_index}"),
                slot_index=slot_index,
                slot_time=_text(div.select_one("label.custom-time-label")),
                state=_state(classes),
                raw_class=" ".join(classes),
                holders=holders,
                tbd=tbd,
                open_seats=len(div.select(".custom-free-slot-link")),
                label=_text(div.select_one(".custom-disabled-txt")),
            )
        )
    return slots


def _manifest(run_dir: Path) -> dict[str, dict]:
    path = run_dir / "manifest.jsonl"
    if not path.exists():
        return {}
    entries = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {entry["object"]: entry for entry in entries if "object" in entry}


def build_observations(run_dir: Path) -> list[dict]:
    """One row per (snapshot, slot) for a downloaded observer run directory.

    ``tMs`` is the offset at which the snapshot's re-render was *requested*,
    measured from the stated window (06:30:00.000 CT) - the instant the sheet
    describes, and the same frame as the race ledger's ``sentMsPastWindow``.
    The manifest is the source; the object name is the fallback, and they
    agree by construction.

    ``refreshOk`` rides along on every row. ``false`` means the club's
    re-render did not land inside the timeout, so that snapshot may repeat the
    previous one (post-mortem skill §7f) and a flip seen only there is suspect.
    """
    manifest = _manifest(run_dir)

    snapshots: list[tuple[int, Path, bool | None]] = []
    for path in run_dir.glob("snapshot_*ms.html"):
        match = _SNAPSHOT_NAME.match(path.name)
        if not match:
            continue
        entry = manifest.get(path.name, {})
        offset = entry.get("sentOffsetMs")
        snapshots.append(
            (
                int(offset) if offset is not None else int(match.group(1)),
                path,
                entry.get("refreshOk"),
            )
        )
    snapshots.sort(key=lambda item: item[0])

    rows: list[dict] = []
    for offset, path, refresh_ok in snapshots:
        for slot in parse_sheet(path.read_bytes()):
            rows.append(
                {
                    "tMs": offset,
                    "snapshot": path.name,
                    "refreshOk": refresh_ok,
                    "course": slot.course,
                    "courseIndex": slot.course_index,
                    "slotIndex": slot.slot_index,
                    "slotTime": slot.slot_time,
                    "state": slot.state,
                    "rawClass": slot.raw_class,
                    "holders": list(slot.holders),
                    "tbd": list(slot.tbd),
                    "openSeats": slot.open_seats,
                    "label": slot.label,
                }
            )
    return rows


def write_observations(run_dir: Path, rows: Iterable[dict]) -> Path:
    """Write ``observations.jsonl`` next to the snapshots it was read from."""
    path = run_dir / OBSERVATIONS_FILE
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _minutes(clock: str) -> int:
    """Minutes past midnight for "08:38 AM" or a 24-hour "08:38"."""
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            moment = datetime.strptime(clock.strip(), fmt)
        except ValueError:
            continue
        return moment.hour * 60 + moment.minute
    raise ValueError(f"not a clock time: {clock!r}")


def flip_table(
    rows: Iterable[dict],
    *,
    course: str | None = "Northgate",
    start: str | None = None,
    end: str | None = None,
) -> list[Flip]:
    """Per slot, the first snapshot at which it was no longer ``Empty``.

    ``course`` matches the heading case-insensitively; None keeps every course.
    ``start``/``end`` are inclusive tee times ("07:30", "09:30") and narrow the
    table to the contested block.

    A slot that was not ``Empty`` in the first snapshot has no flip - it was
    held or closed before the observer's first look, which says nothing about
    the race.
    """
    lo = _minutes(start) if start else None
    hi = _minutes(end) if end else None

    by_slot: dict[tuple[int, int], list[dict]] = {}
    for row in sorted(rows, key=lambda r: (r["courseIndex"], r["slotIndex"], r["tMs"])):
        if course is not None and row["course"].casefold() != course.casefold():
            continue
        if lo is not None or hi is not None:
            try:
                minutes = _minutes(row["slotTime"])
            except ValueError:
                continue
            if (lo is not None and minutes < lo) or (hi is not None and minutes > hi):
                continue
        by_slot.setdefault((row["courseIndex"], row["slotIndex"]), []).append(row)

    flips: list[Flip] = []
    for history in by_slot.values():
        first = history[0]
        last_empty: int | None = None
        after: dict | None = None
        if first["state"] == "empty":
            for row in history:
                if row["state"] == "empty":
                    last_empty = row["tMs"]
                else:
                    after = row
                    break
        flips.append(
            Flip(
                course=first["course"],
                slot_index=first["slotIndex"],
                slot_time=first["slotTime"],
                first_state=first["state"],
                last_empty_ms=last_empty,
                flipped_at_ms=after["tMs"] if after else None,
                state_after=after["state"] if after else None,
                holders=tuple(after["holders"]) if after else (),
                tbd=tuple(after["tbd"]) if after else (),
            )
        )
    return flips


def format_flip_table(
    rows: list[dict],
    *,
    course: str | None = "Northgate",
    start: str | None = None,
    end: str | None = None,
) -> str:
    """The flip table as text, headed by the snapshots it was read from."""
    offsets = sorted({row["tMs"] for row in rows})
    lines = [f"{len(offsets)} snapshot(s) at " + ", ".join(f"{t:+d}ms" for t in offsets)]
    for offset, name in sorted(
        {(r["tMs"], r["snapshot"]) for r in rows if r["refreshOk"] is False}
    ):
        lines.append(
            f"  WARNING {name} ({offset:+d}ms): refreshOk false - it may repeat the previous "
            "sheet, so do not read a flip from it alone"
        )

    header = (
        f"{'course':<12}  {'#':>3}  {'time':<8}  {'at first':<9}  "
        f"{'stopped being Empty':<22}  {'became':<9}  held by"
    )
    lines += ["", header, "-" * len(header)]
    for flip in flip_table(rows, course=course, start=start, end=end):
        if flip.first_state != "empty":
            when, became, held = "never Empty", "", ""
        elif flip.flipped_at_ms is None:
            when, became, held = f"open through {flip.last_empty_ms:+d}ms", "", ""
        else:
            when = f"({flip.last_empty_ms:+d}, {flip.flipped_at_ms:+d}]ms"
            became = flip.state_after or ""
            held = f"{len(flip.holders)} named + {len(flip.tbd)} TBD"
        lines.append(
            f"{flip.course[:12]:<12}  {flip.slot_index:>3}  {flip.slot_time:<8}  "
            f"{flip.first_state:<9}  {when:<22}  {became:<9}  {held}".rstrip()
        )
    return "\n".join(lines)
