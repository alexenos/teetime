"""Tests for member pseudonyms in logs and reports (issue #256)."""

import logging
from datetime import date, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import log_safety
from app.config import settings
from app.log_safety import MemberPseudonyms, clear_member_pseudonyms
from app.models.database import Base, BookingRecord, MemberPseudonymRecord, SessionRecord
from app.models.schemas import BookingStatus
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
    clear_member_pseudonyms()
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

    async def test_an_assigned_label_is_never_changed(self, db: None) -> None:
        """Changing it would free the old label for someone else."""
        await set_label(MEMBER_ID, "Member C")
        with pytest.raises(PseudonymError, match="already Member C"):
            await set_label(MEMBER_ID, "Member A")
        assert await pseudonyms.label_for(MEMBER_ID) == "Member C"
        assert await assign(OTHER_ID) == ("Member A", True)  # C stays taken

    async def test_setting_the_same_label_again_is_harmless(self, db: None) -> None:
        await set_label(MEMBER_ID, "Member C")
        await set_label(MEMBER_ID, "Member C")
        assert await pseudonyms.label_for(MEMBER_ID) == "Member C"

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

        await member_setup._assign_pseudonym(MEMBER_ID, user, had_login=False)
        await member_setup._assign_pseudonym(MEMBER_ID, user, had_login=True)  # re-entering

        assert len(sent) == 1
        to, message = sent[0]
        assert to == "111"
        assert "Sam (@sam_golf)" in message and "Member C" in message
        assert MEMBER_ID not in message

    async def test_a_failed_confirmation_still_assigns_the_pseudonym(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each post-save step has its own guard; one failing skips none of the others."""
        from app.providers.walden_http_login import LoginCheck, LoginOutcome
        from app.services import member_setup

        async def send_fails(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("telegram down")

        async def accepted(login: str, password: str) -> LoginCheck:
            return LoginCheck(LoginOutcome.ACCEPTED, "scripted")

        async def saved(*args, **kwargs):  # type: ignore[no-untyped-def]
            return None

        async def no_ceiling() -> None:
            return None

        monkeypatch.setattr(member_setup.sms_service, "send_sms", send_fails)
        monkeypatch.setattr(member_setup, "check_login", accepted)
        monkeypatch.setattr(member_setup.credential_service, "set_credentials", saved)
        monkeypatch.setattr(member_setup.credential_service, "get_owner", saved)  # first-time
        monkeypatch.setattr(member_setup, "_warn_if_at_racer_ceiling", no_ceiling)
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", MEMBER_ID)
        monkeypatch.setattr(settings, "telegram_members_chat_id", "")
        member_setup.clear_rate_limits()
        user = {"id": int(MEMBER_ID), "first_name": "Sam"}

        result = await member_setup.submit_login(user, "M-1", "pw", True)

        assert result.status is member_setup.SubmitStatus.SAVED
        assert await pseudonyms.label_for(MEMBER_ID) == "Member A"

    async def test_a_first_time_member_s_id_never_reaches_the_log(
        self, db: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines written before the label exists must not carry the ID."""
        from app.providers.walden_http_login import LoginCheck, LoginOutcome
        from app.services import member_setup

        async def accepted(login: str, password: str) -> LoginCheck:
            return LoginCheck(LoginOutcome.ACCEPTED, "scripted")

        async def quiet(*args, **kwargs):  # type: ignore[no-untyped-def]
            return None

        monkeypatch.setattr(member_setup, "check_login", accepted)
        monkeypatch.setattr(member_setup.credential_service, "set_credentials", quiet)
        monkeypatch.setattr(member_setup.credential_service, "get_owner", quiet)  # first-time
        monkeypatch.setattr(member_setup.sms_service, "send_sms", quiet)
        monkeypatch.setattr(member_setup, "_warn_if_at_racer_ceiling", quiet)
        monkeypatch.setattr(settings, "telegram_allowed_user_ids", MEMBER_ID)
        monkeypatch.setattr(settings, "telegram_members_chat_id", "")
        member_setup.clear_rate_limits()
        caplog.set_level(logging.DEBUG, logger="app")

        await member_setup.submit_login({"id": int(MEMBER_ID)}, "M-1", "pw", True)

        # The app's own lines. (The test database driver, aiosqlite, logs SQL
        # parameters at DEBUG; production uses asyncpg on Postgres.)
        app_lines = [r.getMessage() for r in caplog.records if r.name.startswith("app")]
        assert app_lines
        assert not any(MEMBER_ID in line for line in app_lines)

    async def test_an_existing_member_is_not_labelled_automatically(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """They may already be "Member A" in the registry; a second label is permanent."""
        from app.services import member_setup

        sent: list[tuple[str, str]] = []

        async def fake_send(to_number, message, **kwargs):  # type: ignore[no-untyped-def]
            sent.append((to_number, message))
            return "msg-1"

        monkeypatch.setattr(member_setup.sms_service, "send_sms", fake_send)
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        user = {"id": int(MEMBER_ID), "first_name": "Sam"}

        await member_setup._assign_pseudonym(MEMBER_ID, user, had_login=True)

        assert await pseudonyms.label_for(MEMBER_ID) is None
        assert len(sent) == 1 and "set-pseudonym" in sent[0][1]

    async def test_an_existing_member_already_mapped_gets_no_message(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import member_setup

        sent: list[str] = []

        async def fake_send(to_number, message, **kwargs):  # type: ignore[no-untyped-def]
            sent.append(message)
            return "msg-1"

        monkeypatch.setattr(member_setup.sms_service, "send_sms", fake_send)
        monkeypatch.setattr(settings, "telegram_admin_user_id", "111")
        await set_label(MEMBER_ID, "Member A")

        await member_setup._assign_pseudonym(MEMBER_ID, {"id": int(MEMBER_ID)}, had_login=True)

        assert sent == []
        assert await pseudonyms.label_for(MEMBER_ID) == "Member A"


class TestReservedLabelSetting:
    def test_well_formed_labels_accepted(self) -> None:
        from app.config import Settings

        assert Settings(
            member_pseudonyms_reserved="Member A, Member B"
        ).reserved_member_pseudonyms() == {
            "Member A",
            "Member B",
        }

    @pytest.mark.parametrize("value", ["Member a", "member A", "Rival 1", "Member A,Alex"])
    def test_malformed_labels_rejected(self, value: str) -> None:
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError, match="MEMBER_PSEUDONYMS_RESERVED"):
            Settings(member_pseudonyms_reserved=value)


class TestFilterOnlyGrows:
    def test_an_older_snapshot_cannot_drop_a_label(self) -> None:
        """Overlapping refreshes: the stale one must not remove a newer member."""
        filt = MemberPseudonyms()
        filt.set({MEMBER_ID: "Member C"})
        filt.set({})  # an older database snapshot, from before the insert
        record = logging.LogRecord("t", logging.INFO, "", 0, "user %s", (MEMBER_ID,), None)
        filt.filter(record)
        assert record.getMessage() == "user <Member C>"

    def test_later_mappings_merge_in(self) -> None:
        filt = MemberPseudonyms()
        filt.set({MEMBER_ID: "Member C"})
        filt.set({OTHER_ID: "Member D"})
        record = logging.LogRecord("t", logging.INFO, "", 0, f"{MEMBER_ID} {OTHER_ID}", None, None)
        filt.filter(record)
        assert record.getMessage() == "<Member C> <Member D>"


class TestForget:
    """Issue #276: /forget keeps a member's booking history under their label only."""

    TODAY = date(2026, 10, 25)
    GROUP_CHAT = "-1001234567890"

    @pytest_asyncio.fixture
    async def session_local(self, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.pseudonyms.AsyncSessionLocal", session_local)
        monkeypatch.setattr(settings, "member_pseudonyms_reserved", "")
        yield session_local
        clear_member_pseudonyms()
        await engine.dispose()

    @staticmethod
    def _booking(
        booking_id: str,
        status: BookingStatus,
        played: date,
        requester: str = MEMBER_ID,
        chat: str | None = MEMBER_ID,
    ) -> BookingRecord:
        return BookingRecord(
            booking_id=booking_id,
            phone_number=requester,
            requested_date=played,
            requested_time=time(8, 0),
            status=status,
            origin_channel_id=chat,
            requester_handle="@someone ",
            error_message="free text that might hold anything",
        )

    async def _add(self, session_local, *rows) -> None:  # type: ignore[no-untyped-def]
        async with session_local() as s:
            s.add_all(rows)
            await s.commit()

    async def _bookings(self, session_local) -> dict[str, BookingRecord]:  # type: ignore[no-untyped-def]
        async with session_local() as s:
            return {
                str(b.booking_id): b for b in (await s.execute(select(BookingRecord))).scalars()
            }

    async def _pseudonym_ids(self, session_local) -> set[str]:  # type: ignore[no-untyped-def]
        async with session_local() as s:
            return {
                str(r)
                for r in (await s.execute(select(MemberPseudonymRecord.requester_id))).scalars()
            }

    async def test_finished_bookings_are_rewritten_and_the_rest_wait(self, session_local) -> None:  # type: ignore[no-untyped-def]
        yesterday, tomorrow = self.TODAY - timedelta(days=1), self.TODAY + timedelta(days=1)
        await assign(MEMBER_ID)
        await self._add(
            session_local,
            self._booking("played", BookingStatus.SUCCESS, yesterday),
            self._booking("failed", BookingStatus.FAILED, tomorrow, chat=self.GROUP_CHAT),
            self._booking("cancelled", BookingStatus.CANCELLED, tomorrow),
            self._booking("coming", BookingStatus.SUCCESS, tomorrow),
            self._booking("racing", BookingStatus.IN_PROGRESS, self.TODAY),
            self._booking("other", BookingStatus.SUCCESS, yesterday, requester=OTHER_ID),
            SessionRecord(phone_number=MEMBER_ID),
        )

        outcome = await pseudonyms.forget(MEMBER_ID, today=self.TODAY)

        assert outcome.label == "Member A"
        assert outcome.bookings_rewritten == 3
        rows = await self._bookings(session_local)
        for name in ("played", "failed", "cancelled"):
            assert rows[name].phone_number == "Member A"
            assert rows[name].requester_handle is None
            assert rows[name].error_message is None
        assert rows["played"].origin_channel_id is None  # the private chat is the member
        assert rows["failed"].origin_channel_id == self.GROUP_CHAT  # a group is nobody
        for name in ("coming", "racing"):
            assert rows[name].phone_number == MEMBER_ID  # the racer and messages still need it
        assert rows["other"].phone_number == OTHER_ID
        async with session_local() as s:
            assert (await s.execute(select(SessionRecord))).scalars().first() is None
            record = (await s.execute(select(MemberPseudonymRecord))).scalars().one()
        assert record.forgotten_at is not None
        assert record.requester_id == MEMBER_ID  # broken only once nothing needs it

    async def test_a_member_with_nothing_on_file_is_given_no_label(self, session_local) -> None:  # type: ignore[no-untyped-def]
        outcome = await pseudonyms.forget(MEMBER_ID, today=self.TODAY)
        assert outcome.label is None
        assert await self._pseudonym_ids(session_local) == set()

    async def test_a_member_without_a_label_gets_one_for_their_history(self, session_local) -> None:  # type: ignore[no-untyped-def]
        await self._add(session_local, self._booking("old", BookingStatus.FAILED, self.TODAY))
        outcome = await pseudonyms.forget(MEMBER_ID, today=self.TODAY)
        assert outcome.label == "Member A"
        assert (await self._bookings(session_local))["old"].phone_number == "Member A"

    async def test_the_sweep_finishes_the_job_and_removes_the_id(self, session_local) -> None:  # type: ignore[no-untyped-def]
        """The issue's done-when: no requester ID, handle or private chat ID remains."""
        tomorrow = self.TODAY + timedelta(days=1)
        await assign(MEMBER_ID)
        await self._add(session_local, self._booking("coming", BookingStatus.SUCCESS, tomorrow))
        await pseudonyms.forget(MEMBER_ID, today=self.TODAY)

        not_yet = await pseudonyms.sweep_forgotten(today=self.TODAY)
        assert (not_yet.bookings_rewritten, not_yet.links_released) == (0, 0)
        assert MEMBER_ID in await self._pseudonym_ids(session_local)

        await self._add(session_local, SessionRecord(phone_number=MEMBER_ID))
        day_after = tomorrow + timedelta(days=1)
        dry = await pseudonyms.sweep_forgotten(dry_run=True, today=day_after)
        assert (dry.bookings_rewritten, dry.links_released) == (1, 1)
        assert (await self._bookings(session_local))["coming"].phone_number == MEMBER_ID

        done = await pseudonyms.sweep_forgotten(today=day_after)
        assert (done.bookings_rewritten, done.links_released) == (1, 1)
        row = (await self._bookings(session_local))["coming"]
        assert (row.phone_number, row.requester_handle, row.origin_channel_id) == (
            "Member A",
            None,
            None,
        )
        assert await self._pseudonym_ids(session_local) == {"forgotten-1"}
        async with session_local() as s:
            assert (await s.execute(select(SessionRecord))).scalars().first() is None

        # The label stays taken, and the member would come back as someone new.
        assert await assign(MEMBER_ID) == ("Member B", True)

    async def test_rejoining_before_the_sweep_stops_the_rewriting(self, session_local) -> None:  # type: ignore[no-untyped-def]
        tomorrow = self.TODAY + timedelta(days=1)
        await assign(MEMBER_ID)
        await self._add(session_local, self._booking("coming", BookingStatus.SUCCESS, tomorrow))
        await pseudonyms.forget(MEMBER_ID, today=self.TODAY)

        assert await assign(MEMBER_ID) == ("Member A", False)

        swept = await pseudonyms.sweep_forgotten(today=tomorrow + timedelta(days=1))
        assert (swept.bookings_rewritten, swept.links_released) == (0, 0)
        assert (await self._bookings(session_local))["coming"].phone_number == MEMBER_ID
