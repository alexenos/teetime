"""
Pydantic schemas for the TeeTime application.

This module defines the data models used throughout the application for
tee time booking requests, booking status tracking, conversation state
management, and SMS message handling.
"""

from datetime import date, datetime, time
from enum import Enum

from pydantic import BaseModel, Field


class BookingStatus(str, Enum):
    """
    Represents the lifecycle status of a tee time booking request.

    Status flow:
        PENDING -> SCHEDULED -> IN_PROGRESS -> SUCCESS/FAILED
                             |-> CANCELLED

    Statuses:
        PENDING: Initial state when a booking request is created but not yet
            scheduled for execution. This is a transient state before the
            system calculates when to attempt the booking.
        SCHEDULED: The booking job is scheduled to execute at the reservation
            open time (6:30am CT, 7 days before the requested date). The system
            is waiting for the booking window to open.
        IN_PROGRESS: The booking attempt is currently being executed. The system
            is actively trying to reserve the tee time on the club website.
        SUCCESS: The tee time was successfully reserved on the club website.
            The actual_booked_time and confirmation_number fields will be populated.
        FAILED: The booking attempt failed (e.g., time unavailable, site error).
            The error_message field will contain details about the failure.
        CANCELLED: The user cancelled the booking request before it was executed.
    """

    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TeeTimeRequest(BaseModel):
    """
    Represents a user's request to book a tee time.

    This model captures the user's preferences for their desired tee time,
    including the date, time, number of players, and fallback preferences.
    """

    requested_date: date = Field(..., description="The date for the tee time")
    requested_time: time = Field(..., description="The preferred tee time")
    num_players: int = Field(default=4, ge=1, le=4, description="Number of players (1-4)")
    fallback_window_minutes: int = Field(
        default=30,
        description=(
            "If the exact requested time is unavailable, the system will attempt "
            "to book a time within this many minutes before or after the requested "
            "time. For example, if set to 30 and the user requests 8:00am, the system "
            "will try times between 7:30am and 8:30am if 8:00am is taken."
        ),
    )


class TeeTimeBooking(BaseModel):
    """
    Represents a complete tee time booking record.

    This model tracks the full lifecycle of a booking request, from initial
    creation through execution and final result. It includes the original
    request details, scheduling information, and outcome data.

    Attributes:
        id: Unique identifier for this booking (8-character UUID prefix).
        phone_number: The user's phone number for SMS notifications.
        request: The original TeeTimeRequest with user preferences.
        status: Current status in the booking lifecycle (see BookingStatus).
        scheduled_execution_time: When the system will attempt to book (6:30am CT,
            7 days before the requested date).
        actual_booked_time: The time that was actually booked (may differ from
            requested_time if fallback was used). Only populated on SUCCESS.
        confirmation_number: Confirmation number from the club website. Only
            populated on SUCCESS.
        error_message: Description of why the booking failed. Only populated
            on FAILED status.
        created_at: When this booking record was created.
        updated_at: When this booking record was last modified.
    """

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
    """
    Tracks the current state of an SMS conversation with a user.

    The conversation follows a state machine pattern to collect booking
    information step by step when the user doesn't provide all details
    in a single message.

    States:
        IDLE: No active conversation. Ready for new requests.
        AWAITING_DATE: Asked the user for a date, waiting for response.
        AWAITING_TIME: Asked the user for a time, waiting for response.
        AWAITING_PLAYERS: Asked the user for number of players, waiting for response.
        AWAITING_CONFIRMATION: All details collected, waiting for user to confirm.
        AWAITING_CANCELLATION_SELECTION: User has multiple bookings and we asked
            which one to cancel. Waiting for them to specify which booking.
    """

    IDLE = "idle"
    AWAITING_DATE = "awaiting_date"
    AWAITING_TIME = "awaiting_time"
    AWAITING_PLAYERS = "awaiting_players"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_CANCELLATION_SELECTION = "awaiting_cancellation_selection"


class UserSession(BaseModel):
    """
    Maintains conversation state for a user identified by phone number.

    Each user has a session that tracks their current conversation state
    and any pending booking request being built up through the conversation.

    Attributes:
        phone_number: The user's phone number (unique identifier).
        state: Current conversation state (see ConversationState).
        pending_request: Partially or fully built TeeTimeRequest being
            constructed through the conversation. None when IDLE.
            Deprecated: Use pending_requests for new code.
        pending_requests: List of TeeTimeRequests being constructed through
            the conversation. Supports multiple bookings in a single message.
            None when IDLE.
        pending_cancellation_id: ID of a booking awaiting cancellation confirmation.
            Set when user requests to cancel and we're waiting for them to confirm.
        last_interaction: Timestamp of the user's last message. Used for
            session timeout logic.
    """

    phone_number: str
    state: ConversationState = ConversationState.IDLE
    pending_request: TeeTimeRequest | None = None
    pending_requests: list[TeeTimeRequest] | None = None
    pending_cancellation_id: str | None = None
    last_interaction: datetime = Field(default_factory=datetime.utcnow)


class ParsedIntent(BaseModel):
    """
    Result of parsing a user's SMS message using the Gemini LLM.

    The Gemini service analyzes the user's natural language message and
    extracts structured information about their intent and any booking
    details they provided.

    Attributes:
        intent: The detected user intent. One of:
            - "book": User wants to book a new tee time
            - "modify": User wants to change an existing booking
            - "cancel": User wants to cancel a booking
            - "status": User wants to check their booking status
            - "help": User is asking for help or information
            - "confirm": User is confirming a pending action
            - "unclear": Could not determine user's intent
        raw_message: The original user message text. Used for confirmation
            flows where we need to check the exact response.
        tee_time_request: Extracted booking details if intent is "book".
            May be partial if user didn't provide all information.
            Deprecated: Use tee_time_requests for new code.
        tee_time_requests: List of extracted booking details if intent is "book".
            Supports multiple bookings in a single message.
        booking_id: ID of the booking to modify/cancel (if applicable).
        clarification_needed: Question to ask the user if more information
            is needed to complete their request.
        response_message: Suggested response message to send to the user.
    """

    intent: str = Field(..., description="The user's intent: book, modify, cancel, status, help")
    raw_message: str | None = None
    tee_time_request: TeeTimeRequest | None = None
    tee_time_requests: list[TeeTimeRequest] | None = None
    booking_id: str | None = None
    clarification_needed: str | None = None
    response_message: str | None = None


class SMSMessage(BaseModel):
    """
    Represents an SMS message sent or received via Twilio.

    Used for logging and processing SMS communications with users.

    Attributes:
        from_number: The sender's phone number (E.164 format).
        to_number: The recipient's phone number (E.164 format).
        body: The text content of the message.
        timestamp: When the message was sent/received.
    """

    from_number: str
    to_number: str
    body: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
