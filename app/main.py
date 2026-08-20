import asyncio
import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from fastapi import FastAPI

from app.api import bookings, health, jobs, webhooks
from app.config import settings
from app.models.database import init_db
from app.providers.base import ReservationProvider
from app.providers.walden_provider import MockWaldenProvider, WaldenGolfProvider
from app.services.booking_service import booking_service

if TYPE_CHECKING:
    from app.services.discord_gateway import DiscordGateway


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

    # Silence the WebDriver wire loggers unconditionally - NOT gated on LOG_LEVEL.
    #
    # selenium.webdriver.remote.remote_connection logs every command payload at
    # DEBUG, which includes the send_keys body used to fill the login form. With
    # LOG_LEVEL=DEBUG (how this runs in production, to get BOOKING_DEBUG output)
    # that wrote the Walden member number and password to Cloud Logging in
    # cleartext on every booking run. These loggers must never be allowed to
    # emit below WARNING regardless of how verbose the app itself is.
    for wire_logger in ("selenium", "urllib3", "websockets", "httpcore"):
        logging.getLogger(wire_logger).setLevel(logging.WARNING)


configure_logging()
logger = logging.getLogger(__name__)

# How long startup reconciliation waits for the Discord gateway before giving up
# on delivering its notifications and resolving the stuck rows regardless.
GATEWAY_READY_TIMEOUT_SECONDS = 60

# How long shutdown waits for in-flight booking attempts to report their result.
# Cloud Run allows roughly 10s between SIGTERM and SIGKILL, so this only needs
# to cover an attempt that is seconds from finishing; a hung Selenium run must
# not hold up gateway and provider cleanup.
SHUTDOWN_BOOKING_TIMEOUT_SECONDS = 8


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()

    discord_gateway = None
    if settings.messaging_channel == "discord":
        if settings.discord_bot_token:
            from app.services.discord_gateway import DiscordGateway

            logger.info("Discord messaging channel enabled - starting gateway listener")
            discord_gateway = DiscordGateway(booking_service.handle_incoming_message)
            discord_gateway.start()
        else:
            logger.warning(
                "MESSAGING_CHANNEL=discord but DISCORD_BOT_TOKEN is not set. "
                "Inbound Discord messages will not work."
            )

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

    # Any booking still IN_PROGRESS is left over from a process that died
    # mid-attempt, since no attempt survives a restart. Resolve those and tell
    # the user, rather than leaving the row stuck and the request unanswered.
    # This runs as a background task so a slow or unreachable messaging channel
    # cannot block startup, and it waits for the gateway first so the
    # notification has somewhere to go.
    reconcile_task = asyncio.create_task(
        _reconcile_after_startup(discord_gateway), name="reconcile-interrupted-bookings"
    )

    yield

    # Cancel and collect the reconcile task before tearing the gateway down, so
    # it can't be left pending against a closed client.
    reconcile_task.cancel()
    with suppress(asyncio.CancelledError):
        await reconcile_task
    # Let any booking attempt still in flight report its outcome before the
    # provider closes underneath it - but bounded, because a hung Selenium run
    # would otherwise block gateway and provider cleanup until the platform
    # kills the container.
    await booking_service.wait_for_background_bookings(timeout=SHUTDOWN_BOOKING_TIMEOUT_SECONDS)
    if discord_gateway is not None:
        await discord_gateway.stop()
    await provider.close()


async def _reconcile_after_startup(discord_gateway: "DiscordGateway | None") -> None:
    """Reconcile interrupted bookings once the messaging channel can deliver."""
    try:
        if discord_gateway is not None:
            try:
                await asyncio.wait_for(
                    discord_gateway.client.wait_until_ready(),
                    timeout=GATEWAY_READY_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # A rejected token or an unreachable gateway would otherwise
                # leave this waiting forever, and the stuck rows would never be
                # resolved. Resolving them matters more than delivering the
                # notification, so carry on and record why.
                logger.warning(
                    "Discord gateway not ready after %ss; reconciling anyway - "
                    "interrupted-booking notifications may not be delivered",
                    GATEWAY_READY_TIMEOUT_SECONDS,
                )
        await booking_service.reconcile_interrupted_bookings()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to reconcile interrupted bookings at startup")


app = FastAPI(
    title="TeeTime",
    description="Golf tee time reservation assistant with SMS interface",
    version="0.2.0",
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
