"""
Pull the debug artifacts and Cloud Run logs a booking post-mortem runs on.

The bot already writes to GCS with nothing but ``google.auth`` and ``httpx``
(:meth:`WaldenProvider._upload_bytes_to_gcs`), so reading them back needs no
``gcloud`` binary and no ``google-cloud-storage`` dependency - the same two
libraries, the same JSON APIs, read-only scopes.

Credentials, in order of preference:
    1. ``GCP_ACCESS_TOKEN`` - a pasted short-lived token
       (``gcloud auth print-access-token``). Nothing is stored; it expires in
       about an hour.
    2. Application Default Credentials - ``GOOGLE_APPLICATION_CREDENTIALS``
       pointing at a service-account key. See docs/debug-artifact-access.md for
       the read-only service account this expects.

Usage::

    python scripts/fetch_debug_artifacts.py list  --date 20260813
    python scripts/fetch_debug_artifacts.py fetch --date 20260813 --out ./artifacts
    python scripts/fetch_debug_artifacts.py logs  --date 2026-08-13 --from 06:20 --to 08:00
    python scripts/fetch_debug_artifacts.py ledger ./artifacts/walden/race/*/ledger.jsonl

A note on the two clocks, because every post-mortem trips over it. Object names
come from :func:`datetime.now` inside Cloud Run, and nothing sets ``TZ`` there,
so **artifact dates are UTC**. The member and the club are on CT. A 6:30 AM CT
run lands under the same date either way; an evening run does not. ``logs``
takes CT wall-clock times and converts them, so pass the times you actually
mean.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

try:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:  # pragma: no cover - only bites outside the project venv
    google = None  # type: ignore[assignment]

DEFAULT_PROJECT = "gen-lang-client-0822973627"
DEFAULT_BUCKET = f"{DEFAULT_PROJECT}-teetime-debug-artifacts"
DEFAULT_SERVICE = "teetime"
CLUB_TZ = ZoneInfo("America/Chicago")

SCOPES = [
    "https://www.googleapis.com/auth/devstorage.read_only",
    "https://www.googleapis.com/auth/logging.read",
]


def access_token() -> str:
    """Return a bearer token, from a pasted token or from ADC."""
    pasted = os.getenv("GCP_ACCESS_TOKEN")
    if pasted:
        return pasted.strip()

    if google is None:
        sys.exit(
            "No GCP_ACCESS_TOKEN set and google-auth is not importable.\n"
            "Run inside the project venv (poetry run) or paste a token:\n"
            "  export GCP_ACCESS_TOKEN=$(gcloud auth print-access-token)"
        )

    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        sys.exit(
            f"Could not obtain credentials: {exc}\n"
            "Set GOOGLE_APPLICATION_CREDENTIALS to a service-account key, or paste a token:\n"
            "  export GCP_ACCESS_TOKEN=$(gcloud auth print-access-token)"
        )

    if not credentials.token:
        sys.exit("Credentials produced no access token.")
    return str(credentials.token)


def list_objects(bucket: str, prefix: str, contains: str | None) -> list[dict]:
    """List objects under a prefix, optionally filtered by a substring."""
    token = access_token()
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o"
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, str] = {"prefix": prefix, "maxResults": "1000"}

    found: list[dict] = []
    with httpx.Client(timeout=60.0) as client:
        while True:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code == 403:
                sys.exit(
                    f"403 listing gs://{bucket}. The credential lacks "
                    "storage.objects.list - grant roles/storage.objectViewer on the bucket."
                )
            resp.raise_for_status()
            body = resp.json()
            for item in body.get("items", []):
                if contains is None or contains in item["name"]:
                    found.append(item)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token

    return sorted(found, key=lambda i: i["name"])


def _contained_destination(root: Path, object_name: str) -> Path | None:
    """Map an object name under ``root``, or None if it would escape.

    Object names are strings the bucket hands us, and GCS is happy to store one
    containing ``../`` segments even though it forbids an object named exactly
    ``..``. Ours never do - :meth:`WaldenProvider._upload_bytes_to_gcs` builds
    them from a fixed prefix and a timestamp - but this writes to a real
    filesystem on the strength of a name we did not construct, so it checks.
    """
    candidate = (root / object_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def download(bucket: str, name: str, destination: Path) -> int:
    """Download one object, returning bytes written."""
    token = access_token()
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}"
    with httpx.Client(timeout=120.0) as client:
        resp = client.get(
            url, params={"alt": "media"}, headers={"Authorization": f"Bearer {token}"}
        )
        resp.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(resp.content)
    return len(resp.content)


def _as_utc_z(moment: datetime) -> str:
    """Format an aware datetime as the RFC3339 UTC the Logging API expects.

    The ``Z`` is a claim about the offset, not decoration. Formatting a Central
    Time value with it directly tells the API 06:20 UTC when the caller meant
    06:20 CT - a five-hour error in summer that lands the window nowhere near
    the booking run and returns an empty result set that reads like "no logs".
    """
    if moment.tzinfo is None:
        raise ValueError("refusing to guess the timezone of a naive datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_logs(project: str, service: str, start: datetime, end: datetime) -> list[tuple[str, str]]:
    """Read Cloud Run log entries between two aware datetimes, oldest first."""
    token = access_token()
    log_filter = (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service}" '
        f'AND timestamp>="{_as_utc_z(start)}" '
        f'AND timestamp<="{_as_utc_z(end)}" '
        'AND textPayload!~"discord\\.gateway"'
    )
    body = {
        "resourceNames": [f"projects/{project}"],
        "filter": log_filter,
        "orderBy": "timestamp asc",
        "pageSize": 1000,
    }

    entries: list[tuple[str, str]] = []
    with httpx.Client(timeout=120.0) as client:
        while True:
            resp = client.post(
                "https://logging.googleapis.com/v2/entries:list",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 403:
                sys.exit(
                    f"403 reading logs for {project}. The credential lacks logging.logEntries.list "
                    "- grant roles/logging.viewer on the project."
                )
            resp.raise_for_status()
            payload = resp.json()
            for entry in payload.get("entries", []):
                text = entry.get("textPayload")
                if text is None:
                    text = json.dumps(entry.get("jsonPayload", {}), default=str)
                entries.append((entry.get("timestamp", ""), text))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            body["pageToken"] = page_token

    return entries


def summarize_ledger(path: Path) -> None:
    """Print a race ledger compactly - one line per attempt, then the boundary."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        print(f"{path}: empty")
        return

    print(f"{path} - {len(rows)} attempt(s)")
    header = f"{'#':>3}  {'sent+ms':>8}  {'server+ms':>9}  {'verdict':<9}  {'slot':<9}  {'form slot':<9}  reason"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.get('attempt', '?'):>3}  "
            f"{row.get('sentMsPastWindow', '?'):>8}  "
            f"{row.get('serverMsPastWindow', '?'):>9}  "
            f"{str(row.get('verdict', '?')):<9}  "
            f"{str(row.get('slot') or '-'):<9}  "
            f"{str(row.get('reservationFormSlot') or '-'):<9}  "
            f"{(row.get('reason') or '')[:60]}"
        )

    refused = [r for r in rows if r.get("verdict") == "refused"]
    granted = [r for r in rows if r.get("verdict") == "accepted"]
    print()
    if granted:
        print(f"GRANTED at +{granted[0].get('sentMsPastWindow', '?')}ms - the club gave us a slot.")
    elif refused:
        offsets = [r["sentMsPastWindow"] for r in refused if r.get("sentMsPastWindow") is not None]
        print(
            f"EVERY attempt refused, out to +{max(offsets) if offsets else '?'}ms. "
            "Refusals past ~2s are the first real evidence of contention."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bucket", default=os.getenv("DEBUG_ARTIFACTS_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list artifacts (optionally for one date)")
    p_list.add_argument("--date", help="UTC date stamp in object names, e.g. 20260813")
    p_list.add_argument("--prefix", default="walden/")

    p_fetch = sub.add_parser("fetch", help="download artifacts for a date")
    p_fetch.add_argument("--date", required=True, help="UTC date stamp, e.g. 20260813")
    p_fetch.add_argument("--prefix", default="walden/")
    p_fetch.add_argument("--out", default="./artifacts", type=Path)

    p_logs = sub.add_parser("logs", help="read Cloud Run logs for a CT window")
    p_logs.add_argument("--date", required=True, help="CT calendar date, e.g. 2026-08-13")
    p_logs.add_argument("--from", dest="start", default="06:20", help="CT start time HH:MM")
    p_logs.add_argument("--to", dest="end", default="08:00", help="CT end time HH:MM")
    p_logs.add_argument("--service", default=DEFAULT_SERVICE)
    p_logs.add_argument("--out", type=Path, help="write here instead of stdout")

    p_ledger = sub.add_parser("ledger", help="summarize a downloaded ledger.jsonl")
    p_ledger.add_argument("path", type=Path)

    args = parser.parse_args()

    if args.command == "list":
        items = list_objects(args.bucket, args.prefix, args.date)
        if not items:
            print(
                f"No objects under {args.prefix}" + (f" matching {args.date}" if args.date else "")
            )
            return
        for item in items:
            print(f"{int(item.get('size', 0)):>10}  {item.get('updated', '')}  {item['name']}")
        print(f"\n{len(items)} object(s)")

    elif args.command == "fetch":
        items = list_objects(args.bucket, args.prefix, args.date)
        if not items:
            print(f"No objects matching {args.date} - nothing was written for that date.")
            return
        root = args.out.resolve()
        written_count = 0
        for item in items:
            destination = _contained_destination(root, item["name"])
            if destination is None:
                print(f"{'SKIPPED':>10}  {item['name']} - would land outside {root}")
                continue
            written = download(args.bucket, item["name"], destination)
            written_count += 1
            print(f"{written:>10} bytes  {destination}")
        print(f"\n{written_count} object(s) -> {root}")

        for item in items:
            if item["name"].endswith("ledger.jsonl"):
                destination = _contained_destination(root, item["name"])
                if destination is not None and destination.exists():
                    print()
                    summarize_ledger(destination)

    elif args.command == "logs":
        day = datetime.strptime(args.date, "%Y-%m-%d")
        start_ct = day.replace(
            hour=int(args.start.split(":")[0]), minute=int(args.start.split(":")[1])
        ).replace(tzinfo=CLUB_TZ)
        end_ct = day.replace(
            hour=int(args.end.split(":")[0]), minute=int(args.end.split(":")[1])
        ).replace(tzinfo=CLUB_TZ)
        if end_ct <= start_ct:
            end_ct += timedelta(days=1)

        entries = read_logs(args.project, args.service, start_ct, end_ct)
        lines = [f"{timestamp} {text}" for timestamp, text in entries]
        if args.out:
            args.out.write_text("\n".join(lines) + "\n")
            print(f"{len(lines)} log line(s) -> {args.out}")
        else:
            print("\n".join(lines))
            print(f"\n{len(lines)} log line(s)", file=sys.stderr)

    elif args.command == "ledger":
        summarize_ledger(args.path)


if __name__ == "__main__":
    main()
