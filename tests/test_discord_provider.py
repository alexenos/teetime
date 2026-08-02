"""Tests for the Discord messaging provider and gateway message handling."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.providers.discord_provider import (
    DM_ORIGIN,
    MAX_MESSAGE_LEN,
    DiscordProvider,
    split_message,
)
from app.services.discord_gateway import DiscordGateway, should_handle_message


class TestSplitMessage:
    def test_short_message_single_chunk(self) -> None:
        assert split_message("hello") == ["hello"]

    def test_exactly_limit_single_chunk(self) -> None:
        msg = "x" * MAX_MESSAGE_LEN
        assert split_message(msg) == [msg]

    def test_long_message_split_at_newline(self) -> None:
        first = "a" * 1500
        second = "b" * 1000
        chunks = split_message(f"{first}\n{second}")
        assert chunks == [first, second]

    def test_long_message_without_newlines_hard_split(self) -> None:
        msg = "x" * (MAX_MESSAGE_LEN * 2 + 10)
        chunks = split_message(msg)
        assert all(len(c) <= MAX_MESSAGE_LEN for c in chunks)
        assert "".join(chunks) == msg


class TestResolveUserId:
    def test_numeric_id_passthrough(self) -> None:
        assert DiscordProvider.resolve_user_id("123456789012345678") == "123456789012345678"

    def test_phone_number_falls_back_to_configured_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "discord_user_id", "987654321")
        assert DiscordProvider.resolve_user_id("+15551234567") == "987654321"

    def test_non_numeric_without_config_returned_as_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "discord_user_id", "")
        assert DiscordProvider.resolve_user_id("+15551234567") == "+15551234567"


def make_provider(handler) -> DiscordProvider:  # type: ignore[no-untyped-def]
    return DiscordProvider(transport=httpx.MockTransport(handler))


class TestSendSms:
    async def test_sends_dm_via_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "discord_bot_token", "test-token")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/users/@me/channels"):
                return httpx.Response(200, json={"id": "555"})
            if request.url.path.endswith("/channels/555/messages"):
                return httpx.Response(200, json={"id": "msg-1"})
            return httpx.Response(404)

        provider = make_provider(handler)
        result = await provider.send_sms("123456789012345678", "tee time confirmed")

        assert result.success
        assert result.message_sid == "msg-1"
        assert json.loads(requests[0].content) == {"recipient_id": "123456789012345678"}
        assert json.loads(requests[1].content) == {"content": "tee time confirmed"}
        assert requests[0].headers["Authorization"] == "Bot test-token"

    async def test_dm_channel_cached_between_sends(self) -> None:
        channel_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal channel_calls
            if request.url.path.endswith("/users/@me/channels"):
                channel_calls += 1
                return httpx.Response(200, json={"id": "555"})
            return httpx.Response(200, json={"id": "msg"})

        provider = make_provider(handler)
        await provider.send_sms("42", "one")
        await provider.send_sms("42", "two")
        assert channel_calls == 1

    async def test_long_message_sent_in_chunks(self) -> None:
        contents: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/@me/channels"):
                return httpx.Response(200, json={"id": "555"})
            contents.append(json.loads(request.content)["content"])
            return httpx.Response(200, json={"id": f"msg-{len(contents)}"})

        provider = make_provider(handler)
        result = await provider.send_sms("42", "y" * (MAX_MESSAGE_LEN + 5))
        assert result.success
        assert len(contents) == 2

    async def test_api_error_returns_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Missing Access"})

        provider = make_provider(handler)
        result = await provider.send_sms("42", "hello")
        assert not result.success
        assert "403" in (result.error_message or "")

    async def test_no_user_id_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "discord_user_id", "")
        provider = make_provider(lambda request: httpx.Response(500))
        result = await provider.send_sms("", "hello")
        assert not result.success

    async def test_posts_to_shared_channel_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With DISCORD_CHANNEL_ID set, send to that channel (mentioning the user),
        not a private DM."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "msg-1"})

        provider = make_provider(handler)
        result = await provider.send_sms("123456789012345678", "tee time confirmed")

        assert result.success
        # No DM channel was opened; the message went straight to the channel.
        assert all(not r.url.path.endswith("/users/@me/channels") for r in requests)
        assert requests[0].url.path.endswith("/channels/9001/messages")
        assert json.loads(requests[0].content) == {
            "content": "<@123456789012345678> tee time confirmed"
        }

    async def test_shared_channel_unset_falls_back_to_dm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no DISCORD_CHANNEL_ID, delivery opens and uses a private DM."""
        monkeypatch.setattr(settings, "discord_channel_id", "")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/users/@me/channels"):
                return httpx.Response(200, json={"id": "555"})
            return httpx.Response(200, json={"id": "msg-1"})

        provider = make_provider(handler)
        result = await provider.send_sms("123456789012345678", "hi")

        assert result.success
        # A DM channel was opened and used.
        assert requests[0].url.path.endswith("/users/@me/channels")
        assert requests[1].url.path.endswith("/channels/555/messages")
        assert json.loads(requests[1].content) == {"content": "hi"}

    async def test_origin_channel_wins_over_configured_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A notification replies in the channel its conversation started in,
        even when a different shared channel is configured."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "msg-1"})

        provider = make_provider(handler)
        result = await provider.send_sms("123456789012345678", "booked", origin_channel_id="4242")

        assert result.success
        assert requests[0].url.path.endswith("/channels/4242/messages")
        assert json.loads(requests[0].content) == {"content": "<@123456789012345678> booked"}

    async def test_dm_origin_stays_a_dm_despite_configured_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A conversation started in a DM is answered in the DM, without a
        mention, rather than being aired in the configured shared channel."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/users/@me/channels"):
                return httpx.Response(200, json={"id": "555"})
            return httpx.Response(200, json={"id": "msg-1"})

        provider = make_provider(handler)
        result = await provider.send_sms(
            "123456789012345678", "booked", origin_channel_id=DM_ORIGIN
        )

        assert result.success
        assert requests[0].url.path.endswith("/users/@me/channels")
        assert requests[1].url.path.endswith("/channels/555/messages")
        assert json.loads(requests[1].content) == {"content": "booked"}

    async def test_unrecognized_origin_falls_back_to_configured_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed stored origin must not be interpolated into the API path;
        it falls back to the configured channel."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "msg-1"})

        provider = make_provider(handler)
        result = await provider.send_sms("123456789012345678", "hi", origin_channel_id="#general")

        assert result.success
        assert requests[0].url.path.endswith("/channels/9001/messages")

    def test_validate_request_always_true(self) -> None:
        provider = DiscordProvider()
        assert provider.validate_request("http://x", {}, None)


class TestResolveChannelId:
    """Destination precedence: origin channel, then DM sentinel, then config."""

    def test_numeric_origin_used_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The conversation's own channel outranks the configured one."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        assert DiscordProvider.resolve_channel_id("4242") == "4242"

    def test_origin_is_trimmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stray whitespace around a stored ID does not break the API path."""
        monkeypatch.setattr(settings, "discord_channel_id", "")
        assert DiscordProvider.resolve_channel_id("  4242  ") == "4242"

    def test_dm_sentinel_means_dm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A DM conversation stays a DM even with a shared channel configured."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        assert DiscordProvider.resolve_channel_id(DM_ORIGIN) is None

    def test_no_origin_uses_configured_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Notifications with no conversation behind them use the fallback."""
        monkeypatch.setattr(settings, "discord_channel_id", "9001")
        assert DiscordProvider.resolve_channel_id(None) == "9001"

    def test_no_origin_and_no_config_means_dm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With nothing configured and no origin, delivery falls back to a DM."""
        monkeypatch.setattr(settings, "discord_channel_id", "")
        assert DiscordProvider.resolve_channel_id(None) is None

    def test_non_numeric_configured_channel_falls_back_to_dm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings rejects a bad DISCORD_CHANNEL_ID at load, but the value is
        interpolated into an API path, so a value that reached settings another
        way (a direct assignment, bypassing validation) must not be used."""
        monkeypatch.setattr(settings, "discord_channel_id", "../../users/@me")
        assert DiscordProvider.resolve_channel_id(None) is None
        assert DiscordProvider.resolve_channel_id(DM_ORIGIN) is None


class TestDiscordChannelIdValidation:
    """DISCORD_CHANNEL_ID must be a numeric snowflake, or empty (DM fallback)."""

    def test_empty_is_allowed(self) -> None:
        """An unset channel ID is valid and means 'use DMs'."""
        assert Settings(discord_channel_id="").discord_channel_id == ""

    def test_valid_snowflake_is_accepted_and_trimmed(self) -> None:
        """A numeric ID (with surrounding whitespace) is accepted and trimmed."""
        assert Settings(discord_channel_id="  123456789012345678  ").discord_channel_id == (
            "123456789012345678"
        )

    def test_channel_name_is_rejected(self) -> None:
        """A channel name like '#general' is rejected at load time."""
        with pytest.raises(ValidationError):
            Settings(discord_channel_id="#general")


class TestShouldHandleMessage:
    ALLOWED = "111222333444555666"

    def test_message_from_allowed_user(self) -> None:
        assert should_handle_message(self.ALLOWED, False, self.ALLOWED)

    def test_message_from_other_user_ignored(self) -> None:
        assert not should_handle_message("999", False, self.ALLOWED)

    def test_bot_message_ignored(self) -> None:
        assert not should_handle_message(self.ALLOWED, True, self.ALLOWED)

    def test_no_allowlist_fails_closed(self) -> None:
        assert not should_handle_message(self.ALLOWED, False, "")


BOT_ID = 999000111
ALLOWED_ID = "111222333444555666"
GUILD_CHANNEL_ID = "778899001122334455"


def make_message(
    *,
    author_id: str = ALLOWED_ID,
    content: str = "book saturday 8am",
    is_bot: bool = False,
    in_guild: bool = False,
    channel_id: str = GUILD_CHANNEL_ID,
) -> MagicMock:
    """Build a stand-in for discord.Message with a spy on channel.send."""
    message = MagicMock()
    message.author.id = int(author_id)
    message.author.bot = is_bot
    message.content = content
    message.guild = MagicMock() if in_guild else None
    message.channel.id = channel_id
    message.channel.name = "tee-times"
    message.channel.send = AsyncMock()
    return message


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> tuple[DiscordGateway, AsyncMock]:
    """A gateway whose discord.Client is replaced by a stub bot identity."""
    monkeypatch.setattr(settings, "discord_user_id", ALLOWED_ID)
    handler = AsyncMock(return_value="ok")
    gw = DiscordGateway(handler)
    gw.client = MagicMock()  # type: ignore[assignment]
    gw.client.user.id = BOT_ID
    return gw, handler


class TestGatewayOnMessage:
    async def test_dm_from_allowed_user_dispatched(self, gateway) -> None:  # type: ignore[no-untyped-def]
        gw, handler = gateway
        message = make_message()

        await gw._on_message(message)

        handler.assert_awaited_once_with(ALLOWED_ID, "book saturday 8am", DM_ORIGIN)
        message.channel.send.assert_awaited_once_with("ok")

    async def test_guild_message_from_allowed_user_dispatched(self, gateway) -> None:  # type: ignore[no-untyped-def]
        """Server-channel messages must work, not just DMs, and the channel they
        arrived in is passed along so later notifications can reply there."""
        gw, handler = gateway
        message = make_message(in_guild=True)

        await gw._on_message(message)

        handler.assert_awaited_once_with(ALLOWED_ID, "book saturday 8am", GUILD_CHANNEL_ID)
        message.channel.send.assert_awaited_once_with("ok")

    async def test_own_message_suppressed(
        self,
        gateway,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bot must not react to its own outbound messages.

        The allowlist is deliberately pointed at the bot's own ID so this
        exercises the self-check rather than passing for the incidental
        reason that the bot isn't the allowed user.
        """
        gw, handler = gateway
        monkeypatch.setattr(settings, "discord_user_id", str(BOT_ID))
        message = make_message(author_id=str(BOT_ID), is_bot=False)

        await gw._on_message(message)

        handler.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_other_user_ignored(self, gateway) -> None:  # type: ignore[no-untyped-def]
        gw, handler = gateway
        message = make_message(author_id="424242424242")

        await gw._on_message(message)

        handler.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_empty_content_ignored(self, gateway) -> None:  # type: ignore[no-untyped-def]
        """Empty content signals a missing Message Content intent."""
        gw, handler = gateway
        message = make_message(content="   ")

        await gw._on_message(message)

        handler.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    @pytest.mark.parametrize("mention_fmt", ["<@{id}>", "<@!{id}>"])
    async def test_bot_mention_stripped_before_dispatch(self, gateway, mention_fmt: str) -> None:  # type: ignore[no-untyped-def]
        """In a server channel the user @-mentions the bot; the raw snowflake
        must not reach the parser. Discord sends the nickname form <@!id> when
        the mentioned user has a per-server nickname, and the plain <@id> form
        otherwise - both must be stripped."""
        gw, handler = gateway
        mention = mention_fmt.format(id=BOT_ID)
        message = make_message(content=f"{mention} book 8/2 at 5:06p", in_guild=True)

        await gw._on_message(message)

        handler.assert_awaited_once_with(ALLOWED_ID, "book 8/2 at 5:06p", GUILD_CHANNEL_ID)

    @pytest.mark.parametrize("mention_fmt", ["<@{id}>", "<@!{id}>"])
    async def test_mention_only_message_ignored(self, gateway, mention_fmt: str) -> None:  # type: ignore[no-untyped-def]
        gw, handler = gateway
        message = make_message(content=mention_fmt.format(id=BOT_ID), in_guild=True)

        await gw._on_message(message)

        handler.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_handler_error_reported_to_user(self, gateway) -> None:  # type: ignore[no-untyped-def]
        gw, handler = gateway
        handler.side_effect = RuntimeError("gemini exploded")
        message = make_message()

        await gw._on_message(message)

        message.channel.send.assert_awaited_once()
        assert "went wrong" in message.channel.send.await_args.args[0]

    async def test_long_response_sent_in_chunks(self, gateway) -> None:  # type: ignore[no-untyped-def]
        gw, handler = gateway
        handler.return_value = "z" * (MAX_MESSAGE_LEN + 10)
        message = make_message()

        await gw._on_message(message)

        assert message.channel.send.await_count == 2
