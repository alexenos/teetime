from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Enum, Integer, String, Text, Time
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.models.schemas import BookingStatus, ConversationState


class Base(DeclarativeBase):
    pass


class BookingRecord(Base):
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
