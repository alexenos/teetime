"""Block until the Tests workflow on a commit finishes, measured by the wall clock.

    python scripts/wait_for_tests.py --sha <head sha> --since <PR created_at>

Written for the scoreboard Routine after five mornings (2026-11-04, 11-05, 11-08,
11-09, 11-10) on which it wrote ``ok:false`` with a note like "Tests still
in_progress after ~15-30 minutes", while the job finished in about five. The
ledger objects' write times show each session gave up 2 to 4.5 minutes after
opening its PR. The sessions estimated elapsed time from the number of polls
they had made, each counted as the 3-4 minute background sleep it had started,
but they re-polled seconds later instead of waiting for the sleep to finish.
The GitHub API was not stale; the clock the session kept in its head was.

So this script owns both the waiting and the clock. It polls the public Actions
API (the repository is public, so no token is needed) and decides "timed out"
from ``--since`` and the system clock, never from a poll count.

One call waits at most ``--max-wait`` seconds (default 480), which keeps it inside
a shell tool's 10-minute limit. Exit 3 means "still running, the budget is not
spent": call it again with the same arguments. Because the budget is measured
from ``--since``, repeated calls cannot extend it.

Stdlib only. Exit status: 0 Tests passed, 1 Tests finished and did not pass,
2 the budget (``--budget-min`` after ``--since``) is spent with Tests unfinished,
3 this call's ``--max-wait`` ran out first, 4 cannot check (bad arguments, or the
API could not be read on every attempt in this call).
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import sys
import time
import urllib.request
from collections.abc import Callable
from typing import Any

REPO = "alexenos/teetime"
WORKFLOW = "test.yml"

Fetch = Callable[[str], dict[str, Any]]


def parse_time(value: str) -> dt.datetime:
    """Parse an ISO-8601 UTC timestamp such as GitHub's ``2026-11-10T13:35:18Z``."""
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value}")
    return parsed.astimezone(dt.UTC)


def fetch_json(url: str) -> dict[str, Any]:
    """GET a GitHub API URL and decode its JSON body."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "wait-for-tests"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https URL
        result: dict[str, Any] = json.load(response)
        return result


def runs_url(sha: str) -> str:
    """The Actions API listing of Tests runs for one commit, from pull_request events."""
    return (
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/runs"
        f"?head_sha={sha}&event=pull_request&per_page=10"
    )


def newest_run(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The most recently created run in a runs listing, or None when there is none.

    Raises ValueError on a payload of the wrong shape, so that ``wait`` treats it
    as a failed read (exit 4) rather than crashing with exit 1, which a caller
    would read as "Tests failed".
    """
    if not isinstance(payload, dict):
        raise ValueError("Actions API payload is not an object")
    runs: list[dict[str, Any]] = payload.get("workflow_runs") or []
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise ValueError("Actions API workflow_runs is not a list of objects")
    if not runs:
        return None
    return max(runs, key=lambda run: str(run.get("created_at", "")))


def describe(run: dict[str, Any] | None) -> str:
    """One log line's worth of a run's state."""
    if run is None:
        return "no Tests run for this commit yet"
    return (
        f"run {run.get('id')} status={run.get('status')} "
        f"conclusion={run.get('conclusion')} updated={run.get('updated_at')}"
    )


def wait(
    sha: str,
    since: dt.datetime,
    budget: dt.timedelta,
    max_wait: float,
    interval: float,
    fetch: Fetch = fetch_json,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll until Tests finishes, the budget is spent, or this call's wait runs out.

    Returns the exit status documented in the module docstring.
    """
    deadline = since + budget
    call_ends = now() + dt.timedelta(seconds=max_wait)
    read_ok = False
    while True:
        current = now()
        try:
            run = newest_run(fetch(runs_url(sha)))
            read_ok = True
        except (OSError, http.client.HTTPException, ValueError) as exc:
            run = None
            print(f"{current:%H:%M:%SZ} could not read the Actions API: {exc}", flush=True)
        elapsed = int((current - since).total_seconds())
        print(f"{current:%H:%M:%SZ} +{elapsed}s since PR opened: {describe(run)}", flush=True)
        if run is not None and run.get("status") == "completed":
            if run.get("conclusion") == "success":
                print(f"PASSED: {run.get('html_url')}")
                return 0
            print(f"FAILED ({run.get('conclusion')}): {run.get('html_url')}")
            return 1
        if current >= deadline:
            print(
                f"TIMED OUT: still unfinished at {current:%H:%M:%SZ}, "
                f"{elapsed // 60} min after the PR opened (budget {budget})"
            )
            return 2
        if current >= call_ends:
            if not read_ok:
                print("CANNOT CHECK: every read in this call failed")
                return 4
            print(
                f"STILL RUNNING: budget runs to {deadline:%H:%M:%SZ}. "
                "Run this command again with the same arguments."
            )
            return 3
        sleep(max(0.0, min(interval, (min(deadline, call_ends) - current).total_seconds())))


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run ``wait``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--sha", required=True, help="the PR's head commit")
    parser.add_argument("--since", required=True, help="the PR's created_at, ISO-8601 UTC")
    parser.add_argument("--budget-min", type=float, default=25.0)
    parser.add_argument("--max-wait", type=float, default=480.0, help="seconds, per call")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    args = parser.parse_args(argv)
    try:
        since = parse_time(args.since)
    except ValueError as exc:
        print(f"CANNOT CHECK: --since: {exc}")
        return 4
    return wait(
        args.sha,
        since,
        dt.timedelta(minutes=args.budget_min),
        args.max_wait,
        args.interval,
    )


if __name__ == "__main__":
    sys.exit(main())
