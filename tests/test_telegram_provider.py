"""Tests for the Telegram messaging provider and its inbound webhook."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.providers.sms_base import split_message
from app.providers.telegram_provider import (
    MAX_MESSAGE_LEN,
    TelegramProvider,
    is_authorized_user,
    verify_webhook_secret,
)


class TestSplitMessage:
    def test_respects_telegram_limit(self) -> None:
        msg = "z" * (MAX_MESSAGE_LEN * 2 + 10)
        chunks = split_message(msg, MAX_MESSAGE_LEN)
        assert all(len(c) <= MAX_MESSAGE_LEN for c in chunks)
        assert "".join(chunks) == msg


class TestIsAuthorizedUser:
    def test_allowed_user_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", "111,222")
        assert is_authorized_user("222", is_bot=False) is True

    def test_other_user_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", "111")
        assert is_authorized_user("999", is_bot=False) is False

    def test_bots_ignored_even_when_allowlisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", "111")
        assert is_authorized_user("111", is_bot=True) is False

    def test_empty_allowlist_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", "")
        assert is_authorized_user("111", is_bot=False) is False


class TestVerifyWebhookSecret:
    def test_matching_secret_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        assert verify_webhook_secret("s3cret") is True

    def test_wrong_secret_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        assert verify_webhook_secret("nope") is False

    def test_missing_header_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        assert verify_webhook_secret(None) is False

    def test_unconfigured_secret_rejects_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset secret must fail closed, not wave every caller through."""
        monkeypatch.setattr(settings, "telegram_webhook_secret", "")
        assert verify_webhook_secret("anything") is False
        assert verify_webhook_secret("") is False


class TestSettingsValidation:
    def test_username_rejected_as_allowlist(self) -> None:
        with pytest.raises(ValidationError, match="numeric Telegram user IDs"):
            Settings(telegram_allowed_user_ids="@alexenos")

    def test_numeric_list_accepted(self) -> None:
        assert Settings(telegram_allowed_user_ids="1, 2 ,3").telegram_allowed_ids() == {
            "1",
            "2",
            "3",
        }

    def test_unset_allowlist_is_empty(self) -> None:
        assert Settings(telegram_allowed_user_ids="").telegram_allowed_ids() == frozenset()


class TestResolveChatId:
    def test_origin_chat_wins(self) -> None:
        assert TelegramProvider.resolve_chat_id("111", "222") == "222"

    def test_negative_group_chat_accepted(self) -> None:
        """Group chat IDs are negative, so this cannot use isdigit()."""
        assert TelegramProvider.resolve_chat_id("111", "-1001234567890") == "-1001234567890"

    def test_no_origin_falls_back_to_user(self) -> None:
        assert TelegramProvider.resolve_chat_id("111", None) == "111"

    def test_non_numeric_origin_ignored(self) -> None:
        assert TelegramProvider.resolve_chat_id("111", "#general") == "111"


def make_provider(handler) -> TelegramProvider:  # type: ignore[no-untyped-def]
    return TelegramProvider(transport=httpx.MockTransport(handler))


class TestSendSms:
    async def test_sends_message_to_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

        result = await make_provider(handler).send_sms("111", "tee time confirmed", "-100999")

        assert result.success
        assert result.message_sid == "7"
        assert json.loads(requests[0].content) == {
            "chat_id": "-100999",
            "text": "tee time confirmed",
        }
        assert requests[0].url.path.endswith("/bottest-token/sendMessage")

    async def test_long_message_sent_in_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
        texts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            texts.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(texts)}})

        result = await make_provider(handler).send_sms("111", "y" * (MAX_MESSAGE_LEN + 5))

        assert result.success
        assert len(texts) == 2
        assert all(len(t) <= MAX_MESSAGE_LEN for t in texts)

    async def test_api_error_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="Forbidden: bot was blocked by the user")

        result = await make_provider(handler).send_sms("111", "hello")

        assert not result.success
        assert result.error_message is not None
        assert "403" in result.error_message

    async def test_missing_token_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "")

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
            raise AssertionError("should not call the API without a token")

        result = await make_provider(handler).send_sms("111", "hello")
        assert not result.success


class TestRegisterWebhook:
    async def test_registers_with_secret_and_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        monkeypatch.setattr(settings, "telegram_webhook_base_url", "https://teetime.example.com/")
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": True})

        assert await make_provider(handler).register_webhook() is True
        assert bodies[0] == {
            "url": "https://teetime.example.com/webhooks/telegram",
            "secret_token": "s3cret",
            "allowed_updates": ["message"],
        }

    async def test_skipped_without_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registering an unauthenticated webhook would accept forged bookings."""
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
        monkeypatch.setattr(settings, "telegram_webhook_secret", "")
        monkeypatch.setattr(settings, "telegram_webhook_base_url", "https://teetime.example.com")

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
            raise AssertionError("should not register without a secret")

        assert await make_provider(handler).register_webhook() is False

    async def test_skipped_without_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        monkeypatch.setattr(settings, "telegram_webhook_base_url", "")

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
            raise AssertionError("should not register without a base URL")

        assert await make_provider(handler).register_webhook() is False

    async def test_api_failure_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        monkeypatch.setattr(settings, "telegram_webhook_base_url", "https://teetime.example.com")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        assert await make_provider(handler).register_webhook() is False


class TestTelegramWebhookRoute:
    """The inbound half: what the public endpoint does with an update."""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        from fastapi import FastAPI

        from app.api import webhooks

        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", "111")
        app = FastAPI()
        app.include_router(webhooks.router)
        return TestClient(app)

    @staticmethod
    def _update(text: str = "book 9/5 at 9a", user_id: int = 111, chat_id: int = 111) -> dict:
        return {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "from": {"id": user_id, "is_bot": False, "first_name": "Alex"},
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
            },
        }

    def test_wrong_secret_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/webhooks/telegram",
            json=self._update(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_missing_secret_header_rejected(self, client: TestClient) -> None:
        resp = client.post("/webhooks/telegram", json=self._update())
        assert resp.status_code == 403

    def test_authorized_message_dispatched(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api import webhooks

        seen: dict = {}

        async def fake_handle(phone_number, message, origin_channel_id=None, channel=None):  # type: ignore[no-untyped-def]
            seen.update(
                phone_number=phone_number,
                message=message,
                origin_channel_id=origin_channel_id,
                channel=channel,
            )
            return "Scheduling that for you."

        sent: dict = {}

        async def fake_send(to_number, message, origin_channel_id=None, channel=None):  # type: ignore[no-untyped-def]
            sent.update(to_number=to_number, message=message, channel=channel)
            return "msg-1"

        monkeypatch.setattr(webhooks.booking_service, "handle_incoming_message", fake_handle)
        monkeypatch.setattr(webhooks.sms_service, "send_sms", fake_send)

        resp = client.post(
            "/webhooks/telegram",
            json=self._update(chat_id=-1001234567890),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

        assert resp.status_code == 200
        assert seen == {
            "phone_number": "111",
            "message": "book 9/5 at 9a",
            "origin_channel_id": "-1001234567890",
            "channel": "telegram",
        }
        assert sent["message"] == "Scheduling that for you."
        assert sent["channel"] == "telegram"

    def test_unauthorized_user_ignored_with_200(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram retries non-2xx; retrying will not make this interesting."""
        from app.api import webhooks

        async def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("should not dispatch an unauthorized message")

        monkeypatch.setattr(webhooks.booking_service, "handle_incoming_message", fail)

        resp = client.post(
            "/webhooks/telegram",
            json=self._update(user_id=999),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}

    def test_non_message_update_ignored_with_200(self, client: TestClient) -> None:
        resp = client.post(
            "/webhooks/telegram",
            json={"update_id": 1, "edited_message": {}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}

    def test_empty_text_ignored_with_200(self, client: TestClient) -> None:
        resp = client.post(
            "/webhooks/telegram",
            json=self._update(text="   "),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}
