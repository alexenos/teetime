"""Derive docs/scoreboard.json from the Routines' ledger rows.

Definitions are in operations/scoreboard.md; the row schema is in
operations/ledger/README.md. Stdlib only, so it runs anywhere a checkout does.

    python operations/ledger/derive_scoreboard.py ROWS... [--now ISO | --as-of YYYY-MM-DD]
        [--out docs/scoreboard.json] [--row scoreboard-row.json]

ROWS is any mix of .jsonl files, .json files (one row each, the GCS layout) and
directories of either. Every row is validated before anything is derived.

It derives the outcome split from race-report rows and the automation streak
from every Routine's rows. Cost is the newest successful cost row (GCP spend for
a completed month, #227), or unavailable when there is none. With --out, it also reports on stderr whether a source metric
changed against the file it is about to overwrite; that line is what the
scoreboard Routine's notify-on-change rule reads.

The streak needs git: a report modified after the commit that added it marks
its run as failed (scoreboard.md section 2), so the checkout must have full
history. A shallow clone is refused rather than read as "never corrected".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

OUTCOMES = ("exact", "fallback", "miss")
WINDOW_DAYS = 28
CT = ZoneInfo("America/Chicago")

Row = dict[str, Any]


@dataclass(frozen=True)
class Schedule:
    """When a Routine fires, and the first date it is expected to write a row.

    ``fires`` is the CT clock time. The cron is UTC and moves at a DST change;
    the CT time it is meant to hit does not, so the walk is kept in CT.
    ``start`` is None when the first row the Routine wrote is taken as its
    start, which cannot detect a missed first run - set it once known.
    """

    fires: dt.time
    start: dt.date | None
    # None for a daily Routine; otherwise the day of the month it fires on.
    day: int | None = None


SCHEDULES = {
    # The ledger write reached the trigger before the 2026-10-01 run; that is
    # its first row. Earlier mornings are covered by backfill or not at all.
    "race-report": Schedule(fires=dt.time(6, 40), start=dt.date(2026, 10, 1)),
    "scoreboard": Schedule(fires=dt.time(7, 30), start=None),
    # Monthly, reporting the month just ended. The first scheduled run is
    # 2026-11-05 at 05:00 CT: the 5th to let the invoice settle, and ahead of the
    # scoreboard run (operations/routines/cost.md, When to run it). The August and
    # September rows before it are manual seeds carrying a backfill object, so
    # they are outside the walk.
    "cost": Schedule(fires=dt.time(5, 0), start=dt.date(2026, 11, 5), day=5),
}

# A run is scored only once it has had time to finish. Reports have merged 8
# and 11 minutes after the run began; 45 minutes leaves the race report's 06:40
# run complete by the scoreboard's 07:30, and the scoreboard's own run not.
COMPLETION_ALLOWANCE = dt.timedelta(minutes=45)

Corrections = Callable[[list[str]], dict[str, str]]


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
    if row.get("routine") not in SCHEDULES:
        raise ValueError(f"{where}: no schedule is defined for this Routine")
    dt.date.fromisoformat(row["date"])
    if "backfill" in row:
        if row.get("ok") is not None:
            raise ValueError(f"{where}: a backfill row records no run, so ok must be null")
    elif not isinstance(row.get("ok"), bool):
        raise ValueError(f"{where}: ok must be true or false on a Routine row")
    if row.get("ok") is False and not row.get("note"):
        raise ValueError(f"{where}: ok is false with no note")
    if row["routine"] == "cost":
        validate_cost(row, where)
        return
    if row["routine"] != "race-report" or not row["raced"]:
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


def validate_cost(row: Row, where: str) -> None:
    """A cost row either carries a GCP figure for a stated month, or says why not."""
    if row.get("ok") is False:
        return
    try:
        dt.datetime.strptime(row["month"], "%Y-%m")
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"{where}: month must be YYYY-MM") from None
    usd = row.get("usd_gcp")
    if isinstance(usd, bool) or not isinstance(usd, int | float):
        raise ValueError(f"{where}: usd_gcp must be a number on a successful cost row")
    if not row.get("scope"):
        raise ValueError(f"{where}: a cost figure with no stated scope")


def cost_board(rows: list[Row], races: list[Row]) -> Row:
    """Cost from the newest successful cost row; unavailable when there is none.

    The month is the row's, not the clock's: a monthly Routine can only report a
    month that has ended. $/booking divides by exact + fallback for that month from
    the race-report rows, so it needs no figure the cost row could get wrong.
    """
    good = [r for r in rows if r["routine"] == "cost" and r.get("ok") is not False]
    if not good:
        return {
            "month": None,
            "usd": None,
            "usd_per_booking": None,
            "scope": "gcp_only",
            "available": False,
            "reason": "No cost figure has been recorded yet.",
        }
    latest = max(good, key=lambda r: (r["month"], r["date"]))

    def booked(month: str) -> int:
        return sum(
            r["outcome"]["exact"] + r["outcome"]["fallback"]
            for r in races
            if r["raced"] and r["date"].startswith(month)
        )

    n = booked(latest["month"])
    prior = (dt.date.fromisoformat(latest["month"] + "-01") - dt.timedelta(days=1)).strftime(
        "%Y-%m"
    )
    before = [r for r in good if r["month"] == prior]
    return {
        "month": latest["month"],
        "usd": round(latest["usd_gcp"], 2),
        "usd_per_booking": round(latest["usd_gcp"] / n, 2) if n else None,
        "bookings": n,
        "scope": latest["scope"],
        "available": True,
        "reason": None,
        "prior_month": prior if before else None,
        "prior_usd": round(before[-1]["usd_gcp"], 2) if before else None,
    }


def split(rows: list[Row]) -> Row:
    raced = [r for r in rows if r["raced"]]
    exact = sum(r["outcome"]["exact"] for r in raced)
    fallback = sum(r["outcome"]["fallback"] for r in raced)
    return {
        "mornings": len(raced),
        "booked": exact + fallback,
        "miss": sum(r["outcome"]["miss"] for r in raced),
        "exact": exact,
        "fallback": fallback,
    }


def git_corrections(reports: list[str]) -> dict[str, str]:
    """Map each report modified after the commit that added it to a note.

    Any later commit counts, whatever it changed: scoreboard.md section 2 takes
    a modification as the evidence and leaves reading it to a person, which is
    what the note is for. A report with no commit at all - its PR never merged -
    has no correction either, so it is left unverified rather than refused.

    Paths are repository-relative, as the ledger records them, so git runs at
    the checkout's top level whatever the working directory.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", top, *args], capture_output=True, text=True, check=True
        ).stdout

    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise SystemExit(
            "shallow clone: corrections to a report cannot be ruled out. "
            "Run `git fetch --unshallow` first."
        )
    found: dict[str, str] = {}
    for path in reports:
        log = git("log", "--reverse", "--format=%h %cs %s", "--", path).splitlines()
        if len(log) > 1:
            found[path] = f"report modified after merge: {log[1]}"
    return found


def scheduled_dates(name: str, rows: list[Row], now: dt.datetime) -> list[dt.date]:
    """Every date the Routine should have fired on and had time to finish."""
    schedule = SCHEDULES[name]
    start = schedule.start or min((dt.date.fromisoformat(r["date"]) for r in rows), default=None)
    if start is None:
        return []
    dates = []
    day = start
    while dt.datetime.combine(day, schedule.fires, CT) + COMPLETION_ALLOWANCE <= now:
        dates.append(day)
        if schedule.day is None:
            day += dt.timedelta(days=1)
        else:
            day = (day.replace(day=1) + dt.timedelta(days=32)).replace(day=schedule.day)
    return dates


def walk(rows: list[Row], now: dt.datetime, corrections: Corrections) -> Row:
    """The automation streak, per operations/scoreboard.md section 2."""
    runs = [r for r in rows if "backfill" not in r]
    reports = sorted(r["report"] for r in runs if r.get("report"))
    corrected = corrections(reports) if reports else {}

    entries = []  # (date, fires, routine, status, reason)
    for name, schedule in SCHEDULES.items():
        own = [r for r in runs if r["routine"] == name]
        by_date = {dt.date.fromisoformat(r["date"]): r for r in own}
        dates = scheduled_dates(name, own, now)
        early = [d for d in by_date if schedule.start and d < schedule.start]
        if early:
            raise ValueError(f"{name} {min(early)}: a Routine row before its schedule start")
        for day in dates:
            row = by_date.get(day)
            if row is None:
                status, reason = "missing", "no ledger row for a scheduled run"
            elif row["ok"] is False:
                status, reason = "failed", row["note"]
            elif row.get("report") in corrected:
                status, reason = "failed", corrected[row["report"]]
            else:
                # ok: true, written by the run being scored. Nothing has
                # checked it, so it extends the streak as unverified.
                status, reason = "unverified", None
            entries.append((day, schedule.fires, name, status, reason))

    if not entries:
        return {
            "available": False,
            "reason": "No Routine has completed a scheduled run with a ledger row yet.",
            "consecutive": None,
            "unverified": None,
            "total_ok": None,
            "last_failure": None,
            "last_failure_reason": None,
            "last_failure_note": None,
            "by_routine": {},
            "history": [],
        }

    entries.sort(key=lambda e: (e[0], e[1]))
    breaks = [e for e in entries if e[3] != "unverified"]

    def streaks(seq: list[tuple[Any, ...]]) -> list[int]:
        lengths = [0]
        for e in seq:
            if e[3] == "unverified":
                lengths[-1] += 1
            else:
                lengths.append(0)
        return lengths

    combined = streaks(entries)
    last = breaks[-1] if breaks else None
    return {
        "available": True,
        "reason": None,
        "consecutive": combined[-1],
        # Every run in the streak rests on its own report of itself; no
        # evidence source yet marks a run as checked (scoreboard.md section 2).
        "unverified": combined[-1],
        "total_ok": len(entries) - len(breaks),
        "last_failure": last[0].isoformat() if last else None,
        # Routine and kind only. A note is free text and the page is public;
        # main() prints it to stderr for the Routine instead.
        "last_failure_reason": f"{last[2]}: {last[3]}" if last else None,
        "last_failure_note": last[4] if last else None,
        "by_routine": {
            name: streaks([e for e in entries if e[2] == name])[-1]
            for name in SCHEDULES
            if any(e[2] == name for e in entries)
        },
        "history": [n for n in combined[:-1] if n > 0],
        "window": {"first": entries[0][0].isoformat(), "last": entries[-1][0].isoformat()},
    }


def derive(rows: list[Row], now: dt.datetime, corrections: Corrections = git_corrections) -> Row:
    for row in rows:
        validate(row)
    # A cost row also names the month it reports, because the seed rows for two
    # months were written on one date.
    keys = [(r["routine"], r["date"], r.get("month")) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("two rows share a Routine and date; the ledger holds one object per run")

    if not any(r["routine"] == "race-report" for r in rows):
        # The backfill alone guarantees rows here, so none means the read
        # failed. Publishing zeros would look like a measured result.
        raise ValueError("no race-report rows at all; the GCS read failed or was skipped")

    as_of = now.astimezone(CT).date()
    races = sorted((r for r in rows if r["routine"] == "race-report"), key=lambda r: r["date"])
    last_start = as_of - dt.timedelta(days=WINDOW_DAYS)
    prior_start = last_start - dt.timedelta(days=WINDOW_DAYS)

    def within(lo: dt.date, hi: dt.date) -> list[Row]:
        return [r for r in races if lo < dt.date.fromisoformat(r["date"]) <= hi]

    # Exact and fallback are published for every morning, confirmed slots or
    # not: the maintainer judged the pre-#216 scoring good enough (2026-10-03).
    # confirmed_slots stays on each row, so the stricter reading is recoverable.
    backfill = [r for r in races if "backfill" in r]
    routine_raced = [r for r in races if "backfill" not in r and r["raced"]]

    return {
        "sample": False,
        "generated": now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "definitions": "v1",
        "backfill": {
            "rows": len(backfill),
            "first": backfill[0]["date"] if backfill else None,
            "last": backfill[-1]["date"] if backfill else None,
            "routine_rows": len(races) - len(backfill),
            # The page drops its backfill banner once a Routine has scored a
            # race of its own (maintainer, 2026-10-03).
            "routine_raced": len(routine_raced),
            "source": "operations/ledger/backfill/race-report.jsonl",
        },
        "outcome": {
            "all_time": split(races),
            "last_4wk": split(within(last_start, as_of)),
            "prior_4wk": split(within(prior_start, last_start)),
        },
        "streak": walk(rows, now, corrections),
        "cost": cost_board(rows, races),
    }


def ledger_row(board: Row, published: str) -> Row:
    """This run's scoreboard row: the metrics as published (README, scoreboard.jsonl)."""
    o, s, c = board["outcome"], board["streak"], board["cost"]

    def counts(part: Row) -> Row:
        return {
            "exact": part["exact"],
            "fallback": part["fallback"],
            "booked": part["booked"],
            "miss": part["miss"],
        }

    return {
        "date": dt.datetime.fromisoformat(board["generated"]).astimezone(CT).date().isoformat(),
        "routine": "scoreboard",
        "ok": True,
        "published": published,
        "outcome_all_time": counts(o["all_time"]),
        "outcome_4wk": counts(o["last_4wk"]),
        "streak": {
            "consecutive": s["consecutive"],
            "unverified": s["unverified"],
            "by_routine": s["by_routine"],
            "total_ok": s["total_ok"],
        },
        "cost": {
            "month": c["month"],
            "usd_total": c["usd"],
            "usd_per_booking": c["usd_per_booking"],
            "source": "unavailable" if not c["available"] else "cost",
            "scope": c["scope"],
        },
    }


def changed_sources(old: Row, new: Row) -> list[str]:
    """Source metrics that moved, for the notify-on-change rule.

    Excluded because they move without news: the streak (every run extends
    it) and the 4-week windows (they slide daily).
    """

    def source(board: Row) -> Row:
        cost = board.get("cost") or {}
        return {
            "outcome": (board.get("outcome") or {}).get("all_time"),
            "cost": (cost.get("usd"), cost.get("usd_per_booking"), cost.get("available")),
        }

    before, after = source(old), source(new)
    return [k for k in before if before[k] != after[k]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rows", nargs="+", type=Path)
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--now", type=dt.datetime.fromisoformat, help="aware ISO datetime")
    when.add_argument("--as-of", type=dt.date.fromisoformat, help="end of this CT date")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--row", type=Path, help="also write this run's scoreboard ledger row")
    args = parser.parse_args()

    if args.now:
        if args.now.tzinfo is None:
            sys.exit("--now needs a UTC offset")
        now = args.now
    elif args.as_of:
        now = dt.datetime.combine(args.as_of, dt.time(23, 59), CT)
    else:
        now = dt.datetime.now(CT)
    try:
        board = derive(load_rows(args.rows), now)
    except (ValueError, KeyError) as exc:
        sys.exit(f"invalid ledger row: {exc}")
    s = board["streak"]
    note = s.pop("last_failure_note")
    if s["last_failure"]:
        print(
            f"last break: {s['last_failure']} {s['last_failure_reason']}: {note}", file=sys.stderr
        )
    text = json.dumps(board, indent=2) + "\n"
    if args.out:
        if args.out.exists():
            moved = changed_sources(json.loads(args.out.read_text(encoding="utf-8")), board)
            print(f"source metrics changed: {', '.join(moved) or 'none'}", file=sys.stderr)
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if args.row:
        published = args.out.as_posix() if args.out else "stdout"
        args.row.write_text(json.dumps(ledger_row(board, published)) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
