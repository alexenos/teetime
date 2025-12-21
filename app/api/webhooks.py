from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.providers.twilio_provider import TwilioSMSProvider
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
    """
    Handle incoming SMS/WhatsApp messages from Twilio.

    Security: When Twilio credentials are configured (twilio_auth_token is set),
    the X-Twilio-Signature header is required and validated. In dev mode (no
    credentials), validation is skipped to allow local testing.

    Note: For WhatsApp messages, the From/To numbers arrive with 'whatsapp:' prefix.
    We normalize these to plain E.164 format for consistent session/DB handling.
    """
    url = str(request.url)
    form_data = await request.form()
    params = {key: str(value) for key, value in form_data.items()}

    if not sms_service.validate_request(url, params, x_twilio_signature):
        raise HTTPException(status_code=403, detail="Invalid or missing Twilio signature")

    normalized_from = TwilioSMSProvider.normalize_phone_number(from_number)

    response_message = await booking_service.handle_incoming_message(normalized_from, body)

    await sms_service.send_sms(normalized_from, response_message)

    return ""


@router.post("/twilio/status")
async def handle_sms_status(
    message_sid: str = Form(..., alias="MessageSid"),
    message_status: str = Form(..., alias="MessageStatus"),
    to_number: str = Form(None, alias="To"),
    error_code: str = Form(None, alias="ErrorCode"),
) -> dict[str, str]:
    print(f"SMS Status Update - SID: {message_sid}, Status: {message_status}")
    if error_code:
        print(f"Error Code: {error_code}")

    return {"status": "received"}
