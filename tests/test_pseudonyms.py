"""Tests for member pseudonyms in logs and reports (issue #256)."""

import logging

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import log_safety
from app.config import settings
from app.log_safety import MemberPseudonyms, set_member_pseudonyms
from app.models.database import Base
from app.services import pseudonyms
from app.services.pseudonyms import PseudonymError, assign, next_label, set_label

MEMBER_ID = "5550001234"
OTHER_ID = "7770009876"


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.pseudonyms.AsyncSessionLocal", session_local)
    monkeypatch.setattr(settings, "member_pseudonyms_reserved", "")
    yield
    set_member_pseudonyms({})
    await engine.dispose()


class TestNextLabel:
    def test_starts_at_a(self) -> None:
        assert next_label(set()) == "Member A"

    def test_skips_taken(self) -> None:
        assert next_label({"Member A", "Member B", "Member D"}) == "Member C"

    def test_after_z_comes_aa(self) -> None:
        taken = {f"Member {chr(c)}" for c in range(ord("A"), ord("Z") + 1)}
        assert next_label(taken) == "Member AA"


class TestAssign:
    async def test_first_member_gets_the_first_free_label(self, db: None) -> None:
        assert await assign(MEMBER_ID) == ("Member A", True)

    async def test_hand_assigned_labels_are_never_handed_out(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "member_pseudonyms_reserved", "Member A, Member B")
        assert await assign(MEMBER_ID) == ("Member C", True)

    async def test_a_member_keeps_their_label(self, db: None) -> None:
        await assign(MEMBER_ID)
        assert await assign(MEMBER_ID) == ("Member A", False)

    async def test_the_next_member_gets_the_next_label(self, db: None) -> None:
        await assign(MEMBER_ID)
        assert await assign(OTHER_ID) == ("Member B", True)

    async def test_assigning_updates_the_log_filter(
        self, db: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        await assign(MEMBER_ID)
        record = logging.LogRecord("t", logging.INFO, "", 0, "booking for %s", (MEMBER_ID,), None)
        log_safety._MEMBER_PSEUDONYMS.filter(record)
        assert record.getMessage() == "booking for <Member A>"


class TestSetLabel:
    async def test_maps_an_existing_member(self, db: None) -> None:
        await set_label(MEMBER_ID, "Member B")
        assert await pseudonyms.label_for(MEMBER_ID) == "Member B"

    async def test_refuses_a_label_held_by_someone_else(self, db: None) -> None:
        await set_label(MEMBER_ID, "Member A")
        with pytest.raises(PseudonymError):
            await set_label(OTHER_ID, "Member A")

    @pytest.mark.parametrize("label", ["Rival 1", "member a", "Member", "Alex"])
    async def test_refuses_a_malformed_label(self, db: None, label: str) -> None:
        with pytest.raises(PseudonymError):
            await set_label(MEMBER_ID, label)

    async def test_mapped_labels_are_skipped_by_assign(self, db: None) -> None:
        await set_label(OTHER_ID, "Member A")
        assert await assign(MEMBER_ID) == ("Member B", True)


class TestLogFilter:
    @staticmethod
    def render(filt: MemberPseudonyms, msg: str, *args: object) -> str:
        record = logging.LogRecord("t", logging.INFO, "", 0, msg, args, None)
        filt.filter(record)
        return record.getMessage()

    def test_rewrites_ids_in_message_and_args(self) -> None:
        filt = MemberPseudonyms()
        filt.set({MEMBER_ID: "Member C"})
        assert self.render(filt, "Telegram user %s joined", MEMBER_ID) == (
            "Telegram user <Member C> joined"
        )
        assert self.render(filt, f"claim for {MEMBER_ID} on 10/10") == (
            "claim for <Member C> on 10/10"
        )

    def test_only_whole_numbers_are_rewritten(self) -> None:
        filt = MemberPseudonyms()
        filt.set({MEMBER_ID: "Member C"})
        assert self.render(filt, f"ts=1{MEMBER_ID}9") == f"ts=1{MEMBER_ID}9"

    def test_short_numbers_are_never_treated_as_ids(self) -> None:
        filt = MemberPseudonyms()
        filt.set({"123": "Member C"})
        assert self.render(filt, "took 123ms") == "took 123ms"

    def test_unknown_ids_pass_through(self) -> None:
        filt = MemberPseudonyms()
        filt.set({MEMBER_ID: "Member C"})
        assert self.render(filt, f"unauthorized user {OTHER_ID}") == (
            f"unauthorized user {OTHER_ID}"
        )

    def test_installed_on_the_root_handlers(self) -> None:
        root = logging.getLogger()
        handler = logging.StreamHandler()
        root.addHandler(handler)
        try:
            log_safety.silence_wire_loggers()
            assert log_safety._MEMBER_PSEUDONYMS in handler.filters
        finally:
            root.removeHandler(handler)


class TestSetupFormAssignsALabel:
    async def test_admin_is_told_the_new_label(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import member_setup

        sent: list[tuple[str, str]] = []

        async def fake_send(to_number, message, **kwargs):  # type: ignore[no-untyped-def]
            sent.append((to_number, message))
            return "msg-1"

        monkeypatch.setattr(member_setup.sms_service, "send_sms", fake_send)
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        monkeypatch.setattr(settings, "member_pseudonyms_reserved", "Member A,Member B")
        user = {"id": int(MEMBER_ID), "first_name": "Sam", "username": "sam_golf"}

        await member_setup._assign_pseudonym(MEMBER_ID, user)
        await member_setup._assign_pseudonym(MEMBER_ID, user)  # re-entering a login

        assert len(sent) == 1
        to, message = sent[0]
        assert to == "111"
        assert "Sam (@sam_golf)" in message and "Member C" in message
        assert MEMBER_ID not in message
