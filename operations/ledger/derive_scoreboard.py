"""Derive docs/scoreboard.json from race-report ledger rows.

Definitions are in operations/scoreboard.md; the row schema is in
operations/ledger/README.md. Stdlib only, so it runs anywhere a checkout does.

    python operations/ledger/derive_scoreboard.py ROWS... [--as-of YYYY-MM-DD]
        [--out docs/scoreboard.json]

ROWS is any mix of .jsonl files, .json files (one row each, the GCS layout) and
directories of either. Every row is validated before anything is derived.

Scope, deliberately narrow: this derives the outcome split. It does not derive
the automation streak, which needs the scheduled-date walk in scoreboard.md §2.
While every row is a backfill row that walk has no input, and the streak is
published as unavailable. The first Routine-written row makes this script stop
rather than publish a streak it cannot compute (#234).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

OUTCOMES = ("exact", "fallback", "miss")
WINDOW_DAYS = 28

Row = dict[str, Any]


def load_rows(paths: list[Path]) -> list[Row]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")) + sorted(p.glob("*.jsonl")))
        else:
            files.append(p)
    rows: list[Row] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if f.suffix == ".jsonl":
            rows.extend(json.loads(line) for line in text.splitlines() if line.strip())
        else:
            rows.append(json.loads(text))
    return rows


def validate(row: Row) -> None:
    where = f"{row.get('routine')} {row.get('date')}"
    if row.get("routine") != "race-report":
        raise ValueError(f"{where}: only race-report rows are derived here")
    dt.date.fromisoformat(row["date"])
    if row.get("ok") is False and not row.get("note"):
        raise ValueError(f"{where}: ok is false with no note")
    if not row["raced"]:
        return
    rollup = dict.fromkeys(OUTCOMES, 0)
    for r in row["requests"]:
        if r["member"] is not None:
            raise ValueError(f"{where}: member must be null until a salt exists")
        if r["outcome"] not in OUTCOMES:
            raise ValueError(f"{where}: outcome {r['outcome']!r} is not a schema value")
        if (r["booked"] is None) != (r["outcome"] == "miss"):
            raise ValueError(f"{where}: booked and outcome disagree for {r['requested']}")
        if r["outcome"] == "exact" and r["booked"] != r["requested"]:
            raise ValueError(f"{where}: exact but booked {r['booked']} != {r['requested']}")
        rollup[r["outcome"]] += 1
    if rollup != row["outcome"]:
        raise ValueError(f"{where}: outcome {row['outcome']} != rollup {rollup}")


def split(rows: list[Row], separable: bool) -> Row:
    raced = [r for r in rows if r["raced"]]
    exact = sum(r["outcome"]["exact"] for r in raced)
    fallback = sum(r["outcome"]["fallback"] for r in raced)
    return {
        "mornings": len(raced),
        "booked": exact + fallback,
        "miss": sum(r["outcome"]["miss"] for r in raced),
        "exact": exact if separable else None,
        "fallback": fallback if separable else None,
    }


def derive(rows: list[Row], as_of: dt.date, generated: str) -> Row:
    for row in rows:
        validate(row)
    dates = [r["date"] for r in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("two rows share a date; the ledger holds one object per run")

    routine_rows = [r for r in rows if "backfill" not in r]
    if routine_rows:
        raise SystemExit(
            f"{len(routine_rows)} Routine-written row(s) found. The streak walk in "
            "operations/scoreboard.md section 2 is not implemented here; see #234."
        )

    rows = sorted(rows, key=lambda r: r["date"])
    last_start = as_of - dt.timedelta(days=WINDOW_DAYS)
    prior_start = last_start - dt.timedelta(days=WINDOW_DAYS)

    def within(lo: dt.date, hi: dt.date) -> list[Row]:
        return [r for r in rows if lo < dt.date.fromisoformat(r["date"]) <= hi]

    # Exact and fallback are only published when every row agreed its slots
    # (#216). A mixed history is not separable as a whole.
    separable = bool(rows) and all(r["confirmed_slots"] for r in rows)
    backfill = [r for r in rows if "backfill" in r]

    return {
        "sample": False,
        "generated": generated,
        "definitions": "v1",
        "backfill": {
            "rows": len(backfill),
            "first": backfill[0]["date"] if backfill else None,
            "last": backfill[-1]["date"] if backfill else None,
            "routine_rows": len(routine_rows),
            "source": "operations/ledger/backfill/race-report.jsonl",
        },
        "outcome": {
            "separable": separable,
            "all_time": split(rows, separable),
            "last_4wk": split(within(last_start, as_of), separable),
            "prior_4wk": split(within(prior_start, last_start), separable),
        },
        "streak": {
            "available": False,
            "reason": "No Routine has written a ledger row yet. Backfilled rows "
            "record outcomes, not runs, so they neither extend nor break the streak.",
            "consecutive": None,
            "total_ok": None,
            "last_failure": None,
            "by_routine": {},
            "history": [],
        },
        "cost": {
            "month": as_of.strftime("%Y-%m"),
            "usd": None,
            "usd_per_booking": None,
            "scope": "gcp_only",
            "available": False,
            "reason": "No billing export exists yet.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rows", nargs="+", type=Path)
    parser.add_argument("--as-of", type=dt.date.fromisoformat)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    now = dt.datetime.now(ZoneInfo("America/Chicago"))
    as_of = args.as_of or now.date()
    generated = now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        board = derive(load_rows(args.rows), as_of, generated)
    except (ValueError, KeyError) as exc:
        sys.exit(f"invalid ledger row: {exc}")
    text = json.dumps(board, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
