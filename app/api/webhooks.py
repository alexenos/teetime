import logging

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.providers.telegram_provider import is_authorized_user, verify_webhook_secret
from app.providers.twilio_provider import TwilioSMSProvider
from app.services.booking_service import booking_service
from app.services.sms_service import sms_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def get_external_url(request: Request) -> str:
    """
    Reconstruct the external URL from forwarded headers.

    Cloud Run and other proxies pass the original URL via X-Forwarded-* headers.
    Twilio signs requests using the external URL, so we must reconstruct it
    for signature validation to work correctly.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host", request.url.netloc
    )
    path = request.url.path
    query = request.url.query

    url = f"{proto}://{host}{path}"
    if query:
        url = f"{url}?{query}"
    return url


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
    url = get_external_url(request)
    form_data = await request.form()
    params = {key: str(value) for key, value in form_data.items()}

    if not sms_service.validate_request(url, params, x_twilio_signature):
        raise HTTPException(status_code=403, detail="Invalid or missing Twilio signature")

    normalized_from = TwilioSMSProvider.normalize_phone_number(from_number)

    response_message = await booking_service.handle_incoming_message(
        normalized_from, body, channel="twilio"
    )

    await sms_service.send_sms(normalized_from, response_message, channel="twilio")

    return ""


@router.post("/telegram")
async def handle_telegram_update(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, str]:
    """
    Handle an inbound Telegram update.

    This is the Telegram equivalent of the Discord gateway's on_message, and the
    reason Telegram needs no always-on instance: Telegram POSTs here instead of
    holding a socket open, so the service can scale to zero between messages.

    Security: Telegram does not sign payloads. The only proof an update came
    from Telegram is the secret registered with setWebhook and echoed back in
    X-Telegram-Bot-Api-Secret-Token, so a bad or missing one is rejected before
    the body is read. Beyond that, only the users in TELEGRAM_ALLOWED_USER_IDS
    are answered.

    Always returns 200 for an update that is merely uninteresting - a message
    from someone else, an empty body, an update type we did not ask for.
    Telegram retries any non-2xx response, and retrying will not make an
    ignored message interesting.
    """
    if not verify_webhook_secret(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Invalid or missing Telegram webhook secret")

    update = await request.json()
    message = update.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()

    user_id = str(sender.get("id", ""))
    chat_id = str(chat.get("id", ""))

    if not user_id or not chat_id:
        logger.info("Telegram update carried no message to handle; ignoring")
        return {"status": "ignored"}

    if not is_authorized_user(user_id, bool(sender.get("is_bot"))):
        logger.info(f"Ignoring Telegram message from unauthorized user {user_id}")
        return {"status": "ignored"}

    if not text:
        logger.info(f"Telegram message from {user_id} had no text; ignoring")
        return {"status": "ignored"}

    logger.info(f"Telegram message received from {user_id} in chat {chat_id}: {text[:80]}")

    try:
        response_message = await booking_service.handle_incoming_message(
            user_id, text, origin_channel_id=chat_id, channel="telegram"
        )
    except Exception:
        logger.exception("Error handling Telegram message")
        response_message = "Sorry, something went wrong processing that message."

    await sms_service.send_sms(
        user_id, response_message, origin_channel_id=chat_id, channel="telegram"
    )

    return {"status": "ok"}


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
