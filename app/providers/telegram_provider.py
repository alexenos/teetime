"""
Telegram implementation of the messaging provider interface.

Both halves of this channel are plain HTTPS. Outbound messages are POSTed to
the Bot API; inbound updates arrive as webhook POSTs from Telegram (see
``app/api/webhooks.py``), not over a persistent socket. That is the whole point
of this provider: unlike the Discord gateway it holds no connection, so the
Cloud Run service can scale to zero between messages.

Throughout the app the user identifier field is called ``phone_number``; for
Telegram it carries the user's numeric ID as a string, which fits the existing
20-char column without a schema change. ``origin_channel_id`` carries the chat
the conversation is happening in - the same value as the user ID in a private
chat, and a negative group ID in a group - so a booking result days later
replies where it was asked for.
"""

import hmac
import logging

import httpx

from app.config import settings
from app.providers.sms_base import SMSProvider, SMSResult, split_message

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096  # Telegram's hard limit per message
WEBHOOK_PATH = "/webhooks/telegram"


def is_authorized_user(user_id: str, is_bot: bool) -> bool:
    """Decide whether an incoming Telegram update should be processed.

    Mirrors the Discord allowlist: only the configured user IDs are handled,
    wherever the bot can see them, and an empty allowlist authorizes no one.
    The webhook is a public URL whose only other protection is the shared
    secret, so this must fail closed.
    """
    if is_bot:
        return False
    allowed = settings.telegram_allowed_ids()
    if not allowed:
        logger.warning("TELEGRAM_ALLOWED_USER_IDS not configured; ignoring update from %s", user_id)
        return False
    return user_id in allowed


def verify_webhook_secret(header_value: str | None) -> bool:
    """Check the secret Telegram echoes in X-Telegram-Bot-Api-Secret-Token.

    Telegram does not sign webhook payloads; this shared secret is the only
    thing standing between the public endpoint and a forged booking, so an
    unconfigured secret rejects everything rather than accepting everything.
    Compared with compare_digest to keep the check constant-time.
    """
    configured = settings.telegram_webhook_secret
    if not configured:
        logger.error(
            "TELEGRAM_WEBHOOK_SECRET is not configured; rejecting the update. "
            "Inbound Telegram is disabled until it is set."
        )
        return False
    if not header_value:
        return False
    return hmac.compare_digest(header_value, configured)


class TelegramProvider(SMSProvider):
    """Sends messages to a Telegram user or group chat via the Bot API.

    ``to_number`` is a Telegram user ID. ``origin_channel_id`` is the chat to
    post into and takes precedence, so a request made in a group is answered in
    that group; with no origin recorded the message goes to the user directly.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}",
            timeout=15.0,
            transport=self._transport,
        )

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        """Telegram webhooks carry no signature; the shared secret is checked instead.

        See verify_webhook_secret, which the webhook route calls against the
        X-Telegram-Bot-Api-Secret-Token header. This interface method takes form
        params and a signature, neither of which Telegram sends, so it has
        nothing to validate here.
        """
        return True

    @staticmethod
    def resolve_chat_id(to_number: str, origin_channel_id: str | None) -> str:
        """Pick the chat to post into.

        The conversation's own chat wins when one was recorded, so a booking
        requested in a group is answered in that group rather than splitting
        into a private message. Falls back to the user's ID, which in Telegram
        is also their private chat ID.

        Group chat IDs are negative, so this cannot use isdigit() the way the
        Discord provider does; it checks for an integer instead. Whatever comes
        back is sent as the chat_id parameter, so both sources are checked here
        rather than trusted.
        """
        candidate = (origin_channel_id or "").strip()
        if candidate:
            try:
                int(candidate)
            except ValueError:
                logger.warning(f"Ignoring unrecognized Telegram origin chat {candidate!r}")
            else:
                return candidate
        return to_number.strip()

    async def send_sms(
        self, to_number: str, message: str, origin_channel_id: str | None = None
    ) -> SMSResult:
        if not settings.telegram_bot_token:
            return SMSResult(success=False, error_message="TELEGRAM_BOT_TOKEN is not configured")

        chat_id = self.resolve_chat_id(to_number, origin_channel_id)
        if not chat_id:
            return SMSResult(success=False, error_message="No Telegram chat to send to")

        try:
            async with self._client() as client:
                last_message_id: str | None = None
                for chunk in split_message(message, MAX_MESSAGE_LEN):
                    resp = await client.post(
                        "/sendMessage", json={"chat_id": chat_id, "text": chunk}
                    )
                    resp.raise_for_status()
                    last_message_id = str(resp.json()["result"]["message_id"])
            return SMSResult(success=True, message_sid=last_message_id)
        except httpx.HTTPStatusError as exc:
            # The token is in the URL, so exc.request.url must never be logged.
            body = exc.response.text[:200]
            logger.error(
                f"Telegram API error sending to chat {chat_id}: {exc.response.status_code} {body}"
            )
            return SMSResult(success=False, error_message=f"{exc.response.status_code}: {body}")
        except httpx.HTTPError as exc:
            logger.error(f"Telegram request failed sending to chat {chat_id}: {type(exc).__name__}")
            return SMSResult(success=False, error_message=str(exc))

    async def register_webhook(self) -> bool:
        """Point Telegram at this service's webhook URL.

        Idempotent, and re-run on every startup so a changed service URL heals
        itself rather than leaving the bot silently pointed at a dead endpoint.
        Only "message" updates are requested; the bot has no use for the edits,
        reactions and membership changes Telegram would otherwise deliver, and
        each one would be an unnecessary wake-up for a scale-to-zero service.

        Returns True when the webhook is registered, False when it is not -
        including when it is deliberately not attempted, since a missing base
        URL or secret means inbound Telegram is simply off.
        """
        if not settings.telegram_bot_token:
            return False
        if not settings.telegram_webhook_base_url:
            logger.info(
                "TELEGRAM_WEBHOOK_BASE_URL is not set; skipping webhook registration. "
                "Outbound Telegram still works; inbound will not."
            )
            return False
        if not settings.telegram_webhook_secret:
            logger.warning(
                "TELEGRAM_WEBHOOK_SECRET is not set; skipping webhook registration. "
                "An unauthenticated webhook would accept forged bookings."
            )
            return False

        url = f"{settings.telegram_webhook_base_url.rstrip('/')}{WEBHOOK_PATH}"
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/setWebhook",
                    json={
                        "url": url,
                        "secret_token": settings.telegram_webhook_secret,
                        "allowed_updates": ["message"],
                    },
                )
                resp.raise_for_status()
            logger.info(f"Telegram webhook registered at {url}")
            return True
        except httpx.HTTPError as exc:
            # Never log the exception's URL or response body unfiltered: the bot
            # token is a path segment and setWebhook echoes the secret back.
            logger.error(f"Failed to register Telegram webhook at {url}: {type(exc).__name__}")
            return False
