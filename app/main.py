import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import bookings, health, jobs, webhooks
from app.config import settings
from app.models.database import init_db
from app.providers.base import ReservationProvider
from app.providers.walden_provider import MockWaldenProvider, WaldenGolfProvider
from app.services.booking_service import booking_service


def configure_logging() -> None:
    """
    Configure logging for the application.

    For GCP Cloud Run, logs to stdout are automatically captured by Cloud Logging.
    Set LOG_LEVEL=DEBUG environment variable to see BOOKING_DEBUG messages.
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Configure root logger to capture all app logs
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,  # Override any existing configuration
    )

    # Ensure our app loggers use the configured level
    logging.getLogger("app").setLevel(log_level)

    # Reduce noise from third-party libraries unless in debug mode
    if log_level > logging.DEBUG:
        logging.getLogger("selenium").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


configure_logging()
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
        provider: ReservationProvider = WaldenGolfProvider()
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
