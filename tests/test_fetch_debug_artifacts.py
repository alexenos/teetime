"""
Tests for scripts/fetch_debug_artifacts.py's log-resource filter.

2026-09-15: the ``logs`` command queried only the ``teetime`` Cloud Run
*service* (``resource.type="cloud_run_revision"``). The morning's batch
booking had moved to the ``teetime-racer`` Cloud Run *job*
(``resource.type="cloud_run_job"``) as of #184/#204, so the query came back
a clean, silent zero rows on a morning that raced - indistinguishable from
"no booking ran" without independently checking the GCS artifacts. These
tests pin the fixed filter so it keeps covering both resource types.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_debug_artifacts.py"
_spec = importlib.util.spec_from_file_location("fetch_debug_artifacts", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
fetch_debug_artifacts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fetch_debug_artifacts
_spec.loader.exec_module(fetch_debug_artifacts)


class TestResourceFilter:
    def test_service_and_jobs_are_ored_together(self) -> None:
        result = fetch_debug_artifacts._resource_filter(
            "teetime", ["teetime-racer", "teetime-observer"]
        )

        assert (
            '(resource.type="cloud_run_revision" AND resource.labels.service_name="teetime")'
            in result
        )
        assert (
            '(resource.type="cloud_run_job" AND resource.labels.job_name=('
            '"teetime-racer" OR "teetime-observer"))' in result
        )
        assert " OR " in result

    def test_service_only_when_jobs_excluded(self) -> None:
        result = fetch_debug_artifacts._resource_filter("teetime", [])

        assert "cloud_run_revision" in result
        assert "cloud_run_job" not in result

    def test_jobs_only_when_service_excluded(self) -> None:
        result = fetch_debug_artifacts._resource_filter("", ["teetime-racer"])

        assert "cloud_run_revision" not in result
        assert "cloud_run_job" in result

    def test_neither_is_an_error_not_a_silent_empty_query(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            fetch_debug_artifacts._resource_filter("", [])


def _item(name: str) -> dict:
    """A bucket listing entry, as list_objects returns them."""
    return {"name": name}


class TestMorningRaceDirs:
    """``gate`` reads the 06:30 CT races and nothing else."""

    def test_only_directories_stamped_at_the_morning_race_are_kept(self) -> None:
        items = [
            # 06:30:08 CT during CDT - a race.
            _item("walden/race/20260925_113008_teetime-racer-p79zv_1/ledger.jsonl"),
            _item("walden/race/20260925_113008_teetime-racer-p79zv_1/run.json"),
            _item("walden/race/20260925_113008_teetime-racer-p79zv_1/attempt_01_refused.xml"),
            # 17:48 CT - an evening ad-hoc booking, which says nothing about the gate.
            _item("walden/race/20260815_224814/ledger.jsonl"),
            # 06:30:05 CT after the DST change, stamped an hour later in UTC.
            _item("walden/race/20261102_123005_teetime-racer-abcde_0/ledger.jsonl"),
            # A directory with no ledger is nothing to read.
            _item("walden/race/20260926_113004_teetime-racer-xyz_0/run.json"),
        ]

        dirs = fetch_debug_artifacts.morning_race_dirs(items)

        assert list(dirs) == [
            "20260925_113008_teetime-racer-p79zv_1",
            "20261102_123005_teetime-racer-abcde_0",
        ]
        assert set(dirs["20260925_113008_teetime-racer-p79zv_1"]) == {"ledger.jsonl", "run.json"}

    def test_since_drops_earlier_mornings(self) -> None:
        items = [
            _item("walden/race/20260924_113004_teetime-racer-a_1/ledger.jsonl"),
            _item("walden/race/20260926_113004_teetime-racer-b_1/ledger.jsonl"),
        ]

        assert list(fetch_debug_artifacts.morning_race_dirs(items, since="20260926")) == [
            "20260926_113004_teetime-racer-b_1"
        ]


class TestLedgerGateBrackets:
    """The racer's GATE_BRACKET, recomputed from ledger rows."""

    def test_a_burst_ledger_brackets_by_write_time_plus_lead(self) -> None:
        rows = [
            {
                "burstIndex": 0,
                "slot": "08:38 AM",
                "verdict": "refused",
                "wroteMsPastWindow": 985,
                "leadMs": 15,
            },
            {
                "burstIndex": 1,
                "slot": "08:38 AM",
                "verdict": "accepted",
                "wroteMsPastWindow": 990,
                "leadMs": 15,
            },
            {
                "burstIndex": 2,
                "slot": "08:38 AM",
                "verdict": "refused",
                "wroteMsPastWindow": 995,
                "leadMs": 15,
            },
            # The serial walk after the burst says nothing about the gate.
            {
                "burstIndex": None,
                "slot": "08:45 AM",
                "verdict": "accepted",
                "wroteMsPastWindow": 4000,
                "leadMs": 15,
            },
        ]

        (bracket,) = fetch_debug_artifacts.ledger_gate_brackets(rows)

        assert bracket["slot"] == "08:38 AM"
        assert (bracket["refusedBeforeMs"], bracket["grantedMs"]) == (1000, 1005)
        assert bracket["frame"] == "write+lead"
        assert fetch_debug_artifacts._bracket_phrase(bracket) == "(+1000, +1005]"

    def test_an_old_ledger_falls_back_to_send_times_and_says_so(self) -> None:
        rows = [
            {"slot": "08:00 AM", "verdict": "refused", "sentMsPastWindow": 0},
            {"slot": "08:00 AM", "verdict": "refused", "sentMsPastWindow": 812},
            {"slot": "08:00 AM", "verdict": "accepted", "sentMsPastWindow": 1291},
        ]

        (bracket,) = fetch_debug_artifacts.ledger_gate_brackets(rows)

        assert bracket["frame"] == "send"
        assert fetch_debug_artifacts._bracket_phrase(bracket) == "(+812, +1291]"

    def test_first_ask_granted_and_no_grant_read_as_such(self) -> None:
        granted_first = [{"slot": "x", "verdict": "accepted", "sentMsPastWindow": 1010}]
        never = [{"slot": "x", "verdict": "refused", "sentMsPastWindow": 1010}]

        (upper_only,) = fetch_debug_artifacts.ledger_gate_brackets(granted_first)
        (nothing,) = fetch_debug_artifacts.ledger_gate_brackets(never)

        assert fetch_debug_artifacts._bracket_phrase(upper_only) == "<= +1010"
        assert fetch_debug_artifacts._bracket_phrase(nothing) == "no grant"


class TestCpuRow:
    """The per-race CPU line tells a lost run.json from one that never existed.

    Found in review on #226: the first version printed "before 2026-09-27" for
    any missing run.json, so a current race whose run.json upload failed - the
    provider uploads it separately from the ledger - read as an old race.
    """

    def test_a_missing_run_json_beside_a_new_ledger_is_lost_telemetry(self) -> None:
        rows = [{"burstIndex": 0, "wroteMsPastWindow": 990, "sentMsPastWindow": 989}]

        assert "MISSING" in fetch_debug_artifacts._cpu_row(None, rows)

    def test_a_new_ledger_whose_every_write_failed_still_reports_missing(self) -> None:
        """No ask got its bytes out, so every write time is null - the field is still there.

        Found in review on #226: testing the value rather than the field's presence
        read this ledger as an old one, hiding a lost run.json on exactly the
        morning it would matter most.
        """
        rows = [
            {"burstIndex": 0, "wroteMsPastWindow": None, "sentMsPastWindow": 815},
            {"burstIndex": 1, "wroteMsPastWindow": None, "sentMsPastWindow": 835},
        ]

        assert "MISSING" in fetch_debug_artifacts._cpu_row(None, rows)

    def test_a_missing_run_json_beside_an_old_ledger_is_history(self) -> None:
        rows = [{"burstIndex": 0, "sentMsPastWindow": 1015}]

        assert fetch_debug_artifacts._cpu_row(None, rows) == "no run.json (the race predates it)"

    def test_a_run_json_without_burst_measurements_says_so(self) -> None:
        row = fetch_debug_artifacts._cpu_row({"timing": {"openingMode": "ladder"}}, [])

        assert row == "run.json has no burst measurements (the opening was not a burst)"

    def test_a_full_record_reads_as_one_line(self) -> None:
        record = {
            "timing": {
                "burstWrites": {"members": 28, "warm": 28, "driftMsMax": 1, "lateOver2Ms": 0},
                "burstCpu": {
                    "environment": {"cgroupCpuLimit": 2.0},
                    "sendWindow": {"busyCpus": 0.4, "runQueueDelayMs": 0.2},
                },
            }
        }

        row = fetch_debug_artifacts._cpu_row(record, [])

        assert "cpus 2.0" in row
        assert "warm 28/28" in row
        assert "drift max 1ms" in row
        assert "runq 0.2ms" in row
