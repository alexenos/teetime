"""The osv-scanner exceptions file stays reviewable (#487).

CI's `audit` job fails on any advisory in poetry.lock that `osv-scanner.toml` does
not list. An entry without a reason cannot be reviewed, and one without an expiry
never comes back for review, so both are required here rather than left to habit.
"""

import datetime as dt
import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "osv-scanner.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def _ignored() -> list[dict]:
    return tomllib.loads(CONFIG.read_text(encoding="utf-8")).get("IgnoredVulns", [])


def test_every_exception_has_an_id_a_reason_and_an_expiry() -> None:
    for entry in _ignored():
        assert entry.get("id"), entry
        assert len(entry.get("reason", "").strip()) >= 40, f"{entry.get('id')}: reason too short"
        assert isinstance(
            entry.get("effectiveUntil"), dt.date
        ), f"{entry.get('id')}: effectiveUntil must be a TOML date"


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


def test_the_ci_job_scans_poetry_lock_with_a_pinned_checksummed_binary() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"OSV_SCANNER_VERSION: v\d+\.\d+\.\d+\b", text)
    assert re.search(r"OSV_SCANNER_SHA256: [0-9a-f]{64}\b", text)
    assert "sha256sum -c" in text
    assert "--lockfile=poetry.lock" in text
    assert "--config=osv-scanner.toml" in text
