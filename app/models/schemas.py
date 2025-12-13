from datetime import date, datetime, time
from enum import Enum

from pydantic import BaseModel, Field


class BookingStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TeeTimeRequest(BaseModel):
    requested_date: date = Field(..., description="The date for the tee time")
    requested_time: time = Field(..., description="The preferred tee time")
    num_players: int = Field(default=4, ge=1, le=4, description="Number of players (1-4)")
    fallback_window_minutes: int = Field(
        default=30, description="Minutes before/after requested time to accept as fallback"
    )


class TeeTimeBooking(BaseModel):
    id: str | None = None
    phone_number: str
    request: TeeTimeRequest
    status: BookingStatus = BookingStatus.PENDING
    scheduled_execution_time: datetime | None = None
    actual_booked_time: time | None = None
    confirmation_number: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ConversationState(str, Enum):
    IDLE = "idle"
    AWAITING_DATE = "awaiting_date"
    AWAITING_TIME = "awaiting_time"
    AWAITING_PLAYERS = "awaiting_players"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


class UserSession(BaseModel):
    phone_number: str
    state: ConversationState = ConversationState.IDLE
    pending_request: TeeTimeRequest | None = None
    last_interaction: datetime = Field(default_factory=datetime.utcnow)


class ParsedIntent(BaseModel):
    intent: str = Field(..., description="The user's intent: book, modify, cancel, status, help")
    tee_time_request: TeeTimeRequest | None = None
    booking_id: str | None = None
    clarification_needed: str | None = None
    response_message: str | None = None


class SMSMessage(BaseModel):
    from_number: str
    to_number: str
    body: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
