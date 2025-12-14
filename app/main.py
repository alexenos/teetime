from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import bookings, health, webhooks
from app.models.database import init_db
from app.providers.walden_provider import MockWaldenProvider
from app.services.booking_service import booking_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()

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
