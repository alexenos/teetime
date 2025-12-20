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
from google.oauth2 import id_token
from pydantic import BaseModel

from app.config import settings
from app.services.booking_service import booking_service

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
    """
    if not authorization.startswith("Bearer "):
        return False

    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        # Verify the token and get claims
        # The audience should be the Cloud Run service URL
        claims = id_token.verify_oauth2_token(token, None)  # type: ignore[no-untyped-call]

        # Verify the email matches the expected scheduler service account
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
    # Try OIDC token first (preferred method)
    if authorization:
        if verify_oidc_token(authorization, request):
            return

    # Fall back to legacy API key
    if x_scheduler_api_key:
        if settings.scheduler_api_key and x_scheduler_api_key == settings.scheduler_api_key:
            return
        raise HTTPException(
            status_code=401,
            detail="Invalid scheduler API key",
        )

    # No valid authentication provided
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide OIDC token or X-Scheduler-API-Key header.",
    )


@router.post("/execute-due-bookings", response_model=JobExecutionResult)
async def execute_due_bookings(
    _: None = Depends(verify_scheduler_auth),
) -> JobExecutionResult:
    """
    Execute all bookings that are due for execution.

    This endpoint is called by Cloud Scheduler at 6:30am CT daily.
    It finds all SCHEDULED bookings where scheduled_execution_time <= now
    and executes them sequentially with per-booking timeouts.

    Security: Accepts OIDC token from Cloud Scheduler (preferred) or legacy API key.

    Idempotency: Bookings are transitioned to IN_PROGRESS before execution,
    so retries will not re-execute already-started bookings.
    """
    tz = pytz.timezone(settings.timezone)
    now = datetime.now(tz)

    due_bookings = await booking_service.get_due_bookings(now)

    results: list[JobExecutionItem] = []
    succeeded = 0
    failed = 0

    for booking in due_bookings:
        try:
            success = await asyncio.wait_for(
                booking_service.execute_booking(booking.id),
                timeout=BOOKING_EXECUTION_TIMEOUT_SECONDS,
            )
            if success:
                succeeded += 1
                updated_booking = await booking_service.get_booking(booking.id)
                results.append(
                    JobExecutionItem(
                        booking_id=booking.id,
                        status=JobExecutionStatus.SUCCESS,
                        requested_date=booking.request.requested_date,
                        requested_time=booking.request.requested_time,
                        confirmation_number=updated_booking.confirmation_number
                        if updated_booking
                        else None,
                    )
                )
            else:
                failed += 1
                updated_booking = await booking_service.get_booking(booking.id)
                results.append(
                    JobExecutionItem(
                        booking_id=booking.id,
                        status=JobExecutionStatus.FAILED,
                        requested_date=booking.request.requested_date,
                        requested_time=booking.request.requested_time,
                        error=updated_booking.error_message if updated_booking else "Unknown error",
                    )
                )
        except TimeoutError:
            failed += 1
            logger.error(
                f"Booking {booking.id} timed out after {BOOKING_EXECUTION_TIMEOUT_SECONDS}s"
            )
            results.append(
                JobExecutionItem(
                    booking_id=booking.id,
                    status=JobExecutionStatus.TIMEOUT,
                    requested_date=booking.request.requested_date,
                    requested_time=booking.request.requested_time,
                    error=f"Execution timed out after {BOOKING_EXECUTION_TIMEOUT_SECONDS} seconds",
                )
            )
        except Exception as e:
            failed += 1
            logger.exception(f"Booking {booking.id} failed with error: {e}")
            results.append(
                JobExecutionItem(
                    booking_id=booking.id,
                    status=JobExecutionStatus.ERROR,
                    requested_date=booking.request.requested_date,
                    requested_time=booking.request.requested_time,
                    error=str(e),
                )
            )

    return JobExecutionResult(
        executed_at=now,
        total_due=len(due_bookings),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )
