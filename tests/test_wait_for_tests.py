"""Tests for scripts/wait_for_tests.py, the scoreboard Routine's wall-clock CI wait."""

import datetime as dt
import importlib.util
import sys
import urllib.error
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wait_for_tests.py"
_spec = importlib.util.spec_from_file_location("wait_for_tests", _PATH)
assert _spec is not None and _spec.loader is not None
wft = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = wft
_spec.loader.exec_module(wft)

SINCE = dt.datetime(2026, 11, 10, 13, 35, 18, tzinfo=dt.UTC)


class Clock:
    """A fake clock that only moves when the code under test sleeps."""

    def __init__(self, start: dt.datetime) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> dt.datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += dt.timedelta(seconds=seconds)


def _run(
    status: str, conclusion: str | None = None, created: str = "2026-11-10T13:35:21Z"
) -> dict[str, Any]:
    return {"id": 1, "status": status, "conclusion": conclusion, "created_at": created}


def _fetch_sequence(*payloads: Any) -> Any:
    items = list(payloads)

    def fetch(url: str) -> dict[str, Any]:
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return {"workflow_runs": item}

    return fetch


def _wait(fetch: Any, clock: Clock, budget_min: float = 25, max_wait: float = 480) -> int:
    return wft.wait(
        "abc",
        SINCE,
        dt.timedelta(minutes=budget_min),
        max_wait,
        30,
        fetch=fetch,
        now=clock.now,
        sleep=clock.sleep,
    )


def test_waits_through_in_progress_until_success() -> None:
    # The 2026-11-10 shape: in_progress for about four minutes, then success.
    clock = Clock(SINCE + dt.timedelta(minutes=1))
    fetch = _fetch_sequence(*([[_run("in_progress")]] * 8), [_run("completed", "success")])
    assert _wait(fetch, clock) == 0
    assert sum(clock.sleeps) == 8 * 30


def test_failure_conclusion_exits_1() -> None:
    clock = Clock(SINCE)
    assert _wait(_fetch_sequence([_run("completed", "failure")]), clock) == 1


def test_budget_is_measured_from_since_not_from_poll_count() -> None:
    # Starting 24 minutes after the PR opened leaves one minute of a 25-minute budget.
    clock = Clock(SINCE + dt.timedelta(minutes=24))
    assert _wait(_fetch_sequence([_run("in_progress")]), clock) == 2
    assert sum(clock.sleeps) == 60


def test_call_limit_exits_3_before_budget_is_spent() -> None:
    clock = Clock(SINCE)
    assert _wait(_fetch_sequence([_run("queued")]), clock, max_wait=90) == 3
    assert clock.t - SINCE == dt.timedelta(seconds=90)


def test_no_run_yet_keeps_waiting() -> None:
    clock = Clock(SINCE)
    fetch = _fetch_sequence([], [_run("completed", "success")])
    assert _wait(fetch, clock) == 0


def test_newest_run_wins_over_an_older_rerun() -> None:
    old = _run("completed", "failure", created="2026-11-10T13:35:21Z")
    new = _run("completed", "success", created="2026-11-10T13:50:00Z")
    assert _wait(_fetch_sequence([old, new]), Clock(SINCE)) == 0


def test_transient_read_error_is_retried() -> None:
    clock = Clock(SINCE)
    fetch = _fetch_sequence(urllib.error.URLError("reset"), [_run("completed", "success")])
    assert _wait(fetch, clock) == 0


def test_every_read_failing_exits_4() -> None:
    clock = Clock(SINCE)
    assert _wait(_fetch_sequence(urllib.error.URLError("down")), clock, max_wait=60) == 4


def test_since_without_timezone_exits_4() -> None:
    assert wft.main(["--sha", "abc", "--since", "2026-11-10T13:35:18"]) == 4


def test_parse_time_reads_github_format() -> None:
    assert wft.parse_time("2026-11-10T13:35:18Z") == SINCE


def test_malformed_payload_is_a_failed_read_not_a_test_failure() -> None:
    clock = Clock(SINCE)

    def fetch(url: str) -> Any:
        return {"workflow_runs": ["not a run"]}

    assert _wait(fetch, clock, max_wait=60) == 4


def test_non_object_payload_is_a_failed_read() -> None:
    clock = Clock(SINCE)
    assert _wait(lambda url: [], clock, max_wait=60) == 4


def test_null_workflow_runs_is_a_failed_read_not_an_empty_listing() -> None:
    clock = Clock(SINCE)
    assert _wait(lambda url: {"workflow_runs": None}, clock, max_wait=60) == 4


def test_reads_failing_until_the_budget_is_spent_exits_4_not_2() -> None:
    clock = Clock(SINCE + dt.timedelta(minutes=24))
    assert _wait(_fetch_sequence(urllib.error.URLError("down")), clock) == 4
