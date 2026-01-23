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
        last_interaction: Timestamp of the user's last message.
    """

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    state: Column[Any] = Column(Enum(ConversationState), default=ConversationState.IDLE)
    pending_request_json = Column(Text, nullable=True)
    pending_cancellation_id = Column(String(50), nullable=True)
    last_interaction = Column(DateTime, default=datetime.utcnow)


engine = create_async_engine(
    settings.database_url.replace("sqlite://", "sqlite+aiosqlite://")
    if settings.database_url.startswith("sqlite://")
    else settings.database_url,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def _run_column_migrations(conn: Any) -> None:
    """
    Run idempotent schema migrations for columns added after initial deployment.

    This handles the case where tables already exist but are missing new columns.
    Uses database-specific syntax for idempotent column addition.
    """
    is_postgres = settings.database_url.startswith("postgresql")
    is_sqlite = settings.database_url.startswith("sqlite")

    if is_postgres:
        await conn.execute(
            text(
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS "
                "pending_cancellation_id VARCHAR(50)"
            )
        )
        logger.info("Checked/added pending_cancellation_id column to sessions table")
    elif is_sqlite:
        try:
            await conn.execute(
                text("ALTER TABLE sessions ADD COLUMN " "pending_cancellation_id VARCHAR(50)")
            )
            logger.info("Added pending_cancellation_id column to sessions table")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                logger.debug("pending_cancellation_id column already exists")
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
