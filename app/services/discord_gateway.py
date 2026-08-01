"""
Discord gateway listener: the inbound half of the Discord messaging channel.

Discord bots receive messages over a persistent WebSocket (the "gateway"),
unlike Twilio's HTTP webhooks. This module runs a discord.py client as a
background task inside the FastAPI process and routes DMs from the configured
user into the same booking_service.handle_incoming_message() entry point the
Twilio webhook uses.

Requires the "Message Content Intent" to be enabled for the bot in the
Discord Developer Portal, and DISCORD_BOT_TOKEN / DISCORD_USER_ID settings.

Note: because this needs an always-on process, the Cloud Run service must run
with min-instances=1 when the Discord channel is enabled.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from app.config import settings
from app.providers.discord_provider import split_message

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, str], Awaitable[str]]


def should_handle_message(
    author_id: str,
    author_is_bot: bool,
    is_dm: bool,
    allowed_user_id: str,
) -> bool:
    """Decide whether an incoming Discord message should be processed.

    Only direct messages from the single configured user are handled; everything
    else (other users, guild channels, other bots, our own messages) is ignored.
    An empty allowed_user_id means no one is authorized - fail closed.
    """
    if author_is_bot or not is_dm:
        return False
    if not allowed_user_id:
        logger.warning("DISCORD_USER_ID not configured; ignoring DM from %s", author_id)
        return False
    return author_id == allowed_user_id


class DiscordGateway:
    """Runs the discord.py client and dispatches DMs to a message handler."""

    def __init__(self, message_handler: MessageHandler) -> None:
        self._message_handler = message_handler
        self._task: asyncio.Task[None] | None = None

        intents = discord.Intents.none()
        intents.dm_messages = True
        intents.message_content = True
        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready() -> None:  # pragma: no cover - needs live gateway
            user = self.client.user
            logger.info(f"Discord gateway connected as {user} (id={getattr(user, 'id', '?')})")

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

    async def _on_message(self, message: discord.Message) -> None:
        author_id = str(message.author.id)
        is_dm = message.guild is None
        if not should_handle_message(
            author_id, message.author.bot, is_dm, settings.discord_user_id
        ):
            return
        content = message.content.strip()
        if not content:
            return
        logger.info(f"Discord DM received from {author_id}: {content[:80]}")
        try:
            response = await self._message_handler(author_id, content)
        except Exception:
            logger.exception("Error handling Discord message")
            response = "Sorry, something went wrong processing that message."
        for chunk in split_message(response):
            await message.channel.send(chunk)

    def start(self) -> None:
        """Start the gateway client as a background task on the running loop."""
        if not settings.discord_bot_token:
            raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
        self._task = asyncio.create_task(
            self.client.start(settings.discord_bot_token), name="discord-gateway"
        )

    async def stop(self) -> None:
        if not self.client.is_closed():
            await self.client.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
