"""
SQLAlchemy database models for persistent storage.

This module defines the database schema for storing booking records and
user session state. These models mirror the Pydantic schemas but are
designed for database persistence.
"""

import logging
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from sqlalchemy import Column, Date, DateTime, Enum, Integer, String, Text, Time, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.models.schemas import BookingStatus, ConversationState

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


class BookingRecord(Base):
    """
    Database model for storing tee time booking records.

    This table persists all booking requests and their outcomes, allowing
    the system to track booking history and recover state after restarts.

    Columns:
        id: Auto-incrementing primary key.
        booking_id: Application-level unique identifier (8-char UUID prefix).
        phone_number: User's phone number for SMS notifications.
        requested_date: The date the user wants to play golf.
        requested_time: The user's preferred tee time.
        num_players: Number of players in the group (1-4).
        fallback_window_minutes: If the exact requested time is unavailable,
            the system will try to book a time within this many minutes
            before or after. For example, if set to 30 and the user requests
            8:00am, the system will try times between 7:30am and 8:30am.
        status: Current booking status (see BookingStatus enum).
        scheduled_execution_time: When the booking job will run (6:30am CT,
            7 days before the requested date).
        actual_booked_time: The time that was actually reserved (may differ
            from requested_time if fallback was used).
        confirmation_number: Confirmation number from the club website.
        error_message: Details about why a booking failed.
        origin_channel_id: Discord channel ID the booking was requested in, so
            the result notification replies in that conversation rather than a
            DM. NULL for SMS and REST API bookings.
        created_at: When this record was created.
        updated_at: When this record was last modified.
    """

    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(50), unique=True, nullable=False, index=True)
    phone_number = Column(String(20), nullable=False, index=True)
    requested_date = Column(Date, nullable=False)
    requested_time = Column(Time, nullable=False)
    num_players = Column(Integer, default=4)
    fallback_window_minutes = Column(Integer, default=32)
    status: Column[Any] = Column(Enum(BookingStatus), default=BookingStatus.PENDING)
    scheduled_execution_time = Column(DateTime, nullable=True)
    actual_booked_time = Column(Time, nullable=True)
    confirmation_number = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    origin_channel_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SessionRecord(Base):
    """
    Database model for storing user conversation sessions.

    This table persists the conversation state for each user, allowing
    the system to maintain context across messages and recover state
    after restarts.

    Columns:
        id: Auto-incrementing primary key.
        phone_number: User's phone number (unique identifier).
        state: Current conversation state (see ConversationState enum).
        pending_request_json: JSON-serialized TeeTimeRequest being built
            through the conversation. NULL when state is IDLE.
        pending_cancellation_id: Booking ID awaiting cancellation confirmation.
            Set when user requests to cancel and we're waiting for confirmation.
        origin_channel_id: Discord channel ID of the user's current conversation,
            refreshed on each inbound message. NULL for SMS users.
        last_interaction: Timestamp of the user's last message.
    """

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    state: Column[Any] = Column(Enum(ConversationState), default=ConversationState.IDLE)
    pending_request_json = Column(Text, nullable=True)
    pending_cancellation_id = Column(String(50), nullable=True)
    origin_channel_id = Column(String(32), nullable=True)
    last_interaction = Column(DateTime, default=datetime.utcnow)


_database_url = (
    settings.database_url.replace("sqlite://", "sqlite+aiosqlite://")
    if settings.database_url.startswith("sqlite://")
    else settings.database_url
)


# This service is idle almost all day: the 06:28 booking job is often its only
# caller between one morning and the next, and Cloud Run gives an idle instance
# no CPU between requests. A pooled Postgres connection does not reliably
# survive a gap that long - the Cloud SQL socket is gone by the time the next
# request wakes the container - so the pool hands the job a dead connection and
# its first query raises "connection is closed" before anything can retry. That
# is the 2026-08-19 failure; see docs/booking-post-mortem-2026-08-20.md.
#
#   pre_ping  - round-trips a cheap SELECT 1 and transparently reconnects on a
#               dead connection. This is the setting that closes the failure.
#   recycle   - retires connections after 30 minutes so long-lived ones are
#               replaced on a normal request rather than discovered dead on the
#               one request of the day that is racing a clock. Follows Google's
#               documented Cloud SQL guidance; no specific server-side idle
#               timeout is known to apply on the unix-socket path this uses.
#
# SQLite gets neither: it is a local file or an in-memory database with no
# connection to go stale, and :memory: is served by a StaticPool where recycling
# would discard the schema along with the connection.
def _pool_options_for(url: str) -> dict[str, Any]:
    """Return the connection-pool keyword arguments to use for a database URL."""
    if url.startswith("sqlite"):
        return {}
    return {"pool_pre_ping": True, "pool_recycle": 1800}


engine = create_async_engine(
    _database_url,
    echo=False,
    **_pool_options_for(_database_url),
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# Columns added after the initial deployment, as (table, column, SQL type).
# create_all() only creates missing tables, so existing installs need these
# backfilled explicitly. Append to this list when adding a nullable column.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("sessions", "pending_cancellation_id", "VARCHAR(50)"),
    ("sessions", "origin_channel_id", "VARCHAR(32)"),
    ("bookings", "origin_channel_id", "VARCHAR(32)"),
]


async def _run_column_migrations(conn: Any) -> None:
    """
    Run idempotent schema migrations for columns added after initial deployment.

    This handles the case where tables already exist but are missing new columns.
    Uses database-specific syntax for idempotent column addition.
    """
    is_postgres = settings.database_url.startswith("postgresql")
    is_sqlite = settings.database_url.startswith("sqlite")

    for table, column, sql_type in _ADDED_COLUMNS:
        if is_postgres:
            await conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
            )
            logger.info(f"Checked/added {column} column to {table} table")
        elif is_sqlite:
            # SQLite has no ADD COLUMN IF NOT EXISTS; a duplicate is the no-op case.
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                logger.info(f"Added {column} column to {table} table")
            except Exception as e:
                if "duplicate column" in str(e).lower():
                    logger.debug(f"{column} column already exists on {table}")
                else:
                    raise


async def _run_enum_migrations() -> None:
    """
    Run idempotent enum type migrations for PostgreSQL.

    ALTER TYPE ... ADD VALUE cannot run inside a transaction block in PostgreSQL,
    so this must be run with autocommit mode using a separate connection.
    SQLite stores enums as strings, so no migration is needed there.
    """
    if not settings.database_url.startswith("postgresql"):
        return

    from sqlalchemy import create_engine

    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    sync_engine = create_engine(sync_url, isolation_level="AUTOCOMMIT")

    try:
        with sync_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT 1 FROM pg_enum "
                    "WHERE enumlabel = 'AWAITING_CANCELLATION_SELECTION' "
                    "AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'conversationstate')"
                )
            )
            if result.fetchone() is None:
                conn.execute(
                    text("ALTER TYPE conversationstate ADD VALUE 'AWAITING_CANCELLATION_SELECTION'")
                )
                logger.info("Added AWAITING_CANCELLATION_SELECTION to conversationstate enum")
            else:
                logger.debug(
                    "AWAITING_CANCELLATION_SELECTION already exists in conversationstate enum"
                )
    finally:
        sync_engine.dispose()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_column_migrations(conn)
    await _run_enum_migrations()
