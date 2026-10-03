"""Tests for a member connecting their own Walden login (issue #240)."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api import onboarding, webhooks
from app.config import settings
from app.models.database import Base
from app.providers import telegram_provider
from app.providers.telegram_provider import (
    TelegramProvider,
    parse_bot_command,
    validate_init_data,
)
from app.providers.walden_http_login import LoginCheck, LoginOutcome
from app.services import member_setup, telegram_members
from app.services.credential_service import credential_service
from app.services.member_setup import SubmitStatus, handle_command, submit_login

TOKEN = "123456:test-bot-token"
MEMBER = 555
LOGIN = "M-unit-login"
PASSWORD = "unit-test-password-42"
USER = {"id": MEMBER, "first_name": "Sam", "username": "sam_golf", "is_bot": False}


def sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Sign init data independently of the code under test, per Telegram's spec."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def init_data(user: dict | None = None, auth_date: int | None = None, **extra: str) -> str:
    fields = {
        "user": json.dumps(user or USER),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH-test",
        **extra,
    }
    return sign(fields)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    member_setup.clear_rate_limits()
    telegram_members.clear_membership_cache()
    telegram_provider._bot_username = "teetimebot"
    telegram_provider._bot_username_resolved = True
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", str(MEMBER))
    monkeypatch.setattr(settings, "telegram_members_chat_id", "")
    monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
    monkeypatch.setattr(settings, "telegram_webhook_base_url", "https://teetime.example.com")
    monkeypatch.setattr(settings, "racer_max_requesters", 4)


@pytest_asyncio.fixture
async def test_db(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.credential_service.AsyncSessionLocal", session_local)
    monkeypatch.setattr("app.services.database_service.AsyncSessionLocal", session_local)
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    yield
    await engine.dispose()


def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_send(  # type: ignore[no-untyped-def]
        to_number, message, origin_channel_id=None, channel=None, reply_to_message_id=None
    ):
        sent.append({"to": to_number, "message": message, "chat": origin_channel_id})
        return "msg-1"

    monkeypatch.setattr(member_setup.sms_service, "send_sms", fake_send)
    return sent


def walden_says(monkeypatch: pytest.MonkeyPatch, *outcomes: LoginOutcome) -> list[tuple]:
    """Replace the Walden check with scripted outcomes; record what it was asked."""
    asked: list[tuple] = []
    queue = list(outcomes)

    async def fake_check(login: str, password: str) -> LoginCheck:
        asked.append((login, password))
        return LoginCheck(queue.pop(0) if len(queue) > 1 else queue[0], "scripted")

    monkeypatch.setattr(member_setup, "check_login", fake_check)
    return asked


class TestValidateInitData:
    def test_valid_signature(self) -> None:
        user = validate_init_data(init_data())
        assert user is not None and user["id"] == MEMBER

    def test_tampered_user_refused(self) -> None:
        raw = init_data()
        forged = raw.replace("%3A+555", "%3A+999", 1)
        assert forged != raw
        assert validate_init_data(forged) is None

    def test_signed_with_another_bot_refused(self) -> None:
        fields = {"user": json.dumps(USER), "auth_date": str(int(time.time()))}
        assert validate_init_data(sign(fields, token="999:other-bot")) is None

    def test_stale_refused(self) -> None:
        assert validate_init_data(init_data(auth_date=int(time.time()) - 3600)) is None

    def test_from_the_future_refused(self) -> None:
        assert validate_init_data(init_data(auth_date=int(time.time()) + 3600)) is None

    def test_missing_hash_refused(self) -> None:
        raw = init_data()
        stripped = "&".join(p for p in raw.split("&") if not p.startswith("hash="))
        assert validate_init_data(stripped) is None

    def test_repeated_key_refused(self) -> None:
        assert validate_init_data(init_data() + "&auth_date=1") is None

    def test_no_bot_token_refuses_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "telegram_bot_token", "")
        assert validate_init_data(init_data()) is None

    def test_user_without_numeric_id_refused(self) -> None:
        assert validate_init_data(init_data(user={"id": "555"})) is None

    @pytest.mark.parametrize("raw", ["", "garbage", "hash=abc"])
    def test_junk_refused(self, raw: str) -> None:
        assert validate_init_data(raw) is None


class TestParseBotCommand:
    @staticmethod
    def entity(text: str) -> list[dict]:
        word = text.split()[0]
        return [{"type": "bot_command", "offset": 0, "length": len(word.encode("utf-16-le")) // 2}]

    def test_command_and_args(self) -> None:
        text = "/start setup"
        assert parse_bot_command(text, self.entity(text), "teetimebot") == ("start", "setup")

    def test_command_suffixed_with_this_bot(self) -> None:
        text = "/Status@TeeTimeBot"
        assert parse_bot_command(text, self.entity(text), "teetimebot") == ("status", "")

    def test_command_for_another_bot(self) -> None:
        text = "/start@otherbot"
        assert parse_bot_command(text, self.entity(text), "teetimebot") is None

    def test_plain_text(self) -> None:
        assert parse_bot_command("book 9/5 at 9a", None, "teetimebot") is None

    def test_mention_is_not_a_command(self) -> None:
        text = "@teetimebot status"
        entities = [{"type": "mention", "offset": 0, "length": 11}]
        assert parse_bot_command(text, entities, "teetimebot") is None


class TestCommands:
    async def test_start_offers_the_form(self, test_db: None) -> None:
        reply = await handle_command("start", "setup", str(MEMBER), True, "teetimebot")
        assert reply.open_form is True
        assert "never a chat message" in reply.text

    async def test_start_when_already_connected(self, test_db: None) -> None:
        await credential_service.set_credentials(str(MEMBER), LOGIN, PASSWORD)
        reply = await handle_command("start", "", str(MEMBER), True, "teetimebot")
        assert reply.open_form is True
        assert "already connected" in reply.text

    async def test_login_offers_the_form(self, test_db: None) -> None:
        reply = await handle_command("login", "", str(MEMBER), True, "teetimebot")
        assert reply.open_form is True

    async def test_no_public_url_means_no_form(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        monkeypatch.setattr(settings, "telegram_webhook_base_url", "http://localhost:8000")
        reply = await handle_command("start", "", str(MEMBER), True, "teetimebot")
        assert reply.open_form is False
        assert "isn't available" in reply.text

    async def test_in_a_group_points_to_a_private_chat(self, test_db: None) -> None:
        reply = await handle_command("status", "", str(MEMBER), False, "teetimebot")
        assert reply.open_form is False
        assert "https://t.me/teetimebot?start=setup" in reply.text

    async def test_status_without_a_login(self, test_db: None) -> None:
        reply = await handle_command("status", "", str(MEMBER), True, "teetimebot")
        assert "don't have a Walden login" in reply.text

    async def test_status_never_shows_the_login(self, test_db: None) -> None:
        from datetime import datetime

        await credential_service.set_credentials(
            str(MEMBER), LOGIN, PASSWORD, verified_at=datetime(2026, 10, 3, 1, 0)
        )
        reply = await handle_command("status", "", str(MEMBER), True, "teetimebot")
        assert "accepted it on 2026-10-03" in reply.text
        assert LOGIN not in reply.text and PASSWORD not in reply.text

    async def test_status_for_an_admin_added_login(self, test_db: None) -> None:
        await credential_service.set_credentials(str(MEMBER), LOGIN, PASSWORD)
        reply = await handle_command("status", "", str(MEMBER), True, "teetimebot")
        assert "added before logins were checked" in reply.text

    async def test_forget_asks_for_confirmation_first(self, test_db: None) -> None:
        await credential_service.set_credentials(str(MEMBER), LOGIN, PASSWORD)
        reply = await handle_command("forget", "", str(MEMBER), True, "teetimebot")
        assert "/forget confirm" in reply.text
        assert await credential_service.get_owner(str(MEMBER)) is not None

    async def test_forget_confirm_deletes(self, test_db: None) -> None:
        await credential_service.set_credentials(str(MEMBER), LOGIN, PASSWORD)
        reply = await handle_command("forget", "confirm", str(MEMBER), True, "teetimebot")
        assert "Deleted your Walden login" in reply.text
        assert await credential_service.get_owner(str(MEMBER)) is None

    async def test_forget_with_nothing_stored(self, test_db: None) -> None:
        reply = await handle_command("forget", "confirm", str(MEMBER), True, "teetimebot")
        assert "nothing to delete" in reply.text


class TestSubmitLogin:
    async def test_accepted_login_is_saved_verified_and_confirmed(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent = sent_messages(monkeypatch)
        asked = walden_says(monkeypatch, LoginOutcome.ACCEPTED)

        result = await submit_login(USER, f"  {LOGIN} ", PASSWORD, consent=True)

        assert result.status is SubmitStatus.SAVED
        assert asked == [(LOGIN, PASSWORD)]
        creds = await credential_service.require_credentials(str(MEMBER))
        assert (creds.member_number, creds.password) == (LOGIN, PASSWORD)
        owner = await credential_service.get_owner(str(MEMBER))
        assert owner is not None and owner.verified_at is not None
        assert owner.name == "Sam" and owner.telegram_username == "sam_golf"
        assert [m["to"] for m in sent] == [str(MEMBER)]
        assert "You're connected" in sent[0]["message"]

    async def test_rejected_login_is_not_saved(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent_messages(monkeypatch)
        walden_says(monkeypatch, LoginOutcome.REJECTED)
        result = await submit_login(USER, LOGIN, PASSWORD, consent=True)
        assert result.status is SubmitStatus.REJECTED
        assert await credential_service.get_owner(str(MEMBER)) is None

    async def test_unanswered_check_saves_nothing(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent_messages(monkeypatch)
        walden_says(monkeypatch, LoginOutcome.UNKNOWN)
        result = await submit_login(USER, LOGIN, PASSWORD, consent=True)
        assert result.status is SubmitStatus.UNKNOWN
        assert await credential_service.get_owner(str(MEMBER)) is None

    async def test_replacing_a_login_resets_its_verification(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """The admin script re-saving a row must not inherit the form's check."""
        sent_messages(monkeypatch)
        walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        await submit_login(USER, LOGIN, PASSWORD, consent=True)
        await credential_service.set_credentials(str(MEMBER), LOGIN, "changed-by-script")
        owner = await credential_service.get_owner(str(MEMBER))
        assert owner is not None and owner.verified_at is None

    async def test_unauthorized_user_refused_before_walden_is_asked(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        asked = walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        stranger = {**USER, "id": 999}
        result = await submit_login(stranger, LOGIN, PASSWORD, consent=True)
        assert result.status is SubmitStatus.FORBIDDEN
        assert asked == []

    @pytest.mark.parametrize(
        "login,password,consent",
        [(LOGIN, PASSWORD, False), ("", PASSWORD, True), (LOGIN, "", True), ("x" * 200, "p", True)],
        ids=["no-consent", "no-login", "no-password", "too-long"],
    )
    async def test_invalid_input_never_reaches_walden(
        self,
        monkeypatch: pytest.MonkeyPatch,
        test_db: None,
        login: str,
        password: str,
        consent: bool,
    ) -> None:
        asked = walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        result = await submit_login(USER, login, password, consent=consent)
        assert result.status is SubmitStatus.INVALID
        assert asked == []

    async def test_three_rejections_an_hour_then_wait(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """Each rejection is a failed login on the member's real Walden account."""
        asked = walden_says(monkeypatch, LoginOutcome.REJECTED)
        for _ in range(3):
            assert (await submit_login(USER, LOGIN, PASSWORD, True)).status is SubmitStatus.REJECTED
        result = await submit_login(USER, LOGIN, PASSWORD, True)
        assert result.status is SubmitStatus.RATE_LIMITED
        assert len(asked) == 3

    async def test_rejections_age_out(self, monkeypatch: pytest.MonkeyPatch, test_db: None) -> None:
        now = [1000.0]
        monkeypatch.setattr(member_setup.time, "monotonic", lambda: now[0])
        walden_says(monkeypatch, LoginOutcome.REJECTED)
        for _ in range(3):
            await submit_login(USER, LOGIN, PASSWORD, True)
        now[0] += 3601
        assert (await submit_login(USER, LOGIN, PASSWORD, True)).status is SubmitStatus.REJECTED

    async def test_unanswered_checks_are_limited_separately(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        asked = walden_says(monkeypatch, LoginOutcome.UNKNOWN)
        for _ in range(member_setup.MAX_ATTEMPTS_PER_HOUR):
            await submit_login(USER, LOGIN, PASSWORD, True)
        result = await submit_login(USER, LOGIN, PASSWORD, True)
        assert result.status is SubmitStatus.RATE_LIMITED
        assert len(asked) == member_setup.MAX_ATTEMPTS_PER_HOUR

    async def test_admin_warned_at_the_racer_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        monkeypatch.setattr(settings, "racer_max_requesters", 2)
        await credential_service.set_credentials("777", "other", "pw")
        sent = sent_messages(monkeypatch)
        walden_says(monkeypatch, LoginOutcome.ACCEPTED)

        await submit_login(USER, LOGIN, PASSWORD, True)

        to_admin = [m for m in sent if m["to"] == "111"]
        assert len(to_admin) == 1
        assert "2 members now have Walden logins" in to_admin[0]["message"]

    async def test_no_warning_below_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent = sent_messages(monkeypatch)
        walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        await submit_login(USER, LOGIN, PASSWORD, True)
        assert [m["to"] for m in sent] == [str(MEMBER)]


class TestConcurrency:
    """One member's check-then-save is serialized with itself and with forgetting."""

    @staticmethod
    def held_walden(monkeypatch: pytest.MonkeyPatch, outcome: LoginOutcome) -> asyncio.Event:
        """A Walden check that does not answer until the returned event is set."""
        release = asyncio.Event()

        async def fake_check(login: str, password: str) -> LoginCheck:
            await release.wait()
            return LoginCheck(outcome, "scripted")

        monkeypatch.setattr(member_setup, "check_login", fake_check)
        return release

    async def test_a_second_submission_mid_check_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """Otherwise parallel tries all pass the rejection limit before any is recorded."""
        release = self.held_walden(monkeypatch, LoginOutcome.REJECTED)
        first = asyncio.create_task(submit_login(USER, LOGIN, PASSWORD, True))
        await asyncio.sleep(0)

        second = await submit_login(USER, LOGIN, PASSWORD, True)

        assert second.status is SubmitStatus.RATE_LIMITED
        release.set()
        assert (await first).status is SubmitStatus.REJECTED

    async def test_parallel_tries_cannot_beat_the_rejection_limit(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        release = self.held_walden(monkeypatch, LoginOutcome.REJECTED)
        tasks = [asyncio.create_task(submit_login(USER, LOGIN, PASSWORD, True)) for _ in range(6)]
        await asyncio.sleep(0)
        release.set()
        results = [r.status for r in await asyncio.gather(*tasks)]
        assert results.count(SubmitStatus.REJECTED) == 1
        assert results.count(SubmitStatus.RATE_LIMITED) == 5

    async def test_forget_during_a_check_is_not_undone(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """/forget waits for the check in flight, then deletes what it saved."""
        sent_messages(monkeypatch)
        release = self.held_walden(monkeypatch, LoginOutcome.ACCEPTED)
        saving = asyncio.create_task(submit_login(USER, LOGIN, PASSWORD, True))
        await asyncio.sleep(0)
        forgetting = asyncio.create_task(
            handle_command("forget", "confirm", str(MEMBER), True, "teetimebot")
        )
        await asyncio.sleep(0)

        release.set()
        assert (await saving).status is SubmitStatus.SAVED
        reply = await forgetting

        assert "Deleted your Walden login" in reply.text
        assert await credential_service.get_owner(str(MEMBER)) is None


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(onboarding.router)
    app.include_router(webhooks.router)
    return TestClient(app, raise_server_exceptions=False)


class TestSetupPage:
    def test_served_with_a_strict_policy(self, client: TestClient) -> None:
        resp = client.get("/onboarding/walden")
        assert resp.status_code == 200
        csp = resp.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'nonce-" in csp
        assert "frame-ancestors https://web.telegram.org" in csp
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["referrer-policy"] == "no-referrer"

    def test_loads_no_external_script(self, client: TestClient) -> None:
        html = client.get("/onboarding/walden").text
        assert "<script src" not in html and "telegram-web-app.js" not in html

    def test_nonce_differs_per_response(self, client: TestClient) -> None:
        first = client.get("/onboarding/walden").headers["content-security-policy"]
        second = client.get("/onboarding/walden").headers["content-security-policy"]
        assert first != second

    def test_password_managers_can_fill_it(self, client: TestClient) -> None:
        html = client.get("/onboarding/walden").text
        assert 'autocomplete="username"' in html
        assert 'autocomplete="current-password"' in html


class TestSetupSubmit:
    def post(self, client: TestClient, body: object, init: str | None = None):  # type: ignore[no-untyped-def]
        return client.post(
            "/onboarding/walden",
            content=body if isinstance(body, str) else json.dumps(body),
            headers={"X-Telegram-Init-Data": init_data() if init is None else init},
        )

    def test_saved(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent_messages(monkeypatch)
        walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        resp = self.post(client, {"login": LOGIN, "password": PASSWORD, "consent": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "saved"
        assert resp.headers["cache-control"] == "no-store"

    def test_without_init_data_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        resp = self.post(client, {"login": LOGIN, "password": PASSWORD, "consent": True}, init="")
        assert resp.status_code == 403
        assert asked == []

    def test_forged_init_data_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = walden_says(monkeypatch, LoginOutcome.ACCEPTED)
        forged = sign({"user": json.dumps(USER), "auth_date": str(int(time.time()))}, token="9:x")
        resp = self.post(client, {"login": LOGIN, "password": PASSWORD, "consent": True}, forged)
        assert resp.status_code == 403
        assert asked == []

    @pytest.mark.parametrize(
        "body",
        [
            "not json " + PASSWORD,
            ["login", PASSWORD],
            {"login": LOGIN, "password": 12345, "consent": True},
            {"login": LOGIN, "consent": True},
        ],
        ids=["not-json", "a-list", "password-not-a-string", "no-password"],
    )
    def test_bad_body_never_echoes_input(self, client: TestClient, body: object) -> None:
        resp = self.post(client, body)
        assert resp.status_code == 400
        assert PASSWORD not in resp.text and LOGIN not in resp.text

    def test_a_crash_saves_nothing_and_says_so(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("database down")

        monkeypatch.setattr(onboarding, "submit_login", boom)
        resp = self.post(client, {"login": LOGIN, "password": PASSWORD, "consent": True})
        assert resp.json()["status"] == "unknown"
        assert PASSWORD not in resp.text

    @pytest.mark.parametrize("outcome", list(LoginOutcome))
    def test_password_never_logged_or_returned(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        test_db: None,
        outcome: LoginOutcome,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        sent = sent_messages(monkeypatch)
        walden_says(monkeypatch, outcome)
        resp = self.post(client, {"login": LOGIN, "password": PASSWORD, "consent": True})
        assert PASSWORD not in caplog.text and LOGIN not in caplog.text
        assert PASSWORD not in resp.text and LOGIN not in resp.text
        assert all(PASSWORD not in m["message"] and LOGIN not in m["message"] for m in sent)


class TestWebhookCommands:
    @pytest.fixture
    def webhook_client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        app = FastAPI()
        app.include_router(webhooks.router)
        return TestClient(app)

    @staticmethod
    def update(text: str, chat_type: str = "private", chat_id: int = MEMBER) -> dict:
        word = text.split()[0]
        return {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "from": {"id": MEMBER, "is_bot": False, "first_name": "Sam"},
                "chat": {"id": chat_id, "type": chat_type},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(word)}],
            },
        }

    def test_start_in_private_sends_the_form_button(
        self, webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        buttons: list[tuple] = []
        menus: list[tuple] = []

        async def fake_button(self, chat_id, text, button_text, url):  # type: ignore[no-untyped-def]
            buttons.append((chat_id, button_text, url))

        async def fake_menu(self, chat_id, button_text, url):  # type: ignore[no-untyped-def]
            menus.append((chat_id, url))
            return True

        async def no_parser(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("a setup command must not reach the parser")

        monkeypatch.setattr(TelegramProvider, "send_web_app_button", fake_button)
        monkeypatch.setattr(TelegramProvider, "set_chat_menu_button", fake_menu)
        monkeypatch.setattr(webhooks.booking_service, "handle_incoming_message", no_parser)

        resp = webhook_client.post(
            "/webhooks/telegram",
            json=self.update("/start setup"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

        assert resp.json() == {"status": "ok"}
        url = "https://teetime.example.com/onboarding/walden"
        assert buttons == [(str(MEMBER), "Connect Walden account", url)]
        assert menus == [(str(MEMBER), url)]

    def test_status_in_a_group_replies_with_the_private_link(
        self, webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent: list[dict] = []

        async def fake_send(to_number, message, **kwargs):  # type: ignore[no-untyped-def]
            sent.append({"to": to_number, "message": message, **kwargs})
            return "msg-1"

        monkeypatch.setattr(webhooks.sms_service, "send_sms", fake_send)
        resp = webhook_client.post(
            "/webhooks/telegram",
            json=self.update("/status", chat_type="supergroup", chat_id=-1001234567890),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

        assert resp.json() == {"status": "ok"}
        assert len(sent) == 1
        assert "https://t.me/teetimebot?start=setup" in sent[0]["message"]
        assert sent[0]["origin_channel_id"] == "-1001234567890"

    def test_other_commands_still_reach_the_parser(
        self, webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def fake_handle(phone_number, message, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(message)
            return "ok"

        async def fake_send(*args, **kwargs):  # type: ignore[no-untyped-def]
            return "msg-1"

        monkeypatch.setattr(webhooks.booking_service, "handle_incoming_message", fake_handle)
        monkeypatch.setattr(webhooks.sms_service, "send_sms", fake_send)
        webhook_client.post(
            "/webhooks/telegram",
            json=self.update("/book 9/5 at 9a"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert seen == ["9/5 at 9a"]
