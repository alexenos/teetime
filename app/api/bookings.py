from datetime import date, time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.schemas import BookingStatus, TeeTimeRequest
from app.services.booking_service import booking_service

router = APIRouter(prefix="/bookings", tags=["bookings"])


class CreateBookingRequest(BaseModel):
    phone_number: str
    requested_date: date
    requested_time: time
    num_players: int = 4
    fallback_window_minutes: int = 30


class BookingResponse(BaseModel):
    id: str | None
    phone_number: str
    requested_date: date
    requested_time: time
    num_players: int
    status: BookingStatus
    confirmation_number: str | None = None
    error_message: str | None = None


@router.post("/", response_model=BookingResponse)
async def create_booking(request: CreateBookingRequest) -> BookingResponse:
    tee_time_request = TeeTimeRequest(
        requested_date=request.requested_date,
        requested_time=request.requested_time,
        num_players=request.num_players,
        fallback_window_minutes=request.fallback_window_minutes,
    )

    booking = await booking_service.create_booking(request.phone_number, tee_time_request)

    return BookingResponse(
        id=booking.id,
        phone_number=booking.phone_number,
        requested_date=booking.request.requested_date,
        requested_time=booking.request.requested_time,
        num_players=booking.request.num_players,
        status=booking.status,
        confirmation_number=booking.confirmation_number,
        error_message=booking.error_message,
    )


@router.get("/", response_model=list[BookingResponse])
async def list_bookings(
    phone_number: str | None = None, status: BookingStatus | None = None
) -> list[BookingResponse]:
    bookings = booking_service.get_bookings(phone_number=phone_number, status=status)

    return [
        BookingResponse(
            id=b.id,
            phone_number=b.phone_number,
            requested_date=b.request.requested_date,
            requested_time=b.request.requested_time,
            num_players=b.request.num_players,
            status=b.status,
            confirmation_number=b.confirmation_number,
            error_message=b.error_message,
        )
        for b in bookings
    ]


@router.get("/{booking_id}", response_model=BookingResponse)
async def get_booking(booking_id: str) -> BookingResponse:
    booking = booking_service.get_booking(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    return BookingResponse(
        id=booking.id,
        phone_number=booking.phone_number,
        requested_date=booking.request.requested_date,
        requested_time=booking.request.requested_time,
        num_players=booking.request.num_players,
        status=booking.status,
        confirmation_number=booking.confirmation_number,
        error_message=booking.error_message,
    )


@router.delete("/{booking_id}")
async def cancel_booking(booking_id: str) -> dict:
    booking = booking_service.cancel_booking(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found or cannot be cancelled")

    return {"status": "cancelled", "booking_id": booking_id}


@router.post("/{booking_id}/execute")
async def execute_booking(booking_id: str) -> dict:
    booking = booking_service.get_booking(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    success = await booking_service.execute_booking(booking_id)

    return {
        "success": success,
        "booking_id": booking_id,
        "status": booking.status.value,
        "confirmation_number": booking.confirmation_number,
        "error_message": booking.error_message,
    }
