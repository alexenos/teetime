"""The osv-scanner exceptions file stays reviewable (#487).

CI's `audit` job fails on any advisory in poetry.lock that `.github/osv-scanner.toml`
does not list. An entry without a reason cannot be reviewed, and one without an
expiry never comes back for review, so both are required here rather than left to
habit. The file lives under `.github/` so that renewing an exception fires no
deploy (Cloud Build ignores `.github/**`; CLAUDE.md, Constraints).
"""

import datetime as dt
import pathlib
import re
import tomllib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".github" / "osv-scanner.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def _ignored() -> list[dict]:
    return tomllib.loads(CONFIG.read_text(encoding="utf-8")).get("IgnoredVulns", [])


def test_every_exception_has_an_id_a_reason_and_an_expiry() -> None:
    for entry in _ignored():
        assert entry.get("id"), entry
        assert len(entry.get("reason", "").strip()) >= 40, f"{entry.get('id')}: reason too short"
        until = entry.get("effectiveUntil")
        # datetime subclasses date, so a TOML datetime would pass a bare date check
        # and then fail the comparison below with a TypeError instead of this message.
        assert isinstance(until, dt.date) and not isinstance(
            until, dt.datetime
        ), f"{entry.get('id')}: effectiveUntil must be a TOML date (YYYY-MM-DD)"


def test_no_exception_uses_the_deprecated_ignore_until() -> None:
    # osv-scanner 2.7.1 deprecated ignoreUntil in favour of effectiveUntil.
    for entry in _ignored():
        assert "ignoreUntil" not in entry, entry.get("id")


def test_an_exception_is_granted_for_at_most_six_months() -> None:
    # Measured against today, not against when the entry was added (that would need
    # git). So it catches an entry written with a far-off date when it is added,
    # which is when it matters; it cannot tell an old entry from a new one.
    limit = dt.date.today() + dt.timedelta(days=183)
    for entry in _ignored():
        assert entry["effectiveUntil"] <= limit, f"{entry['id']}: more than six months out"


def _audit_job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow.get("jobs", {}).get("audit")
    assert job, "test.yml has no audit job"
    return job


def test_the_audit_job_pins_and_checksums_the_binary() -> None:
    job = _audit_job()
    env = job.get("env", {})
    assert re.fullmatch(r"v\d+\.\d+\.\d+", str(env.get("OSV_SCANNER_VERSION", "")))
    assert re.fullmatch(r"[0-9a-f]{64}", str(env.get("OSV_SCANNER_SHA256", "")))
    runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    assert "sha256sum -c" in runs


def test_the_audit_job_scans_poetry_lock_with_the_exceptions_file() -> None:
    runs = [str(step.get("run", "")) for step in _audit_job().get("steps", [])]
    scans = [run for run in runs if "osv-scanner scan" in run]
    assert scans, "the audit job runs no osv-scanner scan"
    for run in scans:
        assert "--lockfile=poetry.lock" in run
        assert "--config=.github/osv-scanner.toml" in run
