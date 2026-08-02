"""
Discord implementation of the messaging provider interface.

Outbound messages are sent via the REST API (bot token auth), into the channel
the conversation started in when one was recorded, and as a DM otherwise.
Inbound messages arrive over the gateway WebSocket (see
app/services/discord_gateway.py), not HTTP webhooks, so there is no request
signature to validate.

Throughout the app the user identifier field is called ``phone_number``; for
Discord it carries the user's snowflake ID as a string (e.g. "1234567890123456789"),
which fits the existing 20-char column without a schema change.
"""

import logging

import httpx

from app.config import settings
from app.providers.sms_base import SMSProvider, SMSResult

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_MESSAGE_LEN = 2000  # Discord's hard limit per message

# Recorded as a conversation's origin when it started in a DM rather than a
# guild channel. A DM channel has a real snowflake, but storing it would make
# replies indistinguishable from a shared channel and add a spurious @-mention;
# the sentinel just says "reply the way we always did".
DM_ORIGIN = "dm"


def split_message(message: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split a message into chunks under Discord's length limit, preferring newlines."""
    if len(message) <= limit:
        return [message]
    chunks = []
    remaining = message
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class DiscordProvider(SMSProvider):
    """Sends messages to a Discord user, in a shared channel or via bot DM.

    The ``to_number`` argument is a Discord user ID (snowflake string). If it
    is not numeric (e.g. a legacy phone number), the configured
    ``discord_user_id`` is used instead so notification paths written for SMS
    still reach the user.

    ``origin_channel_id`` selects the destination - see ``resolve_channel_id``.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._dm_channels: dict[str, str] = {}
        self._transport = transport

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {settings.discord_bot_token}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=DISCORD_API_BASE,
            headers=self._headers,
            timeout=15.0,
            transport=self._transport,
        )

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        """Discord input comes via the gateway, not webhooks; nothing to validate."""
        return True

    @staticmethod
    def resolve_user_id(to_number: str) -> str:
        """Map the recipient identifier to a Discord user ID."""
        candidate = to_number.strip()
        if candidate.isdigit():
            return candidate
        if settings.discord_user_id:
            return settings.discord_user_id
        return candidate

    async def _get_dm_channel(self, client: httpx.AsyncClient, user_id: str) -> str:
        if user_id in self._dm_channels:
            return self._dm_channels[user_id]
        resp = await client.post("/users/@me/channels", json={"recipient_id": user_id})
        resp.raise_for_status()
        channel_id = str(resp.json()["id"])
        self._dm_channels[user_id] = channel_id
        return channel_id

    @staticmethod
    def resolve_channel_id(origin_channel_id: str | None) -> str | None:
        """Pick the channel to post into, or None to fall back to a DM.

        Async notifications (the 6:30am booking result) are sent days after the
        request, so they carry the channel the conversation started in. Replying
        there keeps the thread where the user asked instead of splitting it into a
        private DM.

        Precedence:
        1. A guild channel ID from the originating conversation.
        2. DM_ORIGIN, meaning the conversation started in a DM - reply in the DM
           even when a shared channel is configured, since a DM is where the user
           chose to talk.
        3. No origin at all (REST API bookings, SMS, the weekly prompt, and
           bookings created before origins were recorded): DISCORD_CHANNEL_ID if
           set, else a DM.

        Whatever comes back is interpolated into ``/channels/{id}/messages``, so
        both sources are checked here rather than trusted. Settings already
        rejects a non-numeric DISCORD_CHANNEL_ID at load time; re-checking keeps
        that invariant local to the place that depends on it.
        """
        candidate = (origin_channel_id or "").strip()
        if candidate.isdigit():
            return candidate
        if candidate == DM_ORIGIN:
            return None
        if candidate:
            logger.warning(f"Ignoring unrecognized Discord origin channel {candidate!r}")

        configured = settings.discord_channel_id.strip()
        if configured.isdigit():
            return configured
        if configured:
            logger.warning(f"Ignoring non-numeric DISCORD_CHANNEL_ID {configured!r}")
        return None

    async def send_sms(
        self, to_number: str, message: str, origin_channel_id: str | None = None
    ) -> SMSResult:
        user_id = self.resolve_user_id(to_number)
        if not user_id:
            return SMSResult(success=False, error_message="No Discord user ID configured")

        target_channel_id = self.resolve_channel_id(origin_channel_id)
        destination = f"channel {target_channel_id}" if target_channel_id else f"DM to {user_id}"
        try:
            async with self._client() as client:
                if target_channel_id:
                    # Shared channel: @-mention so the notification still pings.
                    channel_id = target_channel_id
                    message = f"<@{user_id}> {message}" if user_id.isdigit() else message
                else:
                    channel_id = await self._get_dm_channel(client, user_id)
                last_message_id: str | None = None
                for chunk in split_message(message):
                    resp = await client.post(
                        f"/channels/{channel_id}/messages", json={"content": chunk}
                    )
                    resp.raise_for_status()
                    last_message_id = str(resp.json()["id"])
            return SMSResult(success=True, message_sid=last_message_id)
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:200]
            logger.error(f"Discord API error sending to {destination}: {exc} {body}")
            return SMSResult(success=False, error_message=f"{exc.response.status_code}: {body}")
        except httpx.HTTPError as exc:
            logger.error(f"Discord request failed sending to {destination}: {exc}")
            return SMSResult(success=False, error_message=str(exc))
