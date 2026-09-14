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
import time
from datetime import datetime, timedelta

from app.api.jobs import (
    BOOKING_EXECUTION_TIMEOUT_SECONDS,
    JobExecutionResult,
    run_bookings_and_report,
)
from app.config import settings
from app.log_safety import silence_wire_loggers
from app.models.schemas import TeeTimeBooking
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

# How long one task may spend, measured from its own start: the hold, its own
# race, and any late races of leftover groups. Every race's timeout is capped by
# what is left of this, so however many bookings a group holds, a timed-out race
# still messages its members before Cloud Run's task timeout (1500s,
# terraform/racer.tf) kills the container. That in turn stays shorter than
# INTERRUPTED_MIN_AGE, so no claim looks orphaned while its task can still run.
TASK_BUDGET_S = 1200

# What a member is told when their group was left over and no task had the time
# to race it, even late.
NOT_RACED_MESSAGE = (
    "The booking was not attempted: more bookings were due this morning than "
    "could be raced at once."
)

# The task's clock for its budget. A module attribute so tests can move it
# without patching time.monotonic underneath the event loop.
_clock = time.monotonic


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


def _describe(group: list[TeeTimeBooking]) -> str:
    return (
        f"{len(group)} booking(s) {[b.id for b in group]} for "
        f"{group[0].request.requested_date} at "
        f"{[b.request.requested_time.strftime('%I:%M %p') for b in group]}"
    )


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


async def _race_group(
    group: list[TeeTimeBooking],
    window_ct: datetime,
    now_ct: datetime,
    remaining_s: float,
) -> JobExecutionResult:
    return await run_bookings_and_report(
        group,
        execute_at=CTDateTime.to_naive_ct(window_ct),
        executed_at=now_ct,
        timeout_s=min(BOOKING_EXECUTION_TIMEOUT_SECONDS * len(group), remaining_s),
    )


async def _race_leftovers(
    window_ct: datetime, now_ct: datetime, label: str, started: float
) -> bool:
    """Race, late, any group no task claimed at the start.

    Every task claims its own group within seconds of starting, so a group
    still unclaimed once this task's race is over means more groups were due
    than racer_max_requesters. Leaving it SCHEDULED would strand it silently
    until the next morning's run. Racing it late is the old sequential
    behaviour for exactly those groups - and a late Reserve can still be granted
    (09-13's +74s one was). A group there is no longer time to race is claimed
    anyway and its members told it was not attempted.
    """
    ok = True
    while leftover := await database_service.claim_next_due_group(
        CTDateTime.to_naive_ct(window_ct)
    ):
        remaining_s = TASK_BUDGET_S - (_clock() - started)
        if remaining_s < BOOKING_EXECUTION_TIMEOUT_SECONDS:
            logger.error(
                "RACER: %s left %s unraced - only %.0fs of its budget remain; "
                "raise racer_max_requesters",
                label,
                _describe(leftover),
                remaining_s,
            )
            await booking_service.notify_unreported_bookings(leftover, NOT_RACED_MESSAGE)
            continue

        logger.error(
            "RACER: %s found %s still unclaimed after its own race - more groups were "
            "due than racer tasks; racing it late. Raise racer_max_requesters",
            label,
            _describe(leftover),
        )
        try:
            await _race_group(leftover, window_ct, now_ct, remaining_s)
        except Exception as e:
            logger.exception("RACER: %s failed around a late race", label)
            await booking_service.notify_unreported_bookings(leftover, str(e))
            ok = False
    return ok


async def race() -> bool:
    """One task's morning. Returns False only when the task itself broke.

    A booking the club refused is not a failed task: its member has been told,
    and a red task for every lost slot would bury the runs that really broke.
    """
    started = _clock()
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

    logger.info("RACER: %s claimed %s", label, _describe(claimed))

    try:
        install_reservation_provider(booking_service)
        await _hold_until_login(window_ct)
        result = await _race_group(claimed, window_ct, now_ct, TASK_BUDGET_S - (_clock() - started))
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
    return await _race_leftovers(window_ct, now_ct, label, started)


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
