"""Tests for scripts/cost_row.py, which turns a cost.sql result into a ledger row."""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

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
