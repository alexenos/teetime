from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(bookings.router)
