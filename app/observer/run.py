"""The observer run: pick a date, get warm early, photograph, then upload.

Ordering is a safety property, not a preference. Cloud Scheduler starts this
job at 06:24 and the racer at 06:28, so the observer's login is always the
*older* of the two. Concurrent sessions on one credential are proven fine in
production - a member sits logged in watching while the bot logs in and books,
and is never kicked - but if the club ever did begin enforcing one session per
member, the newer login wins and the casualty would be the observer rather than
somebody's tee time. The observer therefore goes first and stays expendable.

The session is this job's own, in this job's own container: never the racer's.
A Reserve body addresses its slot positionally (``teeTimeSlots:11`` is "the
twelfth row of whatever date this view shows") and the date rides in session
state, so re-pointing a shared view mid-race would reserve the wrong tee time
with no error at all.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta

from app.config import settings
from app.observer import artifacts, sheet
from app.services.credential_service import credential_service
from app.services.database_service import database_service
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

# How far ahead of the window the *racer* logs in - the 06:28 of
# google_cloud_scheduler_job.execute_bookings, against a 06:30 window. Not a
# setting: it is a fact about the other job's schedule, and the observer only
# reads it to check that its own login came first. Keep in step with that
# scheduler entry if it ever moves.
RACER_LOGIN_LEAD_S = 120


def _window_instant(now_ct: datetime) -> datetime:
    """This morning's stated window open, 06:30:00.000 CT.

    The *stated* window, deliberately - not the racer's aiming point
    (``walden_window_opens_offset_ms``, which leads the tick by a second). The
    observer's timestamps have to be an independent reference frame, or they
    cannot be used to check the racer's.
    """
    return now_ct.replace(
        hour=settings.booking_open_hour,
        minute=settings.booking_open_minute,
        second=0,
        microsecond=0,
    )


async def _resolve_target(window_ct: datetime) -> tuple[date, str, str] | None:
    """Which date to watch, and the login to watch it with.

    Returns ``(target_date, member_number, requester)``, or None when no
    credential is available at all.

    The date comes from the booking the racer will run this morning, so the
    observer always parks on the sheet that job is racing for. When the
    configured member has nothing due, it falls back to the date that opens
    today anyway (``today + days_in_advance``): the control group is the cheap
    half of this job's value - a non-Friday morning shows what an uncontested
    gate looks like - and it should not be lost just because nobody asked for a
    tee time that day.
    """
    requester = settings.observer_phone_number or settings.user_phone_number

    due = await database_service.get_due_bookings(CTDateTime.to_naive_ct(window_ct))
    scoped = [b for b in due if not requester or b.phone_number == requester]
    scoped.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))

    if scoped:
        booking = scoped[0]
        target_date = booking.request.requested_date
        if len(scoped) > 1:
            logger.info(
                "OBSERVER: %d due bookings for %s; watching the earliest (%s %s)",
                len(scoped),
                _redacted(requester),
                target_date,
                booking.request.requested_time.strftime("%I:%M %p"),
            )
        logger.info(
            "OBSERVER: watching %s, the date booking %s races for at %s",
            target_date,
            booking.id,
            booking.request.requested_time.strftime("%I:%M %p"),
        )
        requester = booking.phone_number
    else:
        target_date = window_ct.date() + timedelta(days=settings.days_in_advance)
        logger.info(
            "OBSERVER: no booking due for %s this morning - watching %s anyway "
            "(today + %d days), which is the sheet opening at the window",
            _redacted(requester),
            target_date,
            settings.days_in_advance,
        )

    credentials = await credential_service.resolve(requester)
    if credentials is None:
        logger.error(
            "OBSERVER: no Walden credentials resolve for %s; nothing to log in with",
            _redacted(requester),
        )
        return None

    return target_date, credentials.member_number, credentials.password


def _redacted(requester: str) -> str:
    """A requester identity in a form safe to leave in Cloud Logging.

    ``requester`` is a phone number on Twilio and a numeric account id on
    Telegram, and the observer's logs exist to say *which* member was watched,
    which the last four characters answer. Whole identities in logs are
    CWE-532; this keeps the diagnostic value without adding another copy of the
    identifier to a log sink that is retained for months.

    Note that this is not a project-wide convention yet - booking_service still
    logs a resolved proxy target's phone number in full. Narrowing that is out
    of scope here, but it is the reason this helper is local rather than shared.
    """
    if not requester:
        return "any requester"
    tail = requester[-4:]
    return f"...{tail}" if len(requester) > 4 else tail


def _run_id() -> str:
    """An identifier for this execution, unique even against a concurrent twin.

    Stamped from ``datetime.now()`` in a container with no ``TZ``, so it is
    UTC, matching every other object name this project writes. Called once at
    the start of the run rather than when the artifacts are written, so a job
    that begins at 06:24 CDT reads ``1124`` - the time an operator would look
    for - instead of the ~06:30:08 the last snapshot finishes at.

    A second of precision is not enough on its own. Two executions of a Cloud
    Run job can overlap (there is no built-in singleton), and the artifacts
    bucket is written with ``roles/storage.objectCreator``, which grants create
    but not delete - so a colliding prefix does not merge, it makes the second
    run's uploads fail outright. ``CLOUD_RUN_EXECUTION`` is Cloud Run's own name
    for the execution, which is both unique and the handle the console and logs
    use, so an artifact directory can be traced back to exactly one run; a short
    random suffix stands in for it during a local dry run.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    execution = os.getenv("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:8]}"
    return f"{stamp}_{execution}"


def _object_prefix(target_date: date, run_id: str) -> str:
    """Where this run's artifacts live.

    ``walden/observer/<target date>/<run id>/`` - the issue names the target
    date as the first level, and the run id below it keeps a dry run from
    overwriting the real morning when both watch the same sheet.
    """
    return f"walden/observer/{target_date.isoformat()}/{run_id}"


def _store(
    prefix: str,
    prep: sheet.Preparation,
    snapshots: list[sheet.Snapshot],
    window_epoch_ms: int,
) -> int:
    """Write the snapshots and the run record to GCS, after the window.

    Returns the number of snapshots actually stored, which the caller turns
    into the job's exit status.

    Every upload is independent: one object failing must not cost the other
    eight, because a partial morning still separates the two models. But a run
    that stored *nothing* has to fail, and that is why this returns a count
    rather than swallowing the outcome. The only product of this job is
    evidence; a morning that captured nine snapshots, lost every upload and
    exited 0 would be indistinguishable from a good one until someone went
    looking for the bytes a Friday later.
    """
    bucket = artifacts.artifacts_bucket()
    if not bucket:
        logger.error(
            "OBSERVER: DEBUG_ARTIFACTS_BUCKET is unset - %d snapshot(s) captured and "
            "discarded, so this run produced nothing",
            len(snapshots),
        )
        return 0

    stored = 0
    for snap in snapshots:
        name = f"{prefix}/snapshot_+{snap.sent_offset_ms:04d}ms.html"
        try:
            artifacts.upload_bytes(
                bucket_name=bucket,
                object_name=name,
                content_type="text/html; charset=utf-8",
                data=snap.html,
            )
            stored += 1
        except Exception as e:  # noqa: BLE001 - one lost snapshot is not a lost morning
            logger.warning("OBSERVER: failed to store %s: %s", name, e)

    run_record = {
        "targetDate": prep.target_date.isoformat(),
        "windowEpochMs": window_epoch_ms,
        "windowCt": datetime.fromtimestamp(window_epoch_ms / 1000, tz=CTDateTime.CT_TZ).isoformat(),
        "selectedTabText": prep.selected_tab_text,
        "clickedTab": prep.clicked_tab,
        "northgateRowCount": prep.northgate_row_count,
        "preWindowSheetBytes": prep.sheet_bytes,
        "readyAtOffsetMs": prep.ready_at_epoch_ms - window_epoch_ms,
        "snapshotsCaptured": len(snapshots),
        "snapshotsStored": stored,
        "notes": prep.notes,
    }
    rows = [
        {
            "index": s.index,
            "plannedOffsetMs": s.planned_offset_ms,
            "sentOffsetMs": s.sent_offset_ms,
            "settledOffsetMs": s.settled_offset_ms,
            "capturedOffsetMs": s.captured_offset_ms,
            "bytes": len(s.html),
            "refreshOk": s.refresh_ok,
            "object": f"snapshot_+{s.sent_offset_ms:04d}ms.html",
            "note": s.note,
        }
        for s in snapshots
    ]

    for name, content_type, data in (
        (
            f"{prefix}/run.json",
            "application/json; charset=utf-8",
            json.dumps(run_record, indent=2, default=str).encode("utf-8"),
        ),
        (
            f"{prefix}/manifest.jsonl",
            "application/x-ndjson; charset=utf-8",
            ("\n".join(json.dumps(r, default=str) for r in rows) + "\n").encode("utf-8"),
        ),
    ):
        try:
            artifacts.upload_bytes(
                bucket_name=bucket, object_name=name, content_type=content_type, data=data
            )
        except Exception as e:  # noqa: BLE001 - see above
            logger.warning("OBSERVER: failed to store %s: %s", name, e)

    log = logger.info if stored == len(snapshots) else logger.error
    log(
        "OBSERVER: stored %d/%d snapshot(s) under gs://%s/%s/",
        stored,
        len(snapshots),
        bucket,
        prefix,
    )
    return stored


def _report_readiness(ready_ct: datetime, window_ct: datetime) -> None:
    """Say when the observer became warm, and shout if that was too late.

    Three outcomes, and the middle one is the reason this is not a one-line log:

    * Ready before the racer logs in - the intended case, and the one the
      fail-safe ordering depends on.
    * Ready *after* the racer logs in. The observer is then the newer session on
      the shared credential, which inverts the whole point of starting at 06:24:
      were the club ever to enforce one session per member, the casualty would be
      the booking rather than the observer. Nothing can be undone at this point -
      the login already happened - so this is logged at ERROR to be found, and
      the schedule wants moving earlier if it recurs.
    * Ready after the window itself, which additionally means the early
      snapshots - the ones that decide between the two models - are gone.
    """
    racer_login_ct = window_ct - timedelta(seconds=RACER_LOGIN_LEAD_S)

    if ready_ct >= window_ct:
        logger.error(
            "OBSERVER: only ready at %s CT, %.1fs PAST the window - the early snapshots "
            "are already gone, and this run logged in after the racer did",
            ready_ct.strftime("%H:%M:%S.%f")[:-3],
            (ready_ct - window_ct).total_seconds(),
        )
    elif ready_ct >= racer_login_ct:
        logger.error(
            "OBSERVER: ready at %s CT, which is after the racer's %s CT login - the "
            "observer is now the NEWER session on this credential, inverting the "
            "fail-safe ordering. Move observer_schedule earlier.",
            ready_ct.strftime("%H:%M:%S.%f")[:-3],
            racer_login_ct.strftime("%H:%M:%S"),
        )
    else:
        logger.info(
            "OBSERVER: ready at %s CT - %.1fs before the racer logs in, then idle "
            "%.1fs to the window",
            ready_ct.strftime("%H:%M:%S.%f")[:-3],
            (racer_login_ct - ready_ct).total_seconds(),
            (window_ct - ready_ct).total_seconds(),
        )


async def observe() -> bool:
    """One morning's observation. Returns whether it produced stored evidence."""
    if not settings.observer_enabled:
        logger.info("OBSERVER: observer_enabled is false - nothing to do")
        return True

    run_id = _run_id()
    now_ct = CTDateTime.now()
    window_ct = _window_instant(now_ct)
    window_epoch_ms = int(window_ct.timestamp() * 1000)
    logger.info(
        "OBSERVER: run %s started %s CT; window at %s CT (%+.1fs away)",
        run_id,
        now_ct.strftime("%H:%M:%S.%f")[:-3],
        window_ct.strftime("%H:%M:%S.%f")[:-3],
        (window_ct - now_ct).total_seconds(),
    )

    resolved = await _resolve_target(window_ct)
    if resolved is None:
        return False
    target_date, member_number, password = resolved

    driver = await asyncio.to_thread(sheet.create_driver)
    try:
        if not await asyncio.to_thread(sheet.log_in, driver, member_number, password):
            return False
        if not await asyncio.to_thread(sheet.open_tee_sheet, driver):
            return False

        prep = await asyncio.to_thread(sheet.park_on_date, driver, target_date)
        if prep is None:
            return False

        _report_readiness(CTDateTime.now(), window_ct)

        snapshots = await asyncio.to_thread(
            sheet.capture_across_window,
            driver,
            window_epoch_ms=window_epoch_ms,
            count=settings.observer_snapshot_count,
            interval_ms=settings.observer_snapshot_interval_ms,
        )
    finally:
        try:
            driver.quit()
        except Exception as e:  # noqa: BLE001 - the run is over; a stuck browser is not news
            logger.warning("OBSERVER: browser did not shut down cleanly: %s", e)

    if not snapshots:
        logger.error("OBSERVER: no snapshots were captured")
        return False

    stored = await asyncio.to_thread(
        _store, _object_prefix(target_date, run_id), prep, snapshots, window_epoch_ms
    )
    # Partial storage still separates the two models, so a morning that lost one
    # upload stays green. Losing every one of them is a failed run: the bytes
    # were the only thing this job was for.
    return stored > 0


def main() -> int:
    """Entry point for ``python -m app.observer``."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        ok = asyncio.run(observe())
    except Exception:
        logger.exception("OBSERVER: run failed")
        return 1
    return 0 if ok else 1
