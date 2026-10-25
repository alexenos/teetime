"""Fail CI when total coverage is below the committed floor, or has risen past it.

    python scripts/check_coverage_floor.py coverage.json operations/coverage-floor.txt

The floor is a high-water mark (#11). It is one number on one line, to one
decimal, in a committed file. Total coverage is read from ``coverage.json``
(``totals.percent_covered``) and rounded *down* to one decimal.

- Below the floor: fail. Coverage must not fall.
- Above the floor: fail, and print the value to commit. The ratchet moves in
  the pull request that earned it, so no bot writes to ``main``.
- Equal: pass.

Stdlib only. Exit status: 0 pass, 1 below or above the floor, 2 cannot check
(a file is missing or unreadable, or the floor is not a number).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def round_down(percent: float) -> float:
    return math.floor(percent * 10 + 1e-9) / 10


def read_floor(text: str) -> float:
    return float(text.strip())


def check(percent: float, floor: float) -> tuple[int, str]:
    measured = round_down(percent)
    if measured < floor:
        return 1, (
            f"coverage {measured:.1f}% is below the floor {floor:.1f}%. "
            "Add tests for the lines this change leaves uncovered; the floor never goes down."
        )
    if measured > floor:
        return 1, (
            f"coverage {measured:.1f}% is above the floor {floor:.1f}%. "
            f"Raise the floor: write {measured:.1f} to operations/coverage-floor.txt "
            "in this pull request."
        )
    return 0, f"coverage {measured:.1f}% equals the floor {floor:.1f}%."


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        percent = float(json.loads(Path(argv[1]).read_text())["totals"]["percent_covered"])
        floor = read_floor(Path(argv[2]).read_text())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cannot check coverage floor: {exc!r}", file=sys.stderr)
        return 2
    status, message = check(percent, floor)
    print(message)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
