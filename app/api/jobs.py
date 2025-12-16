"""
Scheduled job endpoints for Cloud Scheduler integration.

This module provides endpoints that are called by Cloud Scheduler to execute
scheduled booking operations. These endpoints are secured with an API key.
"""

from datetime import datetime

import pytz
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services.booking_service import booking_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobExecutionResult(BaseModel):
    executed_at: datetime
    total_due: int
    executed: int
    succeeded: int
    failed: int
    results: list[dict]


def verify_scheduler_api_key(x_scheduler_api_key: str = Header(...)) -> None:
    """Verify the API key provided by Cloud Scheduler."""
    if not settings.scheduler_api_key:
        raise HTTPException(
            status_code=500,
            detail="Scheduler API key not configured on server",
        )
    if x_scheduler_api_key != settings.scheduler_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid scheduler API key",
        )


@router.post("/execute-due-bookings", response_model=JobExecutionResult)
async def execute_due_bookings(
    x_scheduler_api_key: str = Header(..., description="API key for scheduler authentication"),
) -> JobExecutionResult:
    """
    Execute all bookings that are due for execution.

    This endpoint is called by Cloud Scheduler at 6:30am CT daily.
    It finds all SCHEDULED bookings where scheduled_execution_time <= now
    and executes them sequentially.

    Security: Requires X-Scheduler-API-Key header matching the configured key.
    """
    verify_scheduler_api_key(x_scheduler_api_key)

    tz = pytz.timezone(settings.timezone)
    now = datetime.now(tz)

    due_bookings = await booking_service.get_due_bookings(now)

    results = []
    succeeded = 0
    failed = 0

    for booking in due_bookings:
        try:
            success = await booking_service.execute_booking(booking.id)
            if success:
                succeeded += 1
                results.append(
                    {
                        "booking_id": booking.id,
                        "status": "success",
                        "requested_date": str(booking.request.requested_date),
                        "requested_time": str(booking.request.requested_time),
                    }
                )
            else:
                failed += 1
                updated_booking = await booking_service.get_booking(booking.id)
                results.append(
                    {
                        "booking_id": booking.id,
                        "status": "failed",
                        "error": updated_booking.error_message
                        if updated_booking
                        else "Unknown error",
                        "requested_date": str(booking.request.requested_date),
                        "requested_time": str(booking.request.requested_time),
                    }
                )
        except Exception as e:
            failed += 1
            results.append(
                {
                    "booking_id": booking.id,
                    "status": "error",
                    "error": str(e),
                    "requested_date": str(booking.request.requested_date),
                    "requested_time": str(booking.request.requested_time),
                }
            )

    return JobExecutionResult(
        executed_at=now,
        total_due=len(due_bookings),
        executed=len(due_bookings),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )
