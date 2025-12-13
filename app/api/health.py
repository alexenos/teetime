from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    return {"status": "healthy", "service": "teetime"}


@router.get("/")
async def root() -> dict:
    return {
        "service": "TeeTime - Golf Reservation Assistant",
        "version": "0.1.0",
        "endpoints": {
            "health": "/health",
            "webhooks": "/webhooks/twilio/sms",
            "bookings": "/bookings",
        },
    }
