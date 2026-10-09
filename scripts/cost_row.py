"""Run operations/routines/cost.sql for one invoice month and emit the cost ledger row.

    python scripts/cost_row.py [--month YYYY-MM] [--seed] [--out cost-row.json]

The month defaults to the one before the current CT month, which is what the cost
Routine reports on the 1st. Stdlib only: the local `bq` is unreliable and the
Routine sandbox has gcloud and nothing else, so this calls the BigQuery REST API
with `gcloud auth print-access-token`.

A failed or empty query still emits a row, with ok false and the reason: a month
with no row is indistinguishable from a month never checked
(operations/routines/cost.md). The exit status is non-zero then as well, so a
caller that only checks it still sees the failure.

--seed marks the row as a manual backfill (ok null, outside the streak), for the
first figure written by hand before the Routine's first scheduled run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT = "gen-lang-client-0822973627"
SQL = Path(__file__).resolve().parents[1] / "operations" / "routines" / "cost.sql"
CT = ZoneInfo("America/Chicago")
SCOPE = "gcp_only"

Row = dict[str, Any]


def previous_month(today: dt.date) -> str:
    return (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")


def build_row(month: str, today: dt.date, rows: list[dict[str, Any]], *, seed: bool = False) -> Row:
    """The ledger row for a query result: one dict per service with currency/gross/net."""
    base: Row = {"date": today.isoformat(), "routine": "cost", "month": month, "scope": SCOPE}
    base["source"] = "bigquery:billing_export"
    if not rows:
        return {**base, "ok": False, "note": f"no billing rows for invoice month {month}"}
    other = sorted({r["currency"] for r in rows if r["currency"] != "USD"})
    if other:
        return {**base, "ok": False, "note": f"non-USD currency in the export: {', '.join(other)}"}
    net = sum(r["net"] for r in rows)
    row: Row = {
        **base,
        "ok": True,
        "usd_gcp": round(net, 2),
        "usd_gcp_gross": round(sum(r["gross"] for r in rows), 2),
        "services": {r["service"]: round(r["net"], 2) for r in rows},
    }
    if seed:
        row["ok"] = None
        row["backfill"] = {"basis": "manual cost.sql run", "written": today.isoformat()}
    return row


def query(month: str) -> list[dict[str, Any]]:
    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
        shell=sys.platform == "win32",
    ).stdout.strip()
    body = {
        "query": SQL.read_text(encoding="utf-8"),
        "useLegacySql": False,
        "location": "US",
        "parameterMode": "NAMED",
        "queryParameters": [
            {
                "name": "invoice_month",
                "parameterType": {"type": "STRING"},
                "parameterValue": {"value": month.replace("-", "")},
            }
        ],
    }
    req = urllib.request.Request(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.load(resp)
    if not result.get("jobComplete"):
        raise RuntimeError("query did not complete within the request timeout")
    return [
        {
            "service": f[0]["v"],
            "currency": f[1]["v"],
            "gross": float(f[2]["v"]),
            "net": float(f[3]["v"]),
        }
        for f in (r["f"] for r in result.get("rows", []))
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    today = dt.datetime.now(CT).date()
    parser.add_argument("--month", default=previous_month(today), help="invoice month, YYYY-MM")
    parser.add_argument("--seed", action="store_true", help="mark as a manual backfill row")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    dt.datetime.strptime(args.month, "%Y-%m")

    try:
        row = build_row(args.month, today, query(args.month), seed=args.seed)
    except (subprocess.CalledProcessError, urllib.error.URLError, RuntimeError, KeyError) as exc:
        row = {
            "date": today.isoformat(),
            "routine": "cost",
            "ok": False,
            "month": args.month,
            "scope": SCOPE,
            "note": f"query failed: {type(exc).__name__}: {exc}",
        }
    text = json.dumps(row)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    if row["ok"] is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
