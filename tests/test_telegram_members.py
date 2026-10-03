"""Tests for authorizing by members-group membership, and offboarding (issue #239)."""

import json
from datetime import time, timedelta

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.models.database import Base
from app.models.schemas import BookingStatus, TeeTimeBooking, TeeTimeRequest
from app.providers import telegram_provider
from app.providers.telegram_provider import (
    ChatMemberLookupError,
    TelegramProvider,
    is_member_status,
)
from app.services import telegram_members
from app.services.credential_service import credential_service
from app.services.database_service import database_service
from app.utils.timezone import CTDateTime

GROUP = "-1001234567890"
MEMBER = "555"


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Group access on, an allowlist that does not include MEMBER, a cold cache."""
    telegram_members.clear_membership_cache()
    telegram_provider._bot_username = "teetimebot"
    telegram_provider._bot_username_resolved = True
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_members_chat_id", GROUP)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "111")
    monkeypatch.setattr(settings, "telegram_admin_user_id", "111")


def _patch_lookup(monkeypatch: pytest.MonkeyPatch, answer) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """Replace getChatMember; `answer` is a dict, None, or an exception to raise."""
    calls: list[tuple[str, str]] = []

    async def fake(self, chat_id: str, user_id: str):  # type: ignore[no-untyped-def]
        calls.append((chat_id, user_id))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(TelegramProvider, "get_chat_member", fake)
    return calls


def _sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture outbound messages instead of sending them."""
    sent: list[dict] = []

    async def fake_send(  # type: ignore[no-untyped-def]
        to_number, message, origin_channel_id=None, channel=None, reply_to_message_id=None
    ):
        sent.append(
            {"to": to_number, "message": message, "chat": origin_channel_id, "channel": channel}
        )
        return "msg-1"

    monkeypatch.setattr(telegram_members.sms_service, "send_sms", fake_send)
    return sent


class TestIsMemberStatus:
    @pytest.mark.parametrize("status", ["creator", "administrator", "member"])
    def test_in_the_group(self, status: str) -> None:
        assert is_member_status({"status": status}) is True

    @pytest.mark.parametrize("status", ["left", "kicked", "", "something-new"])
    def test_out_of_the_group(self, status: str) -> None:
        assert is_member_status({"status": status}) is False

    def test_restricted_member_still_in(self) -> None:
        assert is_member_status({"status": "restricted", "is_member": True}) is True

    def test_restricted_after_leaving_is_out(self) -> None:
        assert is_member_status({"status": "restricted", "is_member": False}) is False


class TestSettings:
    def test_group_id_accepted(self) -> None:
        assert Settings(telegram_members_chat_id=GROUP).telegram_members_chat() == GROUP

    def test_unset_turns_group_access_off(self) -> None:
        assert Settings(telegram_members_chat_id="  ").telegram_members_chat() is None

    @pytest.mark.parametrize("value", ["1234567890", "@northgate", "https://t.me/+abc"])
    def test_not_a_group_id_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError, match="TELEGRAM_MEMBERS_CHAT_ID"):
            Settings(telegram_members_chat_id=value)


class TestGetChatMember:
    @staticmethod
    def _provider(handler) -> TelegramProvider:  # type: ignore[no-untyped-def]
        return TelegramProvider(transport=httpx.MockTransport(handler))

    async def test_returns_member(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"status": "member"}})

        member = await self._provider(handler).get_chat_member(GROUP, MEMBER)
        assert member == {"status": "member"}
        assert bodies == [{"chat_id": GROUP, "user_id": 555}]

    async def test_unknown_user_is_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"ok": False, "description": "user not found"})

        assert await self._provider(handler).get_chat_member(GROUP, MEMBER) is None

    async def test_server_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="Bad Gateway")

        with pytest.raises(ChatMemberLookupError):
            await self._provider(handler).get_chat_member(GROUP, MEMBER)

    async def test_network_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        with pytest.raises(ChatMemberLookupError):
            await self._provider(handler).get_chat_member(GROUP, MEMBER)


class TestIsAuthorized:
    async def test_allowlisted_user_needs_no_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_lookup(monkeypatch, AssertionError("should not look up"))
        assert await telegram_members.is_authorized("111", is_bot=False) is True
        assert calls == []

    async def test_group_member_authorized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True
        assert calls == [(GROUP, MEMBER)]

    async def test_non_member_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_lookup(monkeypatch, {"status": "left"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False

    async def test_never_in_the_group_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_lookup(monkeypatch, None)
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False

    async def test_bots_refused_without_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=True) is False
        assert calls == []

    async def test_group_access_off_means_allowlist_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "telegram_members_chat_id", "")
        calls = _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False
        assert calls == []

    async def test_answer_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True
        assert len(calls) == 1

    async def test_cached_answer_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(telegram_members.time, "monotonic", lambda: now[0])
        calls = _patch_lookup(monkeypatch, {"status": "left"})

        await telegram_members.is_authorized(MEMBER, is_bot=False)
        now[0] += telegram_members.NON_MEMBER_CACHE_SECONDS + 1
        await telegram_members.is_authorized(MEMBER, is_bot=False)

        assert len(calls) == 2

    async def test_lookup_failure_refuses_and_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An outage fails closed, but must not keep a member out once it ends."""
        _patch_lookup(monkeypatch, ChatMemberLookupError("HTTP 502"))
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False

        calls = _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True
        assert len(calls) == 1


def _chat_member_update(old: str, new: str, chat_id: str = GROUP, user_id: int = 555) -> dict:
    user = {"id": user_id, "is_bot": False, "first_name": "Sam", "username": "sam_golf"}
    return {
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "from": {"id": 111, "is_bot": False, "first_name": "Admin"},
        "date": 1,
        "old_chat_member": {"status": old, "user": user},
        "new_chat_member": {"status": new, "user": user},
    }


class TestJoin:
    @pytest.fixture(autouse=True)
    def _telegram_says_they_joined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A join is re-checked live; by default Telegram agrees they are in."""
        _patch_lookup(monkeypatch, {"status": "member"})

    async def test_welcomes_in_the_group_with_a_deep_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = _sent(monkeypatch)

        status = await telegram_members.handle_chat_member_update(
            _chat_member_update("left", "member")
        )

        assert status == "ok"
        assert len(sent) == 1
        assert sent[0]["chat"] == GROUP
        assert sent[0]["message"].startswith("@sam_golf welcome!")
        assert "https://t.me/teetimebot?start=setup" in sent[0]["message"]

    async def test_join_authorizes_without_waiting_for_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached "not a member" from before the approval must not linger."""
        _sent(monkeypatch)
        _patch_lookup(monkeypatch, {"status": "left"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False

        _patch_lookup(monkeypatch, {"status": "member"})
        await telegram_members.handle_chat_member_update(_chat_member_update("left", "member"))

        calls = _patch_lookup(monkeypatch, AssertionError("should answer from the cache"))
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True
        assert calls == []

    async def test_stale_join_after_leaving_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A late join must not re-authorize someone whose leave was processed."""
        sent = _sent(monkeypatch)
        _patch_lookup(monkeypatch, {"status": "left"})

        status = await telegram_members.handle_chat_member_update(
            _chat_member_update("left", "member")
        )

        assert status == "ignored"
        assert sent == []
        calls = _patch_lookup(monkeypatch, AssertionError("should answer from the cache"))
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False
        assert calls == []

    async def test_unconfirmed_join_is_welcomed_but_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = _sent(monkeypatch)
        _patch_lookup(monkeypatch, ChatMemberLookupError("HTTP 502"))

        status = await telegram_members.handle_chat_member_update(
            _chat_member_update("left", "member")
        )

        assert status == "ok"
        assert len(sent) == 1
        calls = _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True
        assert len(calls) == 1

    async def test_late_promotion_does_not_reauthorize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """member -> administrator says nothing about now; it must not be cached."""
        _sent(monkeypatch)
        telegram_members.record_membership(MEMBER, False)

        await telegram_members.handle_chat_member_update(
            _chat_member_update("member", "administrator")
        )

        calls = _patch_lookup(monkeypatch, AssertionError("should answer from the cache"))
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False
        assert calls == []

    async def test_other_chat_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = _sent(monkeypatch)
        update = _chat_member_update("left", "member", chat_id="-100999")
        assert await telegram_members.handle_chat_member_update(update) == "ignored"
        assert sent == []

    async def test_bot_joining_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = _sent(monkeypatch)
        update = _chat_member_update("left", "member")
        update["new_chat_member"]["user"]["is_bot"] = True
        assert await telegram_members.handle_chat_member_update(update) == "ignored"
        assert sent == []

    async def test_promotion_is_neither_join_nor_leave(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = _sent(monkeypatch)
        update = _chat_member_update("member", "administrator")
        assert await telegram_members.handle_chat_member_update(update) == "ignored"
        assert sent == []


@pytest_asyncio.fixture
async def test_db(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """An in-memory database behind both the booking and credential stores."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.database_service.AsyncSessionLocal", session_local)
    monkeypatch.setattr("app.services.credential_service.AsyncSessionLocal", session_local)
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    yield
    await engine.dispose()


def _booking(booking_id: str, status: BookingStatus, days_ahead: int = 7) -> TeeTimeBooking:
    return TeeTimeBooking(
        id=booking_id,
        phone_number=MEMBER,
        request=TeeTimeRequest(
            requested_date=CTDateTime.now().date() + timedelta(days=days_ahead),
            requested_time=time(8, 0),
        ),
        status=status,
    )


class TestLeave:
    @pytest.fixture(autouse=True)
    def _telegram_says_they_left(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Offboarding re-checks membership live; by default Telegram agrees they left."""
        _patch_lookup(monkeypatch, {"status": "left"})

    async def test_cancels_pending_deletes_login_and_tells_both(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent = _sent(monkeypatch)
        await credential_service.set_credentials(MEMBER, "M123", "pw")
        await database_service.create_booking(_booking("pend0001", BookingStatus.PENDING))
        await database_service.create_booking(_booking("schd0001", BookingStatus.SCHEDULED))
        await database_service.create_booking(_booking("done0001", BookingStatus.SUCCESS))
        await database_service.create_booking(_booking("fail0001", BookingStatus.FAILED))

        status = await telegram_members.handle_chat_member_update(
            _chat_member_update("member", "left")
        )

        assert status == "ok"
        statuses = {
            b.id: b.status for b in await database_service.get_bookings(phone_number=MEMBER)
        }
        assert statuses == {
            "pend0001": BookingStatus.CANCELLED,
            "schd0001": BookingStatus.CANCELLED,
            # A tee time already reserved at the club is the member's own.
            "done0001": BookingStatus.SUCCESS,
            "fail0001": BookingStatus.FAILED,
        }
        assert await credential_service.get_owner(MEMBER) is None

        to_member = [m for m in sent if m["to"] == MEMBER]
        to_admin = [m for m in sent if m["to"] == "111"]
        assert len(to_member) == 1 and len(to_admin) == 1
        assert "cancelled 2 pending booking requests" in to_member[0]["message"]
        assert "deleted the Walden login" in to_member[0]["message"]
        assert "1 tee time already reserved at the club" in to_member[0]["message"]
        assert to_admin[0]["message"] == (
            "Sam left the members group. Cancelled 2 pending bookings; "
            "deleted their Walden login."
        )

    async def test_removal_by_an_admin_counts_as_leaving(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        _sent(monkeypatch)
        await credential_service.set_credentials(MEMBER, "M123", "pw")

        await telegram_members.handle_chat_member_update(_chat_member_update("member", "kicked"))

        assert await credential_service.get_owner(MEMBER) is None

    async def test_leave_revokes_access_at_once(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        _sent(monkeypatch)
        _patch_lookup(monkeypatch, {"status": "member"})
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True

        _patch_lookup(monkeypatch, {"status": "left"})
        await telegram_members.handle_chat_member_update(_chat_member_update("member", "left"))

        calls = _patch_lookup(monkeypatch, AssertionError("should answer from the update"))
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is False
        assert calls == []

    async def test_stale_leave_after_rejoining_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """A redelivered leave must not offboard someone who is back in the group."""
        sent = _sent(monkeypatch)
        await credential_service.set_credentials(MEMBER, "M123", "pw")
        await database_service.create_booking(_booking("schd0001", BookingStatus.SCHEDULED))
        _patch_lookup(monkeypatch, {"status": "member"})

        status = await telegram_members.handle_chat_member_update(
            _chat_member_update("member", "left")
        )

        assert status == "ignored"
        assert sent == []
        assert await credential_service.get_owner(MEMBER) is not None
        [booking] = await database_service.get_bookings(phone_number=MEMBER)
        assert booking.status == BookingStatus.SCHEDULED
        assert await telegram_members.is_authorized(MEMBER, is_bot=False) is True

    async def test_failed_recheck_still_offboards(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """The update says they left; keeping a departed member's login is worse."""
        _sent(monkeypatch)
        await credential_service.set_credentials(MEMBER, "M123", "pw")
        _patch_lookup(monkeypatch, ChatMemberLookupError("HTTP 502"))

        await telegram_members.handle_chat_member_update(_chat_member_update("member", "left"))

        assert await credential_service.get_owner(MEMBER) is None

    async def test_running_twice_is_harmless(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        sent = _sent(monkeypatch)
        await credential_service.set_credentials(MEMBER, "M123", "pw")
        update = _chat_member_update("member", "left")

        await telegram_members.offboard(update["new_chat_member"]["user"])
        await telegram_members.offboard(update["new_chat_member"]["user"])

        assert sent[-1]["message"] == (
            "Sam left the members group. Cancelled 0 pending bookings; "
            "no Walden login was stored."
        )

    async def test_cancel_loses_to_a_racer_that_already_claimed(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """A row the racer moved to IN_PROGRESS after it was read stays IN_PROGRESS."""
        _sent(monkeypatch)
        await database_service.create_booking(_booking("schd0001", BookingStatus.SCHEDULED))
        stale = await database_service.get_booking("schd0001")
        assert stale is not None and stale.status == BookingStatus.SCHEDULED

        # The racer's claim commits between offboard's read and its cancel.
        claimed = stale.model_copy(update={"status": BookingStatus.IN_PROGRESS})
        await database_service.update_booking(claimed)

        assert await database_service.cancel_pending_booking("schd0001") is None
        current = await database_service.get_booking("schd0001")
        assert current is not None and current.status == BookingStatus.IN_PROGRESS

    async def test_allowlisted_leaver_keeps_everything(
        self, monkeypatch: pytest.MonkeyPatch, test_db: None
    ) -> None:
        """Still allowlisted means still authorized, so deleting would be wrong."""
        sent = _sent(monkeypatch)
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", f"111,{MEMBER}")
        await credential_service.set_credentials(MEMBER, "M123", "pw")
        await database_service.create_booking(_booking("schd0001", BookingStatus.SCHEDULED))

        await telegram_members.handle_chat_member_update(_chat_member_update("member", "left"))

        assert await credential_service.get_owner(MEMBER) is not None
        [booking] = await database_service.get_bookings(phone_number=MEMBER)
        assert booking.status == BookingStatus.SCHEDULED
        assert [m["to"] for m in sent] == ["111"]
        assert "still on TELEGRAM_ALLOWED_USER_IDS" in sent[0]["message"]


class TestWebhookRoute:
    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from fastapi import FastAPI

        from app.api import webhooks

        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        app = FastAPI()
        app.include_router(webhooks.router)
        return TestClient(app)

    @staticmethod
    def _message(user_id: int = 555) -> dict:
        return {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "from": {"id": user_id, "is_bot": False, "first_name": "Sam"},
                "chat": {"id": user_id, "type": "private"},
                "text": "book 9/5 at 9a",
            },
        }

    def test_group_member_dispatched(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api import webhooks

        _patch_lookup(monkeypatch, {"status": "member"})
        seen: list[str] = []

        async def fake_handle(phone_number, message, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(phone_number)
            return "ok"

        async def fake_send(*args, **kwargs):  # type: ignore[no-untyped-def]
            return "msg-1"

        monkeypatch.setattr(webhooks.booking_service, "handle_incoming_message", fake_handle)
        monkeypatch.setattr(webhooks.sms_service, "send_sms", fake_send)

        resp = client.post(
            "/webhooks/telegram",
            json=self._message(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

        assert resp.json() == {"status": "ok"}
        assert seen == [MEMBER]

    def test_non_member_ignored(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.api import webhooks

        _patch_lookup(monkeypatch, {"status": "left"})

        async def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("should not dispatch a non-member's message")

        monkeypatch.setattr(webhooks.booking_service, "handle_incoming_message", fail)

        resp = client.post(
            "/webhooks/telegram",
            json=self._message(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert resp.json() == {"status": "ignored"}

    def test_chat_member_update_handled(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = _sent(monkeypatch)
        _patch_lookup(monkeypatch, {"status": "member"})
        resp = client.post(
            "/webhooks/telegram",
            json={"update_id": 3, "chat_member": _chat_member_update("left", "member")},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert resp.json() == {"status": "ok"}
        assert len(sent) == 1

    def test_chat_member_failure_answers_500_so_telegram_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An acknowledged failure would leave a departed member's bookings live."""
        from fastapi import FastAPI

        from app.api import webhooks

        async def boom(update):  # type: ignore[no-untyped-def]
            raise RuntimeError("database down")

        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        monkeypatch.setattr(webhooks, "handle_chat_member_update", boom)
        app = FastAPI()
        app.include_router(webhooks.router)
        resp = TestClient(app, raise_server_exceptions=False).post(
            "/webhooks/telegram",
            json={"update_id": 3, "chat_member": _chat_member_update("member", "left")},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert resp.status_code == 500

    def test_chat_member_update_needs_the_secret(self, client: TestClient) -> None:
        resp = client.post(
            "/webhooks/telegram",
            json={"update_id": 3, "chat_member": _chat_member_update("member", "left")},
        )
        assert resp.status_code == 403


class TestRegisterWebhook:
    async def test_requests_chat_member_updates_when_group_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
        monkeypatch.setattr(settings, "telegram_webhook_base_url", "https://teetime.example.com")
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": True})

        provider = TelegramProvider(transport=httpx.MockTransport(handler))
        assert await provider.register_webhook() is True
        assert bodies[0]["allowed_updates"] == ["message", "chat_member"]
