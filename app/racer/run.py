"""One racer task: claim one requester's due bookings, race them, report them.

The morning race used to run every requester's group one after another inside
the booking service. On 2026-09-13 that put the second requester's Reserves at
about +74s, long after the slot was decided. It cannot be fixed by running the
groups concurrently in one process either: every group lands on the same three
seconds, and N headless Chromes contending for one vCPU is the worst possible
shape for them (a co-resident Chrome was measured taking 310ms from one attempt
on 2026-08-28).

So the race is a Cloud Run job whose tasks each run this module once, in their
own container. A task claims exactly one ``(date, requester)`` group - see
``DatabaseService.claim_next_due_group`` - and a task that finds nothing left
exits straight away. Nothing is shared at race time: container, browser,
session and credential are all this task's own, so one crashed browser costs
one requester's booking rather than everyone's.

The booking work itself is not reimplemented here. It is the same
``run_bookings_and_report`` the ``/jobs/execute-due-bookings`` endpoint runs,
with the same messages, timeout and unreported-booking handling.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

from app.api.jobs import run_bookings_and_report
from app.config import settings
from app.log_safety import silence_wire_loggers
from app.providers.setup import install_reservation_provider
from app.services.booking_service import booking_service
from app.services.database_service import database_service
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

# When a task logs in, relative to the window: 06:28, exactly when the service
# path has always logged in. The job itself is triggered earlier than that
# because a Cloud Run job took about two minutes from trigger to a running
# process on 2026-09-13, but the login is held to 06:28 so the session ages
# exactly as it always has - this change is topology only, and the next ledgers
# stay comparable with every earlier one. It also keeps the observer's 06:24
# login the older of the two sessions on a shared credential; the observer's
# RACER_LOGIN_LEAD_S mirrors this value.
LOGIN_LEAD_S = 120


def _window_instant(now_ct: datetime) -> datetime:
    """This morning's stated window open, 06:30:00.000 CT."""
    return now_ct.replace(
        hour=settings.booking_open_hour,
        minute=settings.booking_open_minute,
        second=0,
        microsecond=0,
    )


def _task_label() -> str:
    """Which task of the execution this is, for the logs.

    Cloud Run sets both variables on every job task. A local run has neither.
    """
    index = os.getenv("CLOUD_RUN_TASK_INDEX")
    if index is None:
        return "local run"
    return f"task {index}/{os.getenv('CLOUD_RUN_TASK_COUNT', '?')}"


async def _hold_until_login(window_ct: datetime) -> None:
    """Wait until LOGIN_LEAD_S before the window, or go at once if already past it."""
    login_ct = window_ct - timedelta(seconds=LOGIN_LEAD_S)
    wait_s = (login_ct - CTDateTime.now()).total_seconds()
    if wait_s > 0:
        logger.info(
            "RACER: holding %.1fs so the login starts at %s CT, as it always has",
            wait_s,
            login_ct.strftime("%H:%M:%S"),
        )
        await asyncio.sleep(wait_s)
        return
    # A late start still races - it is the only chance this booking has - but
    # it says so, because a recurring late start means the schedule needs moving
    # earlier rather than this morning's ledger being read in the normal frame.
    logger.error(
        "RACER: ready %.1fs after the %s CT login time - this task started late; "
        "move racer_schedule earlier if this recurs",
        -wait_s,
        login_ct.strftime("%H:%M:%S"),
    )


async def race() -> bool:
    """One task's morning. Returns False only when the task itself broke.

    A booking the club refused is not a failed task: its member has been told,
    and a red task for every lost slot would bury the runs that really broke.
    """
    now_ct = CTDateTime.now()
    window_ct = _window_instant(now_ct)
    label = _task_label()

    claimed = await database_service.claim_next_due_group(CTDateTime.to_naive_ct(window_ct))
    if not claimed:
        logger.info(
            "RACER: %s - no unclaimed booking group due at %s CT, nothing to race",
            label,
            window_ct.strftime("%H:%M"),
        )
        return True

    logger.info(
        "RACER: %s claimed %d booking(s) %s for %s at %s",
        label,
        len(claimed),
        [b.id for b in claimed],
        claimed[0].request.requested_date,
        [b.request.requested_time.strftime("%I:%M %p") for b in claimed],
    )

    try:
        install_reservation_provider(booking_service)
        await _hold_until_login(window_ct)
        result = await run_bookings_and_report(
            claimed,
            execute_at=CTDateTime.to_naive_ct(window_ct),
            executed_at=now_ct,
        )
    except Exception as e:
        # run_bookings_and_report reports its own failures, so this is the
        # stretch around it. These rows are already claimed IN_PROGRESS and no
        # other task will touch them, so an exception that escaped here would
        # leave the members waiting on a message that never comes.
        logger.exception("RACER: %s failed around the race", label)
        await booking_service.notify_unreported_bookings(claimed, str(e))
        return False

    logger.info(
        "RACER: %s finished - succeeded=%d, failed=%d",
        label,
        result.succeeded,
        result.failed,
    )
    return True


def main() -> int:
    """Entry point for ``python -m app.racer``."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # After basicConfig, never before: the job runs at LOG_LEVEL=DEBUG, and
    # selenium's wire loggers would otherwise write the login payload to Cloud
    # Logging, as the observer's did on 2026-09-13. See app/log_safety.py.
    silence_wire_loggers()
    try:
        ok = asyncio.run(race())
    except Exception:
        logger.exception("RACER: run failed")
        return 1
    return 0 if ok else 1
