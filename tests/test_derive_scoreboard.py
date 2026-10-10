"""
Tests for operations/ledger/derive_scoreboard.py, the derivation the scoreboard
Routine runs (operations/routines/scoreboard.md).

The streak walk is the part under test: it must break on a missing row, not
only on ``ok: false``, must not score a run that has not had time to finish,
and must skip backfill rows entirely (operations/scoreboard.md section 2).
"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "operations" / "ledger" / "derive_scoreboard.py"
)
_spec = importlib.util.spec_from_file_location("derive_scoreboard", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
ds = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ds
_spec.loader.exec_module(ds)

CT = ds.CT
RACE_START = ds.SCHEDULES["race-report"].start


def _no_corrections(reports: list[str]) -> dict[str, str]:
    return {}


def _ct(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), CT)


def _quiet(day: dt.date, routine: str = "race-report", ok: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {"date": day.isoformat(), "routine": routine, "ok": ok}
    if routine == "race-report":
        row["raced"] = False
    if not ok:
        row["note"] = "setup_remote_env.sh reported NOT READY"
    return row


def _raced(day: dt.date, outcome: str = "exact", backfill: bool = False) -> dict[str, Any]:
    booked = None if outcome == "miss" else ("08:00" if outcome == "exact" else "08:08")
    row: dict[str, Any] = {
        "date": day.isoformat(),
        "routine": "race-report",
        "ok": None if backfill else True,
        "raced": True,
        "report": f"operations/race-reports/{day.isoformat()}.md",
        "pr": 1,
        "requests": [{"member": None, "requested": "08:00", "booked": booked, "outcome": outcome}],
        "outcome": {k: int(k == outcome) for k in ds.OUTCOMES},
        "confirmed_slots": False,
    }
    if backfill:
        row["backfill"] = {"written": "2026-09-30", "basis": "reservation_check"}
    return row


def _days(n: int) -> list[dt.date]:
    return [RACE_START + dt.timedelta(days=i) for i in range(n)]


def _streak(rows: list[dict[str, Any]], now: dt.datetime, **kw: Any) -> dict[str, Any]:
    corrections = kw.pop("corrections", _no_corrections)
    result: dict[str, Any] = ds.derive(rows, now, corrections)["streak"]
    return result


def test_consecutive_clean_runs_extend_the_streak_as_unverified() -> None:
    days = _days(3)
    s = _streak([_quiet(d) for d in days], _ct(days[-1], 7, 30))
    assert s["available"] is True
    assert s["consecutive"] == 3
    assert s["unverified"] == 3
    assert s["total_ok"] == 3
    assert s["last_failure"] is None


def test_a_missing_row_breaks_the_streak() -> None:
    days = _days(4)
    rows = [_quiet(d) for d in days if d != days[1]]
    s = _streak(rows, _ct(days[-1], 7, 30))
    assert s["consecutive"] == 2
    assert s["last_failure"] == days[1].isoformat()
    assert s["last_failure_reason"] == "race-report: missing"
    assert s["history"] == [1]


def test_ok_false_breaks_the_streak_and_carries_its_note() -> None:
    days = _days(3)
    rows = [_quiet(days[0]), _quiet(days[1], ok=False), _quiet(days[2])]
    s = _streak(rows, _ct(days[-1], 7, 30))
    assert s["consecutive"] == 1
    assert "NOT READY" in s["last_failure_note"]
    assert s["total_ok"] == 2


def test_a_corrected_report_breaks_the_streak() -> None:
    days = _days(2)
    rows = [_raced(days[0]), _quiet(days[1])]

    def corrected(reports: list[str]) -> dict[str, str]:
        return {reports[0]: "report modified after merge: abc1234"}

    s = _streak(rows, _ct(days[-1], 7, 30), corrections=corrected)
    assert s["consecutive"] == 1
    assert s["last_failure"] == days[0].isoformat()
    assert "abc1234" in s["last_failure_note"]


def test_a_run_still_in_progress_is_not_scored() -> None:
    # 07:00 CT is twenty minutes after the race report fired: today's row is
    # absent, and must not count as missing.
    days = _days(3)
    rows = [_quiet(d) for d in days[:2]]
    s = _streak(rows, _ct(days[2], 7, 0))
    assert s["consecutive"] == 2
    assert s["last_failure"] is None


def test_the_scoreboard_does_not_score_its_own_run() -> None:
    # Deriving at 07:31 CT, the scoreboard's own row for today cannot exist yet.
    days = _days(2)
    rows = [_quiet(d) for d in days] + [_quiet(days[0], "scoreboard")]
    s = _streak(rows, _ct(days[1], 7, 31))
    assert s["by_routine"] == {"race-report": 2, "scoreboard": 1}
    assert s["consecutive"] == 3


def test_routines_interleave_so_any_failure_resets_the_combined_streak() -> None:
    days = _days(3)
    rows = [_quiet(d) for d in days]
    rows += [_quiet(days[0], "scoreboard"), _quiet(days[1], "scoreboard", ok=False)]
    rows += [_quiet(days[2], "scoreboard")]
    s = _streak(rows, _ct(days[2], 9, 0))
    # Day 2: race (ok), scoreboard (failed). Day 3: race, scoreboard.
    assert s["consecutive"] == 2
    assert s["by_routine"] == {"race-report": 3, "scoreboard": 1}


def test_backfill_rows_neither_extend_nor_break_the_streak() -> None:
    before = RACE_START - dt.timedelta(days=5)
    rows = [_raced(before, backfill=True), _quiet(RACE_START)]
    s = _streak(rows, _ct(RACE_START, 7, 30))
    assert s["consecutive"] == 1
    assert s["window"]["first"] == RACE_START.isoformat()


def test_only_backfill_leaves_the_streak_unavailable() -> None:
    before = RACE_START - dt.timedelta(days=5)
    s = _streak([_raced(before, backfill=True)], _ct(RACE_START, 6, 0))
    assert s["available"] is False
    assert s["consecutive"] is None


def test_a_routine_row_before_the_schedule_start_is_refused() -> None:
    with pytest.raises(ValueError, match="before its schedule start"):
        _streak([_quiet(RACE_START - dt.timedelta(days=1))], _ct(RACE_START, 7, 30))


def test_a_row_for_an_unscheduled_routine_is_refused() -> None:
    with pytest.raises(ValueError, match="no schedule"):
        _streak([_quiet(RACE_START, routine="bogus")], _ct(RACE_START, 7, 30))


def test_the_same_date_may_hold_one_row_per_routine() -> None:
    rows = [_quiet(RACE_START), _quiet(RACE_START, "scoreboard")]
    ds.derive(rows, _ct(RACE_START, 9, 0), _no_corrections)
    with pytest.raises(ValueError, match="share a Routine and date"):
        ds.derive(rows + [_quiet(RACE_START)], _ct(RACE_START, 9, 0), _no_corrections)


def test_outcome_split_counts_backfill_and_routine_races_but_not_quiet_mornings() -> None:
    before = RACE_START - dt.timedelta(days=3)
    rows = [
        _raced(before, "miss", backfill=True),
        _raced(RACE_START),
        _quiet(RACE_START + dt.timedelta(days=1)),
    ]
    board = ds.derive(rows, _ct(RACE_START + dt.timedelta(days=1), 7, 30), _no_corrections)
    assert board["outcome"]["all_time"] == {
        "mornings": 2,
        "booked": 1,
        "miss": 1,
        "exact": 1,
        "fallback": 0,
    }
    assert board["backfill"]["routine_rows"] == 2
    assert board["backfill"]["routine_raced"] == 1


def test_changed_sources_ignores_the_streak_and_the_sliding_window() -> None:
    days = _days(2)
    a = ds.derive([_raced(days[0])], _ct(days[0], 7, 30), _no_corrections)
    b = ds.derive([_raced(days[0]), _quiet(days[1])], _ct(days[1], 7, 30), _no_corrections)
    assert ds.changed_sources(a, b) == []
    c = ds.derive([_raced(days[0]), _raced(days[1], "miss")], _ct(days[1], 7, 30), _no_corrections)
    assert ds.changed_sources(a, c) == ["outcome"]


def test_ledger_row_matches_the_published_board() -> None:
    days = _days(2)
    board = ds.derive([_quiet(d) for d in days], _ct(days[1], 7, 31), _no_corrections)
    row = ds.ledger_row(board, "docs/scoreboard.json")
    assert row["routine"] == "scoreboard"
    assert row["date"] == days[1].isoformat()
    assert row["streak"]["consecutive"] == board["streak"]["consecutive"]
    assert row["cost"]["usd_total"] is None
    ds.validate(row)


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_git_corrections_names_the_commit_after_the_one_that_added_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "-q")
    clean, fixed = "clean.md", "fixed.md"
    for name in (clean, fixed):
        (tmp_path / name).write_text("report\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Add reports")
    (tmp_path / fixed).write_text("corrected\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "Correct the fixed report")
    monkeypatch.chdir(tmp_path)

    found = ds.git_corrections([clean, fixed])
    assert list(found) == [fixed]
    assert "Correct the fixed report" in found[fixed]

    # A report whose PR never merged has no commit, and so no correction:
    # it stays unverified rather than stopping the run.
    assert ds.git_corrections(["never-committed.md"]) == {}

    # Ledger paths are repository-relative, wherever the script runs from.
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    assert list(ds.git_corrections([clean, fixed])) == [fixed]


def test_no_race_report_rows_is_a_failed_read_not_a_zero_board() -> None:
    with pytest.raises(ValueError, match="no race-report rows"):
        ds.derive([_quiet(RACE_START, "scoreboard")], _ct(RACE_START, 9, 0), _no_corrections)


def test_exact_and_fallback_are_published_whether_or_not_slots_were_confirmed() -> None:
    # Maintainer decision, 2026-10-03: the older, unconfirmed mornings count.
    before = RACE_START - dt.timedelta(days=3)
    agreed = _raced(RACE_START, "fallback")
    agreed["confirmed_slots"] = True
    rows = [_raced(before, backfill=True), agreed]
    o = ds.derive(rows, _ct(RACE_START, 7, 30), _no_corrections)["outcome"]
    assert o["all_time"] == {"mornings": 2, "booked": 2, "miss": 0, "exact": 1, "fallback": 1}


def test_backfill_banner_condition_counts_only_routine_races() -> None:
    before = RACE_START - dt.timedelta(days=3)
    quiet = ds.derive(
        [_raced(before, backfill=True), _quiet(RACE_START)], _ct(RACE_START, 7, 30), _no_corrections
    )
    assert quiet["backfill"]["routine_raced"] == 0


def _cost(month: str, usd: float = 37.17, date: str | None = None, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": date or f"{month}-28",
        "routine": "cost",
        "ok": True,
        "month": month,
        "usd_gcp": usd,
        "scope": "gcp_only",
    }
    row.update(kw)
    return row


def _seed(month: str = "2026-09", usd: float = 37.17) -> dict[str, Any]:
    return _cost(month, usd, date="2026-10-09", ok=None, backfill={"basis": "manual cost.sql run"})


def _board(rows: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    result: dict[str, Any] = ds.derive(rows, now, _no_corrections)
    return result


NOW = _ct(dt.date(2026, 10, 10), 7, 30)


def test_cost_is_unavailable_without_a_row_not_zero() -> None:
    cost = _board([_raced(RACE_START)], NOW)["cost"]
    assert cost["available"] is False
    assert cost["usd"] is None and cost["usd_per_booking"] is None


def test_cost_reports_the_newest_month_with_dollars_per_booking() -> None:
    sept = dt.date(2026, 9, 3)
    rows = [
        _raced(sept, backfill=True),
        _raced(sept + dt.timedelta(days=1), "fallback", backfill=True),
    ]
    rows += [_raced(sept + dt.timedelta(days=2), "miss", backfill=True), _seed(usd=40.0)]
    cost = _board(rows, NOW)["cost"]
    assert cost["available"] is True
    assert (cost["month"], cost["usd"], cost["bookings"]) == ("2026-09", 40.0, 2)
    assert cost["usd_per_booking"] == 20.0
    assert cost["scope"] == "gcp_only"
    assert cost["prior_usd"] is None


def test_cost_with_no_bookings_has_no_per_booking_figure() -> None:
    cost = _board([_raced(RACE_START), _seed()], NOW)["cost"]
    assert cost["usd"] == 37.17 and cost["usd_per_booking"] is None


def test_cost_shows_the_prior_month_when_it_has_a_row() -> None:
    rows = [_raced(RACE_START), _seed("2026-09", 37.17), _seed("2026-10", 40.0)]
    rows[2]["date"] = "2026-10-10"
    cost = _board(rows, _ct(dt.date(2026, 11, 2), 9, 0))["cost"]
    assert (cost["month"], cost["prior_month"], cost["prior_usd"]) == ("2026-10", "2026-09", 37.17)


def test_a_failed_cost_row_does_not_replace_the_last_good_figure() -> None:
    failed = _cost("2026-10", date="2026-11-05", ok=False, note="query returned no rows")
    cost = _board([_raced(RACE_START), _seed(), failed], _ct(dt.date(2026, 11, 2), 9, 0))["cost"]
    assert (cost["available"], cost["month"]) == (True, "2026-09")


def test_a_seed_row_stays_outside_the_streak() -> None:
    days = _days(2)
    now = _ct(days[1], 7, 30)
    with_seed = _board([_quiet(d) for d in days] + [_seed()], now)["streak"]
    assert with_seed["consecutive"] == 2
    assert "cost" not in with_seed["by_routine"]


def test_a_missing_monthly_cost_run_breaks_the_streak() -> None:
    start = dt.date(2026, 11, 5)
    rows = [_quiet(d) for d in _days(40)]
    now = _ct(start, 9, 0)
    assert _board(rows, now)["streak"]["last_failure"] == "2026-11-05"
    ok = _cost("2026-10", date="2026-11-05")
    assert _board(rows + [ok], now)["streak"]["by_routine"]["cost"] == 1


def test_monthly_cost_dates_step_by_month() -> None:
    dates = ds.scheduled_dates("cost", [], _ct(dt.date(2027, 1, 6), 9, 0))
    assert dates == [dt.date(2026, 11, 5), dt.date(2026, 12, 5), dt.date(2027, 1, 5)]


def test_a_cost_figure_without_scope_or_month_is_refused() -> None:
    with pytest.raises(ValueError, match="scope"):
        ds.validate(_cost("2026-09", scope=""))
    with pytest.raises(ValueError, match="month"):
        ds.validate(_cost("September", date="2026-09-28"))
    with pytest.raises(ValueError, match="usd_gcp"):
        ds.validate(_cost("2026-09", usd=None))


def test_two_seed_rows_written_on_one_date_are_not_duplicates() -> None:
    rows = [_raced(RACE_START), _seed("2026-08", 71.18), _seed("2026-09", 37.17)]
    cost = _board(rows, NOW)["cost"]
    assert (cost["month"], cost["prior_month"], cost["prior_usd"]) == ("2026-09", "2026-08", 71.18)
    with pytest.raises(ValueError, match="share a Routine and date"):
        _board(rows + [_seed("2026-09", 1.0)], NOW)


def test_a_cost_month_must_be_zero_padded_and_the_amount_finite() -> None:
    with pytest.raises(ValueError, match="month"):
        ds.validate(_cost("2026-9", date="2026-10-09"))
    with pytest.raises(ValueError, match="finite"):
        ds.validate(_cost("2026-09", usd=float("inf")))
    with pytest.raises(ValueError, match="finite"):
        ds.validate(_cost("2026-09", usd=float("nan")))
