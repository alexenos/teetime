"""
Scheduled job endpoints for Cloud Scheduler integration.

This module provides endpoints that are called by Cloud Scheduler to execute
scheduled booking operations. These endpoints are secured with OIDC token
authentication (preferred) or a legacy API key.
"""

import asyncio
import logging
from datetime import date, datetime, time
from enum import Enum

import pytz
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel

from app.config import settings
from app.services.booking_service import booking_service
from app.services.sms_service import sms_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

BOOKING_EXECUTION_TIMEOUT_SECONDS = 300


class JobExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class JobExecutionItem(BaseModel):
    booking_id: str
    status: JobExecutionStatus
    requested_date: date
    requested_time: time
    error: str | None = None
    confirmation_number: str | None = None


class JobExecutionResult(BaseModel):
    executed_at: datetime
    total_due: int
    succeeded: int
    failed: int
    results: list[JobExecutionItem]


def verify_oidc_token(authorization: str, request: Request) -> bool:
    """
    Verify OIDC token from Cloud Scheduler.

    Returns True if the token is valid and from the expected service account.
    Uses the explicitly configured OIDC_AUDIENCE setting to validate the token's
    audience claim, which must match the audience in Cloud Scheduler's OIDC config.
    """
    if not authorization.startswith("Bearer "):
        return False

    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        if not settings.oidc_audience:
            logger.error("OIDC_AUDIENCE not configured - cannot verify OIDC tokens")
            return False

        audience = settings.oidc_audience.rstrip("/")
        transport_request = google_requests.Request()  # type: ignore[no-untyped-call]
        claims = id_token.verify_oauth2_token(token, transport_request, audience=audience)  # type: ignore[no-untyped-call]

        email = claims.get("email", "")
        if settings.scheduler_service_account and email != settings.scheduler_service_account:
            logger.warning(
                f"OIDC token email mismatch: expected {settings.scheduler_service_account}, got {email}"
            )
            return False

        logger.info(f"OIDC token verified for service account: {email}")
        return True
    except google_auth_exceptions.GoogleAuthError as e:
        logger.warning(f"OIDC token verification failed: {e}")
        return False
    except ValueError as e:
        logger.warning(f"OIDC token validation error: {e}")
        return False


def verify_scheduler_auth(
    request: Request,
    authorization: str | None = Header(None, description="Bearer token for OIDC authentication"),
    x_scheduler_api_key: str | None = Header(
        None, description="Legacy API key for scheduler authentication"
    ),
) -> None:
    """
    Verify scheduler authentication using OIDC token (preferred) or legacy API key.

    Cloud Scheduler sends an OIDC token in the Authorization header.
    For backward compatibility, also accepts X-Scheduler-API-Key header.
    """
    # Track whether any auth was attempted
    auth_attempted = False

    # Try OIDC token first (preferred method)
    if authorization:
        auth_attempted = True
        if verify_oidc_token(authorization, request):
            return

    # Fall back to legacy API key
    if x_scheduler_api_key:
        auth_attempted = True
        # Check if server has API key configured
        if not settings.scheduler_api_key:
            raise HTTPException(
                status_code=500,
                detail="Scheduler API key not configured on server",
            )
        if x_scheduler_api_key == settings.scheduler_api_key:
            return
        raise HTTPException(
            status_code=401,
            detail="Invalid scheduler API key",
        )

    # If auth was attempted but failed (e.g., invalid OIDC token), return 401
    if auth_attempted:
        raise HTTPException(
            status_code=401,
            detail="Authentication failed. Invalid OIDC token or API key.",
        )

    # No authentication provided at all - return 422 (missing required field)
    raise HTTPException(
        status_code=422,
        detail="Authentication required. Provide OIDC token or X-Scheduler-API-Key header.",
    )


@router.post("/execute-due-bookings", response_model=JobExecutionResult)
async def execute_due_bookings(
    _: None = Depends(verify_scheduler_auth),
) -> JobExecutionResult:
    """
    Execute all bookings that are due for execution.

    This endpoint is called by Cloud Scheduler at 6:28am CT daily (2 minutes early).
    It finds all SCHEDULED bookings where scheduled_execution_time <= booking_open_time
    (6:30am CT) and executes them using batch booking for efficiency.

    The early trigger allows the system to log in and navigate to the booking page
    before the booking window opens, then wait until exactly 6:30am to book.

    Optimizations for speed:
    1. Uses batch booking to process multiple bookings with a single login session
    2. Defers SMS notifications until after ALL bookings are complete
    3. Groups bookings by date to minimize navigation overhead
    4. Logs in early and waits until booking window opens

    Security: Accepts OIDC token from Cloud Scheduler (preferred) or legacy API key.

    Idempotency: Bookings are transitioned to IN_PROGRESS before execution,
    so retries will not re-execute already-started bookings.
    """
    tz = pytz.timezone(settings.timezone)
    now = datetime.now(tz)

    # Calculate the booking window open time (6:30am CT)
    # We query for bookings due at this time, not "now", because the scheduler
    # triggers early (6:28am) to allow login before the window opens
    booking_open_time = now.replace(
        hour=settings.booking_open_hour,
        minute=settings.booking_open_minute,
        second=0,
        microsecond=0,
    )

    # Query for bookings due at the booking window open time
    # This finds bookings scheduled for 6:30am even when called at 6:28am
    due_bookings = await booking_service.get_due_bookings(booking_open_time)

    if not due_bookings:
        return JobExecutionResult(
            executed_at=now,
            total_due=0,
            succeeded=0,
            failed=0,
            results=[],
        )

    logger.info(f"BATCH_JOB: Starting batch execution of {len(due_bookings)} bookings")

    booking_map = {b.id: b for b in due_bookings}

    # Strip timezone for passing to batch booking (expects naive datetime in CT)
    booking_open_time_naive = booking_open_time.replace(tzinfo=None)

    logger.info(
        f"BATCH_JOB: Booking window opens at {booking_open_time.strftime('%H:%M:%S')}, "
        f"current time is {now.strftime('%H:%M:%S')}"
    )

    try:
        batch_results = await asyncio.wait_for(
            booking_service.execute_bookings_batch(
                bookings=due_bookings,
                execute_at=booking_open_time_naive,
            ),
            timeout=BOOKING_EXECUTION_TIMEOUT_SECONDS * len(due_bookings),
        )
    except TimeoutError:
        logger.error("BATCH_JOB: Batch execution timed out")
        results: list[JobExecutionItem] = []
        for booking in due_bookings:
            booking_id = booking.id or ""
            results.append(
                JobExecutionItem(
                    booking_id=booking_id,
                    status=JobExecutionStatus.TIMEOUT,
                    requested_date=booking.request.requested_date,
                    requested_time=booking.request.requested_time,
                    error="Batch execution timed out",
                )
            )
        return JobExecutionResult(
            executed_at=now,
            total_due=len(due_bookings),
            succeeded=0,
            failed=len(due_bookings),
            results=results,
        )
    except Exception as e:
        logger.exception(f"BATCH_JOB: Batch execution failed with error: {e}")
        results = []
        for booking in due_bookings:
            booking_id = booking.id or ""
            results.append(
                JobExecutionItem(
                    booking_id=booking_id,
                    status=JobExecutionStatus.ERROR,
                    requested_date=booking.request.requested_date,
                    requested_time=booking.request.requested_time,
                    error=str(e),
                )
            )
        return JobExecutionResult(
            executed_at=now,
            total_due=len(due_bookings),
            succeeded=0,
            failed=len(due_bookings),
            results=results,
        )

    logger.info("BATCH_JOB: Batch execution complete, sending SMS notifications")

    results = []
    succeeded = 0
    failed = 0

    for booking_id, success in batch_results:
        updated_booking = await booking_service.get_booking(booking_id)
        original_booking = booking_map.get(booking_id)

        if not original_booking:
            continue

        if success and updated_booking:
            succeeded += 1
            results.append(
                JobExecutionItem(
                    booking_id=booking_id,
                    status=JobExecutionStatus.SUCCESS,
                    requested_date=original_booking.request.requested_date,
                    requested_time=original_booking.request.requested_time,
                    confirmation_number=updated_booking.confirmation_number,
                )
            )

            date_str = original_booking.request.requested_date.strftime("%A, %B %d")
            time_str = (
                updated_booking.actual_booked_time or original_booking.request.requested_time
            ).strftime("%I:%M %p")
            details = f"{date_str} at {time_str} for {original_booking.request.num_players} players"
            if updated_booking.confirmation_number:
                details += f" (Confirmation: {updated_booking.confirmation_number})"

            await sms_service.send_booking_confirmation(original_booking.phone_number, details)
        else:
            failed += 1
            error_message = updated_booking.error_message if updated_booking else "Unknown error"
            results.append(
                JobExecutionItem(
                    booking_id=booking_id,
                    status=JobExecutionStatus.FAILED,
                    requested_date=original_booking.request.requested_date,
                    requested_time=original_booking.request.requested_time,
                    error=error_message,
                )
            )

            # Build booking details string for the failure message
            date_str = original_booking.request.requested_date.strftime("%A, %B %d")
            time_str = original_booking.request.requested_time.strftime("%I:%M %p")
            booking_details = (
                f"{date_str} at {time_str} for {original_booking.request.num_players} players"
            )

            await sms_service.send_booking_failure(
                original_booking.phone_number,
                error_message or "Unknown error",
                booking_details=booking_details,
            )

    logger.info(
        f"BATCH_JOB: Complete - succeeded={succeeded}, failed={failed}, total={len(due_bookings)}"
    )

    return JobExecutionResult(
        executed_at=now,
        total_due=len(due_bookings),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )
