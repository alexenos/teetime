"""Fail CI when the amount of uncovered code changes from the committed ratchet.

    python scripts/check_coverage_ratchet.py coverage.json operations/coverage-ratchet.txt

The ratchet is a high-water mark on *uncovered code* (#11), not on a percentage:
one integer on one line, in a committed file. Uncovered code is
``totals.missing_lines + totals.missing_branches`` from ``coverage.json``. A
percentage moves when covered code is deleted; a count of what is not covered does
not, so deleting tested dead code is never penalised.

- More uncovered than the ratchet: fail. New or changed code needs tests.
- Fewer uncovered than the ratchet: fail, and print the value to commit. The
  ratchet moves down in the pull request that earned it, so no bot writes to
  ``main``.
- Equal: pass.

Every outcome also prints the coverage percentage, so it is reported in the log.

Stdlib only. Exit status: 0 pass, 1 the count differs from the ratchet, 2 cannot
check (a file is missing or unreadable, the ratchet is not a non-negative
integer, or ``coverage.json`` has no branch data).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def uncovered(totals: dict[str, Any]) -> tuple[int, int]:
    return int(totals["missing_lines"]), int(totals["missing_branches"])


def read_ratchet(text: str) -> int:
    allowed = int(text.strip())
    if allowed < 0:
        raise ValueError("coverage ratchet must not be negative")
    return allowed


def check(totals: dict[str, Any], allowed: int) -> tuple[int, str]:
    lines, branches = uncovered(totals)
    count = lines + branches
    percent = float(totals["percent_covered"])
    detail = (
        f"{count} uncovered ({lines} statements + {branches} branches), coverage {percent:.2f}%"
    )
    if count > allowed:
        return 1, (
            f"{detail}: {count - allowed} more than the ratchet {allowed}. "
            "Add tests for the new or changed code; the count never goes up."
        )
    if count < allowed:
        return 1, (
            f"{detail}: {allowed - count} fewer than the ratchet {allowed}. "
            f"Lower the ratchet: write {count} to operations/coverage-ratchet.txt "
            "in this pull request."
        )
    return 0, f"{detail}, equal to the ratchet {allowed}."


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        totals = json.loads(Path(argv[1]).read_text())["totals"]
        allowed = read_ratchet(Path(argv[2]).read_text())
        status, message = check(totals, allowed)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cannot check coverage ratchet: {exc!r}", file=sys.stderr)
        return 2
    print(message)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
