import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.models.schemas import ConversationState
from app.providers.telegram_provider import (
    TelegramProvider,
    addressee_prefix,
    is_addressed_to_bot,
    is_authorized_user,
    strip_bot_prefix,
    verify_webhook_secret,
)
from app.providers.twilio_provider import TwilioSMSProvider
from app.services.booking_service import booking_service
from app.services.help_text import addressing_help_message
from app.services.sms_service import sms_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# How long an unfinished conversation still counts as "live" for the group
# addressing bypass below. Nothing in the conversation state machine ever
# resets a session back to IDLE on its own - only finishing the flow does
# (booked, cancelled, or an error path) - so without this, someone who was
# once asked "reply yes to confirm" and never answered would have every one
# of their unaddressed group messages routed to the parser, indefinitely.
_GROUP_CONVERSATION_TIMEOUT = timedelta(minutes=15)


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

    # Named explicitly rather than left to MESSAGING_CHANNEL: this route is only
    # ever called by Twilio, and only Twilio's provider checks its signature.
    if not sms_service.validate_request(url, params, x_twilio_signature, channel="twilio"):
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

    try:
        update = await request.json()
    except ValueError:
        # Only a caller holding the secret can get this far, so this is a bug or
        # a malformed retry rather than an attack. Answer 200 either way: a
        # non-2xx would have Telegram redeliver the same unparseable body.
        logger.warning("Telegram update body was not valid JSON; ignoring")
        return {"status": "ignored"}

    if not isinstance(update, dict):
        logger.warning("Telegram update was not an object; ignoring")
        return {"status": "ignored"}

    message = update.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    # Deliberately NOT stripped: entity offsets below are relative to the text
    # exactly as Telegram sent it, so trimming leading whitespace here would
    # shift every offset and cut the wrong range. strip_bot_prefix trims.
    raw_text = message.get("text") or ""

    user_id = str(sender.get("id", ""))
    chat_id = str(chat.get("id", ""))

    if not user_id or not chat_id:
        logger.info("Telegram update carried no message to handle; ignoring")
        return {"status": "ignored"}

    if not is_authorized_user(user_id, bool(sender.get("is_bot"))):
        logger.info(f"Ignoring Telegram message from unauthorized user {user_id}")
        return {"status": "ignored"}

    if not raw_text.strip():
        logger.info(f"Telegram message from {user_id} had no text; ignoring")
        return {"status": "ignored"}

    entities = message.get("entities")
    bot_username = await TelegramProvider().get_bot_username()

    # With group privacy mode on, Telegram only ever delivers a group message
    # that already addresses this bot. Some groups need privacy mode off to
    # get delivery working at all (see docs/telegram-setup.md), which means
    # Telegram now hands over every message regardless of addressing - so an
    # unaddressed one in a non-private chat has to be dropped here instead,
    # or every group message becomes an LLM call.
    #
    # Exception: a user already mid-conversation (we just asked them a
    # question) can keep replying without re-addressing the bot every turn -
    # the same way a human keeps talking after being spoken to, rather than
    # re-tagging the other person in every reply. Scoped to this user's own
    # session, so someone else's unaddressed chatter in the same group still
    # needs its own mention to start a conversation.
    if chat.get("type") != "private" and not is_addressed_to_bot(
        raw_text, entities, bot_username, message.get("reply_to_message")
    ):
        session = await booking_service.get_session(user_id)
        session_age = datetime.now(UTC).replace(tzinfo=None) - session.last_interaction
        live = session.state != ConversationState.IDLE and session_age < _GROUP_CONVERSATION_TIMEOUT
        if not live:
            logger.info(
                f"Telegram message from {user_id} in chat {chat_id} was not addressed; ignoring"
            )
            return {"status": "ignored"}

    # In a group the message has to address the bot to reach us at all, so it
    # arrives as "@teetimebot book 9/5 at 9a" or "/book@teetimebot 9/5 at 9a".
    # Strip that addressing before the parser sees it, the same way the Discord
    # gateway strips "<@1533...>".
    text = strip_bot_prefix(raw_text, entities, bot_username)

    if not text:
        # Addressing and nothing else: "@NorthgateTeetimebot" on its own, with
        # the actual request typed as a second, untagged message. That second
        # message never gets here (privacy mode keeps Telegram from delivering
        # it, and the check above drops it when privacy mode is off), so
        # staying silent reads as the bot ignoring a booking request. Answer
        # with what to do instead.
        logger.info(f"Telegram message from {user_id} was only addressing; replying with help")
        mention = f"@{bot_username} " if bot_username and chat.get("type") != "private" else ""
        response_message = addressing_help_message(mention)
    else:
        logger.info(f"Telegram message received from {user_id} in chat {chat_id}: {text[:80]}")

        # Only a group conversation needs a mention baked into the booking - a
        # private chat has no one else to name it for, and would just read the
        # user their own handle back. "" (rather than omitting the argument)
        # explicitly clears any stale mention left over from an earlier group
        # conversation with this same user.
        requester_handle = addressee_prefix(sender) if chat.get("type") != "private" else ""

        try:
            response_message = await booking_service.handle_incoming_message(
                user_id,
                text,
                origin_channel_id=chat_id,
                channel="telegram",
                requester_handle=requester_handle,
            )
        except Exception:
            logger.exception("Error handling Telegram message")
            response_message = "Sorry, something went wrong processing that message."

    reply_to_message_id: str | None = None
    if chat.get("type") != "private":
        # Several people can be mid-conversation with the bot in this same
        # group at once - name who this reply is for, and thread it to their
        # message so it doesn't read as a bare answer to whoever spoke last.
        response_message = addressee_prefix(sender) + response_message
        message_id = message.get("message_id")
        if message_id is not None:
            reply_to_message_id = str(message_id)

    await sms_service.send_sms(
        user_id,
        response_message,
        origin_channel_id=chat_id,
        channel="telegram",
        reply_to_message_id=reply_to_message_id,
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
