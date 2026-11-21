from fastapi import APIRouter

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "healthy", "service": "teetime"}


@router.get("/")
async def root() -> dict[str, str | dict[str, str]]:
    return {
        "service": "TeeTime - Golf Reservation Assistant",
        "version": __version__,
        "endpoints": {
            "health": "/health",
            "webhooks": "/webhooks/twilio/sms",
        },
    }
