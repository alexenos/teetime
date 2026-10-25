"""Tests for scripts/check_coverage_floor.py, the coverage high-water-mark check."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage_floor.py"
_spec = importlib.util.spec_from_file_location("check_coverage_floor", _PATH)
assert _spec is not None and _spec.loader is not None
ccf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ccf
_spec.loader.exec_module(ccf)


def _run(tmp_path: Path, percent: float, floor: str) -> int:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": percent}}))
    floor_file = tmp_path / "floor.txt"
    floor_file.write_text(floor)
    return int(ccf.main(["check", str(report), str(floor_file)]))


def test_rounds_down_not_to_nearest() -> None:
    assert ccf.round_down(81.19) == 81.1
    assert ccf.round_down(81.99) == 81.9
    assert ccf.round_down(81.0) == 81.0


def test_equal_passes() -> None:
    assert ccf.check(81.04, 81.0)[0] == 0


def test_below_fails() -> None:
    status, message = ccf.check(80.99, 81.0)
    assert status == 1
    assert "below the floor" in message


def test_above_fails_and_names_the_new_floor() -> None:
    status, message = ccf.check(81.26, 81.0)
    assert status == 1
    assert "write 81.2 to operations/coverage-floor.txt" in message


def test_main_passes_on_equal(tmp_path: Path) -> None:
    assert _run(tmp_path, 81.04, "81.0\n") == 0


def test_main_fails_below_and_above(tmp_path: Path) -> None:
    assert _run(tmp_path, 79.9, "81.0\n") == 1
    assert _run(tmp_path, 82.5, "81.0\n") == 1


@pytest.mark.parametrize("floor", ["", "abc\n"])
def test_main_cannot_check_a_bad_floor(tmp_path: Path, floor: str) -> None:
    assert _run(tmp_path, 81.0, floor) == 2


def test_main_cannot_check_a_missing_report(tmp_path: Path) -> None:
    floor_file = tmp_path / "floor.txt"
    floor_file.write_text("81.0\n")
    assert ccf.main(["check", str(tmp_path / "nope.json"), str(floor_file)]) == 2


def test_the_committed_floor_parses() -> None:
    floor = Path(__file__).resolve().parents[1] / "operations" / "coverage-floor.txt"
    assert ccf.read_floor(floor.read_text()) > 0
