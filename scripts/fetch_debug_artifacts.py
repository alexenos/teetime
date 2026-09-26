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
       pointing at a service-account key. See operations/debug-artifact-access.md for
       the read-only service account this expects.

Usage::

    python scripts/fetch_debug_artifacts.py list  --date 20260813
    python scripts/fetch_debug_artifacts.py fetch --date 20260813 --out ./artifacts
    python scripts/fetch_debug_artifacts.py logs  --date 2026-08-13 --from 06:20 --to 08:00
    python scripts/fetch_debug_artifacts.py ledger ./artifacts/walden/race/*/ledger.jsonl
    python scripts/fetch_debug_artifacts.py observations ./artifacts/walden/observer/2026-09-25/<run id>
    python scripts/fetch_debug_artifacts.py gate --since 20260927

``gate`` answers "where does the gate open, and is it different on different
days": it reads every 06:30 race ledger in the bucket (cached under ``--out``),
brackets each task's slot between the last refusal before its first grant and
that grant - by write time plus lead, the frame the gate setting is tuned in -
and prints the brackets by morning and by weekday, with each morning's CPU and
write-timing record from its ``run.json`` alongside.

The observer's objects live under ``walden/observer/<target date>/<run id>/``,
and the run id starts with the UTC stamp of the morning it ran, so ``--date``
with that morning's stamp lists and fetches them alongside the race ledgers.
``fetch`` parses every observer run it downloads into ``observations.jsonl``
and prints its Northgate flip table (see scripts/observer_observations.py);
``observations`` re-reads one directory with a different course or time range.

A note on the two clocks, because every post-mortem trips over it. Object names
come from :func:`datetime.now` inside Cloud Run, and nothing sets ``TZ`` there,
so **artifact dates are UTC**. The member and the club are on CT. A 6:30 AM CT
run lands under the same date either way; an evening run does not. ``logs``
takes CT wall-clock times and converts them, so pass the times you actually
mean.

A second note, added 2026-09-15: ``logs`` queries a Cloud Run *service*
(``resource.type="cloud_run_revision"``) and Cloud Run *jobs*
(``resource.type="cloud_run_job"``) together, because the booking run is
split across both and which one matters shifts under you. Through 2026-09-14
the whole batch ran inside the ``teetime`` service, reached by Cloud
Scheduler hitting ``/jobs/execute-due-bookings``. As of #184/#204
(2026-09-15) the race itself runs as the ``teetime-racer`` job, one task per
requester; ``teetime-observer`` has been a separate job since #189. A query
scoped to the service alone now returns a clean, silent zero rows on a
morning that raced - which reads exactly like "no booking ran". ``--service``
and ``--jobs`` can narrow it back down when you already know which one you
want.
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
DEFAULT_JOBS = ("teetime-racer", "teetime-observer")
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


def download(bucket: str, name: str, destination: Path, token: str | None = None) -> int:
    """Download one object, returning bytes written.

    ``token`` reuses a bearer token across many downloads; without it each call
    fetches its own, which is a credential refresh per object under ADC.
    """
    token = token or access_token()
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


def _resource_filter(service: str | None, jobs: list[str]) -> str:
    """Match the ``teetime`` service and/or its Cloud Run jobs.

    The two resource types use different label keys (``service_name`` vs.
    ``job_name``), so each becomes its own parenthesized clause and the two
    are OR'd together - a query that only knows about the service goes quiet
    on a morning the race ran as a job, and vice versa. See the module
    docstring's 2026-09-15 note.
    """
    clauses = []
    if service:
        clauses.append(
            f'(resource.type="cloud_run_revision" AND resource.labels.service_name="{service}")'
        )
    if jobs:
        job_match = " OR ".join(f'"{j}"' for j in jobs)
        clauses.append(
            f'(resource.type="cloud_run_job" AND resource.labels.job_name=({job_match}))'
        )
    if not clauses:
        raise ValueError("need at least one of a service or a job to query logs for")
    return " OR ".join(clauses)


def read_logs(
    project: str,
    service: str | None,
    jobs: list[str],
    start: datetime,
    end: datetime,
) -> list[tuple[str, str]]:
    """Read Cloud Run log entries between two aware datetimes, oldest first."""
    token = access_token()
    log_filter = (
        f"({_resource_filter(service, jobs)}) "
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


def morning_race_dirs(items: list[dict], since: str | None = None) -> dict[str, dict[str, str]]:
    """The race directories a 06:30 CT race wrote, oldest first: ``{dir: {file: object}}``.

    Picked by the UTC stamp that starts each directory name, converted to CT, so
    the rule holds across the DST change: a race directory is stamped a few
    seconds after 06:30 CT whichever UTC hour that is. Evening ad-hoc bookings
    write race directories too, and say nothing about the 06:30 gate.
    """
    dirs: dict[str, dict[str, str]] = {}
    for item in items:
        parts = item["name"].split("/")
        if len(parts) != 4 or parts[:2] != ["walden", "race"]:
            continue
        if parts[3] in ("ledger.jsonl", "run.json"):
            dirs.setdefault(parts[2], {})[parts[3]] = item["name"]
    mornings: dict[str, dict[str, str]] = {}
    for run_dir in sorted(dirs):
        files = dirs[run_dir]
        if "ledger.jsonl" not in files or (since and run_dir[:8] < since):
            continue
        try:
            stamp = datetime.strptime(run_dir[:15], "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        local = stamp.astimezone(CLUB_TZ)
        if local.hour == 6 and 25 <= local.minute <= 35:
            mornings[run_dir] = files
    return mornings


def ledger_gate_brackets(rows: list[dict]) -> list[dict]:
    """The racer's GATE_BRACKET, recomputed from a ledger's rows.

    Per slot: the latest refusal written before the earliest-written grant, and
    that grant. Each ask is placed at its write time plus the lead - the frame
    the gate setting (walden_window_opens_offset_ms) is tuned in. Ledgers from
    before 2026-09-27 carry no write time or lead, so their asks are placed at
    the logged send time instead and the entry's ``frame`` says so: those sends
    each paid a ~55ms TCP and TLS handshake before any byte left.

    Only the opening burst counts when the ledger has one: the serial walk after
    it asks at whatever pace the club answers, which says nothing about the gate.
    """
    if any(row.get("burstIndex") is not None for row in rows):
        rows = [row for row in rows if row.get("burstIndex") is not None]
    by_slot: dict[str, list[tuple[int, int, str, bool]]] = {}
    for row in rows:
        wrote = row.get("wroteMsPastWindow")
        if wrote is not None:
            written, arrival, timed = wrote, wrote + (row.get("leadMs") or 0), True
        elif row.get("sentMsPastWindow") is not None:
            written = row["sentMsPastWindow"]
            arrival, timed = written, False
        else:
            continue
        by_slot.setdefault(row.get("slot") or "?", []).append(
            (written, arrival, str(row.get("verdict")), timed)
        )
    brackets: list[dict] = []
    for slot, asks in by_slot.items():
        asks.sort(key=lambda ask: ask[0])
        entry: dict = {
            "slot": slot,
            "asks": len(asks),
            "firstMs": asks[0][1],
            "lastMs": asks[-1][1],
            "frame": "write+lead" if all(ask[3] for ask in asks) else "send",
            "grantedMs": None,
            "refusedBeforeMs": None,
        }
        grants = [ask for ask in asks if ask[2] == "accepted"]
        if grants:
            first = grants[0]
            before = [ask for ask in asks if ask[2] == "refused" and ask[0] < first[0]]
            entry["grantedMs"] = first[1]
            entry["refusedBeforeMs"] = before[-1][1] if before else None
        brackets.append(entry)
    return brackets


def _bracket_phrase(bracket: dict) -> str:
    """One bracket as text: "(+995, +1000]", "<= +815", or "no grant"."""
    if bracket["grantedMs"] is None:
        return "no grant"
    if bracket["refusedBeforeMs"] is None:
        return f"<= {bracket['grantedMs']:+d}"
    return f"({bracket['refusedBeforeMs']:+d}, {bracket['grantedMs']:+d}]"


def _cpu_row(record: dict | None, rows: list[dict]) -> str:
    """A run.json's CPU and write-timing record as one table row's tail.

    A missing run.json means one of two things, told apart by the ledger beside
    it rather than by date. A ledger whose rows have a ``wroteMsPastWindow`` field
    came from code that writes run.json too - the provider uploads the two files
    independently - so its absence is telemetry lost to a failed upload, and
    saying "older race" would hide that. The field's *presence* is the test, not
    its value: on a race where no ask got its bytes out, every row carries it as
    null. A ledger without the field predates run.json. A run.json that exists
    but holds no burst measurements is a race whose opening was not a burst.
    """
    if record is None:
        if any("wroteMsPastWindow" in row for row in rows):
            return "run.json MISSING - this race should have written one; see RACE_LEDGER"
        return "no run.json (the race predates it)"
    timing = record.get("timing") or {}
    writes = timing.get("burstWrites") or {}
    cpu = timing.get("burstCpu") or {}
    environment = cpu.get("environment") or {}
    window = cpu.get("sendWindow") or {}
    if not writes and not cpu:
        return "run.json has no burst measurements (the opening was not a burst)"

    def show(value: object, unit: str = "") -> str:
        return "?" if value is None else f"{value}{unit}"

    return (
        f"cpus {show(environment.get('cgroupCpuLimit') or environment.get('usableCpus'))}  "
        f"warm {show(writes.get('warm'))}/{show(writes.get('members'))}  "
        f"drift max {show(writes.get('driftMsMax'), 'ms')}  "
        f"late>2ms {show(writes.get('lateOver2Ms'))}  "
        f"busy {show(window.get('busyCpus'))}  "
        f"runq {show(window.get('runQueueDelayMs'), 'ms')}  "
        f"psi {show(window.get('cpuPressureMs'), 'ms')}  "
        f"throttled {show(window.get('throttledMs'), 'ms')}"
    )


def print_gate_table(mornings: dict[str, tuple[list[dict], dict | None]]) -> None:
    """Print the brackets by morning, then by weekday, then each morning's CPU record."""
    by_weekday: dict[str, list[tuple[int | None, int]]] = {}
    print(f"{'race dir':<40} {'day':<4} {'slot':<9} {'asks':>4}  {'span':<14} {'gate':<18} frame")
    for run_dir, (rows, _record) in mornings.items():
        day = datetime.strptime(run_dir[:8], "%Y%m%d").strftime("%a")
        for bracket in ledger_gate_brackets(rows):
            span = f"{bracket['firstMs']:+d}..{bracket['lastMs']:+d}"
            print(
                f"{run_dir:<40} {day:<4} {bracket['slot']:<9} {bracket['asks']:>4}  "
                f"{span:<14} {_bracket_phrase(bracket):<18} {bracket['frame']}"
            )
            if bracket["grantedMs"] is not None and bracket["frame"] == "write+lead":
                by_weekday.setdefault(day, []).append(
                    (bracket["refusedBeforeMs"], bracket["grantedMs"])
                )

    print("\nBy weekday (write+lead brackets only; the intersection is where one gate would sit):")
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        brackets = by_weekday.get(day, [])
        if not brackets:
            print(f"  {day}: no measured bracket yet")
            continue
        lower = max((low for low, _high in brackets if low is not None), default=None)
        upper = min(high for _low, high in brackets)
        consistent = lower is None or lower < upper
        print(
            f"  {day}: {len(brackets)} bracket(s), intersection "
            f"{'(' + format(lower, '+d') if lower is not None else '(?'}, {upper:+d}]"
            f"{'' if consistent else '  <- brackets disagree: the gate moved, or asks were reordered'}"
        )

    print("\nCPU and write timing per race (run.json; the second-vCPU question):")
    for run_dir, (rows, record) in mornings.items():
        print(f"  {run_dir:<40} {_cpu_row(record, rows)}")


def summarize_observer_run(
    run_dir: Path,
    course: str | None = "Northgate",
    start: str | None = None,
    end: str | None = None,
) -> None:
    """Write ``observations.jsonl`` for an observer run directory and print its flip table."""
    try:
        try:
            import observer_observations as observations  # run as a script: scripts/ is on sys.path
        except ModuleNotFoundError as exc:
            if exc.name != "observer_observations":
                raise
            from scripts import observer_observations as observations
    except ModuleNotFoundError as exc:
        if exc.name != "bs4":
            raise
        sys.exit(
            "Parsing observer snapshots needs beautifulsoup4, a dev dependency - run inside "
            "the project venv (poetry run)."
        )

    rows = observations.build_observations(run_dir)
    if not rows:
        print(f"{run_dir}: no snapshots to parse")
        return
    path = observations.write_observations(run_dir, rows)
    print(f"{run_dir} - {len(rows)} observation(s) -> {path}")
    print(observations.format_flip_table(rows, course=course, start=start, end=end))


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
    p_logs.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help="Cloud Run service to include, or '' to skip it",
    )
    p_logs.add_argument(
        "--jobs",
        default=",".join(DEFAULT_JOBS),
        help="comma-separated Cloud Run job names to include, or '' to skip them",
    )
    p_logs.add_argument("--out", type=Path, help="write here instead of stdout")

    p_ledger = sub.add_parser("ledger", help="summarize a downloaded ledger.jsonl")
    p_ledger.add_argument("path", type=Path)

    p_obs = sub.add_parser(
        "observations", help="parse a downloaded observer run into observations.jsonl"
    )
    p_obs.add_argument("run_dir", type=Path, help="walden/observer/<target date>/<run id>")
    p_obs.add_argument("--course", default="Northgate", help="course heading, or 'all'")
    p_obs.add_argument("--from", dest="start", help="first tee time to show, HH:MM")
    p_obs.add_argument("--to", dest="end", help="last tee time to show, HH:MM")

    p_gate = sub.add_parser(
        "gate", help="bracket the gate on every 06:30 race, by morning and by weekday"
    )
    p_gate.add_argument("--since", help="first UTC date stamp to include, e.g. 20260927")
    p_gate.add_argument("--out", default="./artifacts", type=Path, help="download cache")

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

        for item in items:
            if item["name"].startswith("walden/observer/") and item["name"].endswith(
                "/manifest.jsonl"
            ):
                destination = _contained_destination(root, item["name"])
                if destination is not None and destination.exists():
                    print()
                    summarize_observer_run(destination.parent)

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

        jobs = [j.strip() for j in args.jobs.split(",") if j.strip()]
        entries = read_logs(args.project, args.service, jobs, start_ct, end_ct)
        lines = [f"{timestamp} {text}" for timestamp, text in entries]
        if args.out:
            args.out.write_text("\n".join(lines) + "\n")
            print(f"{len(lines)} log line(s) -> {args.out}")
        else:
            print("\n".join(lines))
            print(f"\n{len(lines)} log line(s)", file=sys.stderr)

    elif args.command == "ledger":
        summarize_ledger(args.path)

    elif args.command == "observations":
        course = None if args.course.casefold() == "all" else args.course
        summarize_observer_run(args.run_dir, course=course, start=args.start, end=args.end)

    elif args.command == "gate":
        dirs = morning_race_dirs(list_objects(args.bucket, "walden/race/", None), args.since)
        if not dirs:
            print("No 06:30 race ledgers" + (f" since {args.since}" if args.since else ""))
            return
        root = args.out.resolve()
        token = access_token()
        mornings: dict[str, tuple[list[dict], dict | None]] = {}
        for run_dir, files in dirs.items():
            loaded: dict[str, Path] = {}
            for filename, name in files.items():
                destination = _contained_destination(root, name)
                if destination is None:
                    continue
                # Ledgers and run records are written once and never change, so
                # a cached copy is as good as a fresh one.
                if not destination.exists():
                    download(args.bucket, name, destination, token=token)
                loaded[filename] = destination
            ledger = loaded.get("ledger.jsonl")
            if ledger is None:
                continue
            rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
            record_path = loaded.get("run.json")
            record = json.loads(record_path.read_text()) if record_path is not None else None
            mornings[run_dir] = (rows, record)
        print_gate_table(mornings)


if __name__ == "__main__":
    main()
