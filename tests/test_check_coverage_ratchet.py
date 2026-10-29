"""Tests for scripts/check_coverage_ratchet.py, the uncovered-code ratchet."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage_ratchet.py"
_spec = importlib.util.spec_from_file_location("check_coverage_ratchet", _PATH)
assert _spec is not None and _spec.loader is not None
ccr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ccr
_spec.loader.exec_module(ccr)


def _totals(lines: int = 1000, branches: int = 500, percent: float = 80.64) -> dict[str, object]:
    return {"missing_lines": lines, "missing_branches": branches, "percent_covered": percent}


def _run(tmp_path: Path, totals: dict[str, object], ratchet: str) -> int:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": totals}))
    ratchet_file = tmp_path / "ratchet.txt"
    ratchet_file.write_text(ratchet)
    return int(ccr.main(["check", str(report), str(ratchet_file)]))


def test_equal_passes_and_reports_the_percentage() -> None:
    status, message = ccr.check(_totals(), 1500)
    assert status == 0
    assert "1500 uncovered (1000 statements + 500 branches)" in message
    assert "coverage 80.64%" in message


def test_within_the_tolerance_passes_either_way() -> None:
    t = ccr.TOLERANCE
    assert ccr.check(_totals(lines=1000 + t), 1500)[0] == 0
    assert ccr.check(_totals(lines=1000 - t), 1500)[0] == 0
    status, message = ccr.check(_totals(lines=1000 + 1), 1500)
    assert status == 0
    assert "(+1)" in message


def test_more_uncovered_than_the_tolerance_fails() -> None:
    status, message = ccr.check(_totals(lines=1000 + ccr.TOLERANCE + 1), 1500)
    assert status == 1
    assert f"{ccr.TOLERANCE + 1} more than the ratchet 1500" in message


def test_fewer_uncovered_than_the_tolerance_fails_and_names_the_new_value() -> None:
    count = 1500 - ccr.TOLERANCE - 1
    status, message = ccr.check(_totals(lines=count - 500), 1500)
    assert status == 1
    assert f"write {count} to operations/coverage-ratchet.txt" in message


def test_the_tolerance_is_five() -> None:
    # Changing it is a decision (#379 is about removing it), so the test pins it.
    assert ccr.TOLERANCE == 5


def test_deleting_tested_code_does_not_move_the_count() -> None:
    # The percentage rises or falls with the denominator; the uncovered count does not.
    before = _totals(percent=80.6)
    after = _totals(percent=80.2)
    assert ccr.check(before, 1500)[0] == 0
    assert ccr.check(after, 1500)[0] == 0


def test_main_passes_on_equal(tmp_path: Path) -> None:
    assert _run(tmp_path, _totals(), "1500\n") == 0


def test_main_fails_outside_the_band(tmp_path: Path) -> None:
    assert _run(tmp_path, _totals(lines=1000 + ccr.TOLERANCE + 1), "1500\n") == 1
    assert _run(tmp_path, _totals(lines=1000 - ccr.TOLERANCE - 1), "1500\n") == 1


def test_main_passes_inside_the_band(tmp_path: Path) -> None:
    assert _run(tmp_path, _totals(lines=1000 + ccr.TOLERANCE), "1500\n") == 0


@pytest.mark.parametrize("ratchet", ["", "abc\n", "-1\n", "1.5\n", "nan\n"])
def test_main_cannot_check_a_bad_ratchet(tmp_path: Path, ratchet: str) -> None:
    assert _run(tmp_path, _totals(), ratchet) == 2


def test_main_cannot_check_a_report_without_branch_data(tmp_path: Path) -> None:
    assert _run(tmp_path, {"missing_lines": 5, "percent_covered": 90.0}, "5\n") == 2


def test_main_cannot_check_a_missing_report(tmp_path: Path) -> None:
    ratchet_file = tmp_path / "ratchet.txt"
    ratchet_file.write_text("1500\n")
    assert ccr.main(["check", str(tmp_path / "nope.json"), str(ratchet_file)]) == 2


def test_the_committed_ratchet_parses() -> None:
    path = Path(__file__).resolve().parents[1] / "operations" / "coverage-ratchet.txt"
    assert ccr.read_ratchet(path.read_text()) >= 0
