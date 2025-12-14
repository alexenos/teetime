"""
SQLAlchemy database models for persistent storage.

This module defines the database schema for storing booking records and
user session state. These models mirror the Pydantic schemas but are
designed for database persistence.
"""

from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Enum, Integer, String, Text, Time
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.models.schemas import BookingStatus, ConversationState


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
    fallback_window_minutes = Column(Integer, default=30)
    status = Column(Enum(BookingStatus), default=BookingStatus.PENDING)
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
        last_interaction: Timestamp of the user's last message.
    """

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    state = Column(Enum(ConversationState), default=ConversationState.IDLE)
    pending_request_json = Column(Text, nullable=True)
    last_interaction = Column(DateTime, default=datetime.utcnow)


engine = create_async_engine(
    settings.database_url.replace("sqlite://", "sqlite+aiosqlite://")
    if settings.database_url.startswith("sqlite://")
    else settings.database_url,
    echo=False,
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
