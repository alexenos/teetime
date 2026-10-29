"""Fail CI when the amount of uncovered code moves past the committed ratchet.

    python scripts/check_coverage_ratchet.py coverage.json operations/coverage-ratchet.txt

The ratchet is a high-water mark on *uncovered code* (#11), not on a percentage:
one integer on one line, in a committed file. Uncovered code is
``totals.missing_lines + totals.missing_branches`` from ``coverage.json``. A
percentage moves when covered code is deleted; a count of what is not covered does
not, so deleting tested dead code is never penalised.

The count is not perfectly stable: two CI runs of identical code differed by one
branch (#379). So a run passes when the count is within ``TOLERANCE`` of the file.

- More than ``TOLERANCE`` above the ratchet: fail. New or changed code needs tests.
- More than ``TOLERANCE`` below it: fail, and print the value to commit. The
  ratchet moves down in the pull request that earned it, so no bot writes to
  ``main``.
- Otherwise: pass. A run inside the band also passes when it is better or worse
  by a few, which is the price of the noise; #379 is about removing it.

Every outcome also prints the coverage percentage, so it is reported in the log.

Stdlib only. Exit status: 0 pass, 1 the count is outside the band, 2 cannot check
(a file is missing or unreadable, the ratchet is not a non-negative integer, or
``coverage.json`` has no branch data).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Allowed difference, either way, between a run and the committed ratchet. Set from
# two CI runs of identical code that differed by 1 (#379); set to 0 when that is fixed.
TOLERANCE = 5


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
    if count > allowed + TOLERANCE:
        return 1, (
            f"{detail}: {count - allowed} more than the ratchet {allowed} "
            f"(tolerance {TOLERANCE}). "
            "Add tests for the new or changed code; the count never goes up."
        )
    if count < allowed - TOLERANCE:
        return 1, (
            f"{detail}: {allowed - count} fewer than the ratchet {allowed} "
            f"(tolerance {TOLERANCE}). "
            f"Lower the ratchet: write {count} to operations/coverage-ratchet.txt "
            "in this pull request."
        )
    return 0, f"{detail}, within {TOLERANCE} of the ratchet {allowed} ({count - allowed:+d})."


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
