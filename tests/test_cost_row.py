"""Tests for scripts/cost_row.py, which turns a cost.sql result into a ledger row."""

import datetime as dt
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cost_row.py"
_spec = importlib.util.spec_from_file_location("cost_row", _PATH)
assert _spec is not None and _spec.loader is not None
cr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cr
_spec.loader.exec_module(cr)

TODAY = dt.date(2026, 11, 1)


def _svc(name: str, gross: float, net: float, currency: str = "USD") -> dict[str, object]:
    return {"service": name, "currency": currency, "gross": gross, "net": net}


def test_previous_month_crosses_a_year_boundary() -> None:
    assert cr.previous_month(dt.date(2026, 11, 1)) == "2026-10"
    assert cr.previous_month(dt.date(2027, 1, 1)) == "2026-12"


def test_row_sums_net_across_services_and_keeps_the_breakdown() -> None:
    rows = [_svc("Cloud Run", 23.87, 18.65), _svc("Cloud SQL", 9.26, 9.26)]
    row = cr.build_row("2026-10", TODAY, rows)
    assert row["ok"] is True
    assert row["usd_gcp"] == 27.91 and row["usd_gcp_gross"] == 33.13
    assert row["services"] == {"Cloud Run": 18.65, "Cloud SQL": 9.26}
    assert row["scope"] == "gcp_only" and row["month"] == "2026-10"


def test_an_empty_result_is_a_failed_row_not_a_zero() -> None:
    row = cr.build_row("2026-10", TODAY, [])
    assert row["ok"] is False and "no billing rows" in row["note"]
    assert "usd_gcp" not in row


def test_a_non_usd_row_is_not_reported_as_dollars() -> None:
    row = cr.build_row("2026-10", TODAY, [_svc("Cloud Run", 1, 1), _svc("X", 1, 1, "EUR")])
    assert row["ok"] is False and "EUR" in row["note"]


def test_a_seed_row_is_a_backfill_with_no_ok() -> None:
    row = cr.build_row("2026-09", dt.date(2026, 10, 9), [_svc("Cloud SQL", 9, 9)], seed=True)
    assert row["ok"] is None and "backfill" in row and row["usd_gcp"] == 9.0


def test_main_rejects_a_month_that_is_not_zero_padded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["cost_row.py", "--month", "2026-9"])
    with pytest.raises(SystemExit) as exc:
        cr.main()
    assert exc.value.code == 2


class _Resp:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_an_unfinished_job_is_polled_not_recorded_as_a_failed_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = {
        "jobComplete": True,
        "rows": [{"f": [{"v": "Cloud SQL"}, {"v": "USD"}, {"v": "9.0"}, {"v": "9.0"}]}],
    }
    replies = [
        {"jobComplete": False, "jobReference": {"jobId": "j1", "location": "US"}},
        {"jobComplete": False, "jobReference": {"jobId": "j1", "location": "US"}},
        done,
    ]
    urls: list[str] = []

    def fake_urlopen(req: Any, timeout: int) -> _Resp:
        urls.append(req.full_url)
        return _Resp(replies.pop(0))

    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="t"))
    monkeypatch.setattr(cr.urllib.request, "urlopen", fake_urlopen)
    rows = cr.query("2026-09")
    assert rows == [{"service": "Cloud SQL", "currency": "USD", "gross": 9.0, "net": 9.0}]
    assert "/queries/j1?" in urls[1] and len(urls) == 3


def test_a_job_that_never_finishes_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = {"jobComplete": False, "jobReference": {"jobId": "j1"}}
    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="t"))
    monkeypatch.setattr(cr.urllib.request, "urlopen", lambda req, timeout: _Resp(pending))
    with pytest.raises(RuntimeError, match="still running"):
        cr.query("2026-09")
