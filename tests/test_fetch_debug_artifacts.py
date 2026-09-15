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
