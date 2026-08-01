"""Tests for the Discord messaging provider and gateway message filtering."""

import json

import httpx
import pytest

from app.config import settings
from app.providers.discord_provider import (
    MAX_MESSAGE_LEN,
    DiscordProvider,
    split_message,
)
from app.services.discord_gateway import should_handle_message


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

    def test_validate_request_always_true(self) -> None:
        provider = DiscordProvider()
        assert provider.validate_request("http://x", {}, None)


class TestShouldHandleMessage:
    ALLOWED = "111222333444555666"

    def test_dm_from_allowed_user(self) -> None:
        assert should_handle_message(self.ALLOWED, False, True, self.ALLOWED)

    def test_dm_from_other_user_ignored(self) -> None:
        assert not should_handle_message("999", False, True, self.ALLOWED)

    def test_guild_message_ignored(self) -> None:
        assert not should_handle_message(self.ALLOWED, False, False, self.ALLOWED)

    def test_bot_message_ignored(self) -> None:
        assert not should_handle_message(self.ALLOWED, True, True, self.ALLOWED)

    def test_no_allowlist_fails_closed(self) -> None:
        assert not should_handle_message(self.ALLOWED, False, True, "")
