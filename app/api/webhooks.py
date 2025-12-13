from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.services.booking_service import booking_service
from app.services.sms_service import sms_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/twilio/sms", response_class=PlainTextResponse)
async def handle_incoming_sms(
    request: Request,
    from_number: str = Form(..., alias="From"),
    to_number: str = Form(..., alias="To"),
    body: str = Form(..., alias="Body"),
    x_twilio_signature: str = Header(None, alias="X-Twilio-Signature"),
) -> str:
    url = str(request.url)
    form_data = await request.form()
    params = {key: value for key, value in form_data.items()}

    if x_twilio_signature:
        if not sms_service.validate_request(url, params, x_twilio_signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    response_message = await booking_service.handle_incoming_message(from_number, body)

    await sms_service.send_sms(from_number, response_message)

    return ""


@router.post("/twilio/status")
async def handle_sms_status(
    message_sid: str = Form(..., alias="MessageSid"),
    message_status: str = Form(..., alias="MessageStatus"),
    to_number: str = Form(None, alias="To"),
    error_code: str = Form(None, alias="ErrorCode"),
) -> dict:
    print(f"SMS Status Update - SID: {message_sid}, Status: {message_status}")
    if error_code:
        print(f"Error Code: {error_code}")

    return {"status": "received"}
