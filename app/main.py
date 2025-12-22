import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import bookings, health, jobs, webhooks
from app.config import settings
from app.models.database import init_db
from app.providers.walden_provider import MockWaldenProvider, WaldenGolfProvider
from app.services.booking_service import booking_service

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()

    if not settings.scheduler_api_key:
        logger.warning(
            "SCHEDULER_API_KEY is not configured. "
            "The /jobs/execute-due-bookings endpoint will return 500 errors. "
            "Set SCHEDULER_API_KEY environment variable for production use."
        )

    if settings.walden_member_number and settings.walden_password:
        logger.info("Walden Golf credentials configured - using real WaldenGolfProvider")
        provider = WaldenGolfProvider()
    else:
        logger.warning(
            "Walden Golf credentials not configured - using MockWaldenProvider. "
            "Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD for real bookings."
        )
        provider = MockWaldenProvider()
    booking_service.set_reservation_provider(provider)

    yield

    await provider.close()


app = FastAPI(
    title="TeeTime",
    description="Golf tee time reservation assistant with SMS interface",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware configuration
# Note: Wide-open CORS with allow_credentials=True is a security anti-pattern.
# For production with a web frontend, configure specific allowed origins via
# environment variables. Currently disabled since the primary interface is
# Twilio webhooks (server-to-server) which don't require CORS.
# TODO: Add CORS_ALLOWED_ORIGINS setting when a web frontend is added.
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=settings.cors_allowed_origins,  # Configure in settings
#     allow_credentials=False,  # Only enable if needed with specific origins
#     allow_methods=["GET", "POST", "DELETE"],
#     allow_headers=["*"],
# )

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(bookings.router)
app.include_router(jobs.router)
