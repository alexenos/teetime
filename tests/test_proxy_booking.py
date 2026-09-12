"""
Tests for admin proxy booking (issue #185).

One designated Telegram ID books *as* a friend: the record carries the
friend's identity, runs under the friend's Walden membership, and reports back
to the friend's own conversation, while the admin sees only the immediate
acknowledgement in their own chat.

Two invariants get the most attention here, because both failure modes are
expensive and silent:

  * an unresolved or ambiguous "for @X" must never fall back to the shared
    global account (a round booked under the wrong membership, and with the
    club's one-per-member-per-day rule, possibly the slot the real booking
    needed), and
  * the admin identity itself must never resolve to a credential at all.
"""

from datetime import date, time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.models.database import Base
from app.models.schemas import ConversationState, ParsedIntent, TeeTimeRequest, UserSession
from app.services import proxy_booking
from app.services.booking_service import BookingService
from app.services.credential_service import (
    CredentialOwner,
    CredentialService,
    ProxyAdminHasNoCredentialError,
    WaldenCredentialRequiredError,
    WaldenCredentials,
)

TEST_KEY = Fernet.generate_key().decode()
ADMIN_ID = "100000001"
ALEX_ID = "200000002"
SAM_ID = "300000003"


@pytest.fixture
def admin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ADMIN_ID the single configured proxy admin."""
    monkeypatch.setattr(settings, "telegram_admin_user_id", ADMIN_ID)


@pytest_asyncio.fixture
async def test_session_local():  # type: ignore[no-untyped-def]
    """An in-memory database with the app's schema, as test_credential_service does."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def credential_service(test_session_local, monkeypatch) -> CredentialService:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.services.credential_service.AsyncSessionLocal", test_session_local)
    monkeypatch.setattr("app.config.settings.credential_encryption_key", TEST_KEY)
    return CredentialService()


class TestSplitProxyTarget:
    """The "for @X" grammar, which decides whose membership a booking uses."""

    @pytest.mark.parametrize(
        "message,target,rest",
        [
            ("for @alex book 9/12 at 8a", "alex", "book 9/12 at 8a"),
            ("For @Alex, book 9/12 at 8a", "Alex", "book 9/12 at 8a"),
            ("for @alex: book 9/12 at 8a", "alex", "book 9/12 at 8a"),
            ("  for @alex   book 9/12 at 8a", "alex", "book 9/12 at 8a"),
            ("FOR @ALEX book 9/12", "ALEX", "book 9/12"),
            ("for @alex", "alex", ""),
        ],
    )
    def test_leading_clause_is_peeled_off(self, message: str, target: str, rest: str) -> None:
        assert proxy_booking.split_proxy_target(message) == (target, rest)

    @pytest.mark.parametrize(
        "message",
        [
            # Anchoring handles the common shape...
            "book 9/12 at 8a for 4 players",
            "book a tee time for tomorrow",
            "yes",
            "cancel my booking",
            "",
            # ...but not this one, which is why the "@" is required. With it
            # optional this read as a target named "4", and because an
            # unresolved target returns before the message is ever parsed, the
            # booking text was discarded outright. Found in review of #187.
            "for 4 players, book 9/12 at 8a",
            "for 4 players book 9/12 at 8a",
            # A name with no sigil is not lost either - it reaches the parser,
            # which reads the booking and then asks who it is for.
            "for alex book 9/12 at 8a",
            "For Alex, book 9/12 at 8a",
        ],
    )
    def test_ordinary_messages_are_untouched(self, message: str) -> None:
        assert proxy_booking.split_proxy_target(message) == (None, message)

    def test_bare_for_is_not_a_clause(self) -> None:
        """ "for" with nothing usable after it stays an ordinary word."""
        assert proxy_booking.split_proxy_target("for @") == (None, "for @")

    def test_a_name_without_a_sigil_keeps_its_booking(self) -> None:
        """The cost of requiring "@" is a turn, never the request itself."""
        assert proxy_booking.split_proxy_target("for alex book 9/12 at 8a") == (
            None,
            "for alex book 9/12 at 8a",
        )

    def test_multiline_request_keeps_its_lines(self) -> None:
        """A multi-booking message is one request per line; the split must not fold them."""
        target, rest = proxy_booking.split_proxy_target("for @alex book 9/12 8a\nand 9/13 8a")
        assert target == "alex"
        assert rest == "book 9/12 8a\nand 9/13 8a"


class TestNormalizeTarget:
    @pytest.mark.parametrize("raw", ["alex", "@alex", "ALEX", "@Alex", "  @alex  "])
    def test_folds_to_one_form(self, raw: str) -> None:
        assert proxy_booking.normalize_target(raw) == "alex"

    def test_empty_stays_empty(self) -> None:
        assert proxy_booking.normalize_target("@") == ""


class TestStripLeadingFor:
    """Answering "for which user?" with "For Ronald" names Ronald, not "For Ronald"."""

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("For Ronald", "Ronald"),
            ("for ronald", "ronald"),
            ("FOR @ronald", "@ronald"),
            ("  for   Ronald  ", "Ronald"),
            ("for Ron Garner", "Ron Garner"),
        ],
    )
    def test_a_restated_preposition_is_dropped(self, reply: str, expected: str) -> None:
        assert proxy_booking.strip_leading_for(reply) == expected

    @pytest.mark.parametrize("reply", ["Ronald", "@ronald", "Ron Garner"])
    def test_a_bare_name_is_untouched(self, reply: str) -> None:
        assert proxy_booking.strip_leading_for(reply) == reply

    @pytest.mark.parametrize("reply", ["for", "  for  ", ""])
    def test_nothing_after_for_is_left_alone(self, reply: str) -> None:
        """Stripping to the empty string would search for nobody in particular."""
        assert proxy_booking.strip_leading_for(reply) == reply

    def test_only_a_leading_for_counts(self) -> None:
        """ "Fortnight" starts with the letters but is not the preposition."""
        assert proxy_booking.strip_leading_for("Fortnight") == "Fortnight"


class TestIsProxyAdmin:
    def test_matches_only_the_configured_id(self, admin_configured: None) -> None:
        assert proxy_booking.is_proxy_admin(ADMIN_ID) is True
        assert proxy_booking.is_proxy_admin(ALEX_ID) is False

    def test_unset_means_nobody(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fails closed, like the allowlist - not "everyone is admin"."""
        monkeypatch.setattr(settings, "telegram_admin_user_id", "")
        assert proxy_booking.is_proxy_admin(ADMIN_ID) is False
        assert proxy_booking.is_proxy_admin("") is False


class TestAdminSetting:
    def test_username_rejected_at_load_time(self) -> None:
        with pytest.raises(ValueError, match="numeric Telegram user ID"):
            Settings(telegram_admin_user_id="@dax")

    def test_multiple_ids_rejected(self) -> None:
        """Exactly one admin, not a list - a comma is the likely mistake."""
        with pytest.raises(ValueError, match="numeric Telegram user ID"):
            Settings(telegram_admin_user_id="111,222")

    def test_unset_is_allowed(self) -> None:
        assert Settings(telegram_admin_user_id="").telegram_admin_id() is None

    def test_admin_missing_from_allowlist_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """The allowlist is checked first, so an unlisted admin is simply mute."""
        with caplog.at_level("WARNING"):
            Settings(telegram_admin_user_id="111", telegram_allowed_user_ids="222")
        assert "not in TELEGRAM_ALLOWED_USER_IDS" in caplog.text

    def test_allowlisted_admin_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            Settings(telegram_admin_user_id="111", telegram_allowed_user_ids="111,222")
        assert "not in TELEGRAM_ALLOWED_USER_IDS" not in caplog.text


class TestCredentialLookupByName:
    @pytest.mark.asyncio
    async def test_matches_name_case_insensitively(
        self, credential_service: CredentialService
    ) -> None:
        await credential_service.set_credentials(ALEX_ID, "m", "pw", name="Alex")

        for typed in ("Alex", "alex", "@alex", "  ALEX "):
            matches = await credential_service.find_by_name_or_telegram_username(typed)
            assert [owner.phone_number for owner in matches] == [ALEX_ID]

    @pytest.mark.asyncio
    async def test_matches_telegram_username(self, credential_service: CredentialService) -> None:
        await credential_service.set_credentials(ALEX_ID, "m", "pw", telegram_username="@alexenos")

        matches = await credential_service.find_by_name_or_telegram_username("alexenos")

        assert [owner.phone_number for owner in matches] == [ALEX_ID]
        # Stored without the "@" however the admin typed it going in.
        assert matches[0].telegram_username == "alexenos"

    @pytest.mark.asyncio
    async def test_unknown_target_matches_nothing(
        self, credential_service: CredentialService
    ) -> None:
        await credential_service.set_credentials(ALEX_ID, "m", "pw", name="Alex")

        assert await credential_service.find_by_name_or_telegram_username("nobody") == []

    @pytest.mark.asyncio
    async def test_collision_returns_every_match(
        self, credential_service: CredentialService
    ) -> None:
        """A name colliding with someone else's handle must not silently pick one."""
        await credential_service.set_credentials(ALEX_ID, "m", "pw", name="Sam")
        await credential_service.set_credentials(SAM_ID, "m", "pw", telegram_username="sam")

        matches = await credential_service.find_by_name_or_telegram_username("sam")

        assert {owner.phone_number for owner in matches} == {ALEX_ID, SAM_ID}

    @pytest.mark.asyncio
    async def test_label_does_not_resolve(self, credential_service: CredentialService) -> None:
        """label stayed a free-text note; only name/telegram_username resolve."""
        await credential_service.set_credentials(ALEX_ID, "m", "pw", label="Alex")

        assert await credential_service.find_by_name_or_telegram_username("Alex") == []

    @pytest.mark.asyncio
    async def test_updating_a_password_keeps_the_name(
        self, credential_service: CredentialService
    ) -> None:
        """Rotating a login must not quietly make someone unaddressable."""
        await credential_service.set_credentials(ALEX_ID, "m", "pw", name="Alex")
        await credential_service.set_credentials(ALEX_ID, "m2", "pw2")

        matches = await credential_service.find_by_name_or_telegram_username("Alex")
        assert [owner.phone_number for owner in matches] == [ALEX_ID]

    @pytest.mark.asyncio
    async def test_get_owner_returns_naming_fields(
        self, credential_service: CredentialService
    ) -> None:
        await credential_service.set_credentials(
            ALEX_ID, "m", "pw", name="Alex", telegram_username="alexenos"
        )

        owner = await credential_service.get_owner(ALEX_ID)

        assert owner == CredentialOwner(
            phone_number=ALEX_ID, name="Alex", telegram_username="alexenos"
        )
        assert owner.display_name == "Alex"

    def test_display_name_falls_back_to_handle_then_id(self) -> None:
        assert CredentialOwner(ALEX_ID, None, "alexenos").display_name == "@alexenos"
        assert CredentialOwner(ALEX_ID, None, None).display_name == ALEX_ID


class TestAdminHasNoCredentialOfItsOwn:
    @pytest.mark.asyncio
    async def test_lookup_raises_for_the_admin_identity(
        self, credential_service: CredentialService, admin_configured: None
    ) -> None:
        """Returning None would mean "use the shared account" - the exact fallback
        the admin account exists to avoid."""
        with pytest.raises(ProxyAdminHasNoCredentialError):
            await credential_service.get_dedicated_credentials(ADMIN_ID)

    @pytest.mark.asyncio
    async def test_require_credentials_raises_for_the_admin(
        self, credential_service: CredentialService, admin_configured: None
    ) -> None:
        with pytest.raises(ProxyAdminHasNoCredentialError):
            await credential_service.require_credentials(ADMIN_ID)

    @pytest.mark.asyncio
    async def test_nobody_falls_back_to_a_shared_account(
        self, credential_service: CredentialService, admin_configured: None
    ) -> None:
        """Replaces test_other_requesters_still_fall_back.

        That test asserted an ordinary requester with no row of their own
        resolved to the global WALDEN_MEMBER_NUMBER. That was #179's migration
        aid and is exactly what this change removes: booking under a login that
        is not the requester's own is the failure being prevented, so the
        refusal now applies to everyone, not only the admin.
        """
        with pytest.raises(WaldenCredentialRequiredError):
            await credential_service.require_credentials(ALEX_ID)


@pytest.fixture(autouse=True)
def requester_has_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the flow tests below off a real database, with a login on file.

    Returned None before; a requester with no credential is now refused rather
    than falling back to the shared account, which would stop these tests at
    create_booking for a reason none of them is about.
    """

    async def _credential(phone_number: str) -> WaldenCredentials:
        return WaldenCredentials(member_number=f"member-{phone_number}", password="pw")

    monkeypatch.setattr(
        "app.services.booking_service.credential_service.get_dedicated_credentials",
        _credential,
    )


class _FakeSessions:
    """A stand-in for database_service's session storage, keyed by identity."""

    def __init__(self, *sessions: UserSession) -> None:
        self._sessions = {s.phone_number: s for s in sessions}

    async def get_or_create_session(self, phone_number: str) -> UserSession:
        if phone_number not in self._sessions:
            self._sessions[phone_number] = UserSession(phone_number=phone_number)
        return self._sessions[phone_number]

    async def get_session(self, phone_number: str) -> UserSession | None:
        return self._sessions.get(phone_number)

    async def update_session(self, session: UserSession) -> UserSession:
        self._sessions[session.phone_number] = session
        return session


class TestProxyBookingFlow:
    """The admin's conversation, end to end."""

    @pytest.fixture
    def service(self) -> BookingService:
        return BookingService()

    @staticmethod
    def _booking_intent() -> ParsedIntent:
        return ParsedIntent(
            intent="book",
            raw_message="book 9/12 at 8a",
            tee_time_request=TeeTimeRequest(
                requested_date=date(2026, 9, 12), requested_time=time(8, 0), num_players=4
            ),
        )

    @staticmethod
    def _owner() -> CredentialOwner:
        return CredentialOwner(phone_number=ALEX_ID, name="Alex", telegram_username="alexenos")

    def _patched(self, sessions: _FakeSessions, matches: list[CredentialOwner]):  # type: ignore[no-untyped-def]
        """Patch the three collaborators the proxy flow reaches for."""
        creds = AsyncMock()
        creds.find_by_name_or_telegram_username = AsyncMock(return_value=matches)
        creds.get_owner = AsyncMock(return_value=matches[0] if len(matches) == 1 else None)
        return (
            patch("app.services.booking_service.database_service", sessions),
            patch("app.services.booking_service.credential_service", creds),
        )

    @pytest.mark.asyncio
    async def test_for_clause_books_as_the_friend(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """The headline case: "for @alex book ..." then "yes"."""
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram", origin_channel_id="-100")
        alex = UserSession(
            phone_number=ALEX_ID,
            channel="telegram",
            origin_channel_id="-555",
            requester_handle="@alexenos ",
        )
        sessions = _FakeSessions(admin, alex)
        db_patch, cred_patch = self._patched(sessions, [self._owner()])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                echo = await service.handle_incoming_message(
                    ADMIN_ID, "for @alex book 9/12 at 8a", channel="telegram"
                )

                # The parser never sees the addressing clause.
                assert gemini.parse_message.await_args.args[0] == "book 9/12 at 8a"

            assert "Alex" in echo
            assert admin.pending_proxy_target == ALEX_ID
            assert admin.state == ConversationState.AWAITING_CONFIRMATION

            with patch.object(service, "create_booking", AsyncMock()) as create:
                create.return_value = AsyncMock(
                    id="abc123",
                    status="scheduled",
                    request=self._booking_intent().tee_time_request,
                    actual_booked_time=None,
                    scheduled_execution_time=None,
                )
                await service.handle_incoming_message(ADMIN_ID, "yes", channel="telegram")

        # Attributed to Alex, and reported back into Alex's own conversation.
        assert create.await_args.args[0] == ALEX_ID
        assert create.await_args.args[2] == "-555"
        assert create.await_args.kwargs["requester_handle"] == "@alexenos "
        # And forgotten, so the admin's next booking isn't silently Alex's too.
        assert admin.pending_proxy_target is None

    @pytest.mark.asyncio
    async def test_failed_target_drops_an_unconfirmed_request(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Regression: a mistyped name must not resurface an earlier booking.

        Caught in review of #187. The sequence is three ordinary turns:

          1. "for @alex book 9/12 8a"  -> echoed back, never confirmed
          2. "for @nobdy book 9/20 8a" -> typo, so nothing resolves; this turn's
             own request is never parsed, because the failure returns early
          3. "@sam"                    -> resolves

        Turn 3 read the request still sitting on the session - 9/12, from turn 1
        - and offered Sam a slot nobody had asked him about, one "yes" away from
        booking it under his membership. The date the admin actually typed in
        turn 2 was never parsed at all.
        """
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)

        creds = AsyncMock()
        creds.get_owner = AsyncMock(return_value=self._owner())
        # Turn 1 resolves Alex; turn 2's typo resolves nobody.
        creds.find_by_name_or_telegram_username = AsyncMock(side_effect=[[self._owner()], []])

        with patch("app.services.booking_service.database_service", sessions):
            with patch("app.services.booking_service.credential_service", creds):
                with patch("app.services.booking_service.gemini_service") as gemini:
                    gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                    await service.handle_incoming_message(
                        ADMIN_ID, "for @alex book 9/12 at 8a", channel="telegram"
                    )

                assert admin.pending_request is not None
                assert admin.state == ConversationState.AWAITING_CONFIRMATION

                with patch("app.services.booking_service.gemini_service") as gemini:
                    gemini.parse_message = AsyncMock(side_effect=AssertionError("must not parse"))
                    response = await service.handle_incoming_message(
                        ADMIN_ID, "for @nobdy book 9/20 at 8a", channel="telegram"
                    )

        assert "don't know who" in response
        # Nothing survives the failure to be resumed under whoever is named next.
        assert admin.pending_request is None
        assert admin.pending_requests is None
        assert admin.pending_proxy_target is None
        assert admin.state == ConversationState.AWAITING_PROXY_TARGET

    @pytest.mark.asyncio
    async def test_missing_target_is_asked_for_and_held(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """The issue's second form: "book ..." -> "for which user?" -> "@alex"."""
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)
        db_patch, cred_patch = self._patched(sessions, [self._owner()])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                asked = await service.handle_incoming_message(
                    ADMIN_ID, "book 9/12 at 8a", channel="telegram"
                )

            assert "which user" in asked.lower()
            assert admin.state == ConversationState.AWAITING_PROXY_TARGET
            # The request is held, not thrown away.
            assert admin.pending_request is not None

            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(side_effect=AssertionError("must not parse"))
                echo = await service.handle_incoming_message(ADMIN_ID, "@alex", channel="telegram")

        assert "Alex" in echo
        assert admin.pending_proxy_target == ALEX_ID
        assert admin.state == ConversationState.AWAITING_CONFIRMATION
        assert admin.pending_request is not None

    @pytest.mark.asyncio
    async def test_name_without_a_sigil_degrades_to_asking(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Requiring "@" costs a turn, not the booking.

        "for alex book ..." is not a proxy clause any more, so the whole string
        reaches the parser; the booking is read normally and the admin is then
        asked who it is for. The request survives, which is the entire argument
        for the stricter grammar - the optional "@" it replaced discarded the
        booking text whenever the target failed to resolve.
        """
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)
        db_patch, cred_patch = self._patched(sessions, [self._owner()])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                asked = await service.handle_incoming_message(
                    ADMIN_ID, "for alex book 9/12 at 8a", channel="telegram"
                )

                # Handed over whole - nothing was peeled off the front.
                assert gemini.parse_message.await_args.args[0] == "for alex book 9/12 at 8a"

        assert "which user" in asked.lower()
        assert admin.state == ConversationState.AWAITING_PROXY_TARGET
        assert admin.pending_request is not None

    @pytest.mark.asyncio
    async def test_player_count_clause_still_reaches_the_parser(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Regression: "for 4 players, book ..." used to lose the booking entirely."""
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)
        db_patch, cred_patch = self._patched(sessions, [])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                asked = await service.handle_incoming_message(
                    ADMIN_ID, "for 4 players, book 9/12 at 8a", channel="telegram"
                )

                assert gemini.parse_message.await_args.args[0] == "for 4 players, book 9/12 at 8a"

        # Asked who it is for, rather than "I don't know who 4 is" with the
        # date silently dropped.
        assert "which user" in asked.lower()
        assert admin.pending_request is not None

    @pytest.mark.asyncio
    async def test_unknown_target_fails_loudly(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Never the shared account - say so and ask again."""
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)
        db_patch, cred_patch = self._patched(sessions, [])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(side_effect=AssertionError("must not parse"))
                response = await service.handle_incoming_message(
                    ADMIN_ID, "for @nobody book 9/12 at 8a", channel="telegram"
                )

        assert "don't know who" in response
        assert admin.pending_proxy_target is None
        assert admin.state == ConversationState.AWAITING_PROXY_TARGET

    @pytest.mark.asyncio
    async def test_ambiguous_target_refuses_to_guess(
        self, service: BookingService, admin_configured: None
    ) -> None:
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)
        both = [
            CredentialOwner(ALEX_ID, "Sam", None),
            CredentialOwner(SAM_ID, None, "sam"),
        ]
        db_patch, cred_patch = self._patched(sessions, both)

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(side_effect=AssertionError("must not parse"))
                response = await service.handle_incoming_message(
                    ADMIN_ID, "for @sam book 9/12 at 8a", channel="telegram"
                )

        assert "more than one person" in response
        assert "Sam" in response and "@sam" in response
        assert admin.pending_proxy_target is None

    @pytest.mark.asyncio
    async def test_answering_with_for_still_names_the_friend(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """The reported case: "for which user?" answered "For Ronald".

        The prompt asks for a name, so restating the preposition is the
        natural reply. Looked up verbatim it folded to "for ronald", matched
        nobody, and came back as `I don't know who "For Ronald" is` - which
        reads as the friend being unconfigured rather than the word "For"
        being taken as part of his name.
        """
        admin = UserSession(
            phone_number=ADMIN_ID,
            channel="telegram",
            state=ConversationState.AWAITING_PROXY_TARGET,
            pending_request=self._booking_intent().tee_time_request,
        )
        sessions = _FakeSessions(admin)
        owner = self._owner()

        # Argument-sensitive on purpose. _patched returns a match whatever it
        # is handed, so with it this test passes even with the strip removed -
        # it proves the flow runs, not that the right name was looked up.
        async def lookup(target: str) -> list[CredentialOwner]:
            wanted = proxy_booking.normalize_target(target)
            candidates = {
                proxy_booking.normalize_target(v)
                for v in (owner.name, owner.telegram_username)
                if v
            }
            return [owner] if wanted in candidates else []

        creds = AsyncMock()
        creds.find_by_name_or_telegram_username = AsyncMock(side_effect=lookup)
        creds.get_owner = AsyncMock(return_value=owner)

        with (
            patch("app.services.booking_service.database_service", sessions),
            patch("app.services.booking_service.credential_service", creds),
        ):
            await service.handle_incoming_message(ADMIN_ID, "For Alex", channel="telegram")

        assert admin.pending_proxy_target == ALEX_ID
        assert admin.state != ConversationState.AWAITING_PROXY_TARGET

    @pytest.mark.asyncio
    async def test_the_lookup_sees_the_name_without_the_preposition(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Strip before the query, not after: the stored side is never folded."""
        admin = UserSession(
            phone_number=ADMIN_ID,
            channel="telegram",
            state=ConversationState.AWAITING_PROXY_TARGET,
            pending_request=self._booking_intent().tee_time_request,
        )
        sessions = _FakeSessions(admin)
        creds = AsyncMock()
        creds.find_by_name_or_telegram_username = AsyncMock(return_value=[self._owner()])
        creds.get_owner = AsyncMock(return_value=self._owner())

        with (
            patch("app.services.booking_service.database_service", sessions),
            patch("app.services.booking_service.credential_service", creds),
        ):
            await service.handle_incoming_message(ADMIN_ID, "For Alex", channel="telegram")

        creds.find_by_name_or_telegram_username.assert_awaited_once_with("Alex")

    @pytest.mark.asyncio
    async def test_abort_leaves_no_pending_booking(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Otherwise "never mind" comes back as "I don't know who never is"."""
        admin = UserSession(
            phone_number=ADMIN_ID,
            channel="telegram",
            state=ConversationState.AWAITING_PROXY_TARGET,
            pending_request=self._booking_intent().tee_time_request,
        )
        sessions = _FakeSessions(admin)
        db_patch, cred_patch = self._patched(sessions, [])

        with db_patch, cred_patch:
            response = await service.handle_incoming_message(
                ADMIN_ID, "never mind", channel="telegram"
            )

        assert "won't book that" in response
        assert admin.state == ConversationState.IDLE
        assert admin.pending_request is None

    @pytest.mark.asyncio
    async def test_non_book_intent_for_a_friend_is_refused(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """v1 is booking only; cancelling for someone else is a follow-up."""
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram")
        sessions = _FakeSessions(admin)
        db_patch, cred_patch = self._patched(sessions, [self._owner()])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(
                    return_value=ParsedIntent(intent="cancel", raw_message="cancel")
                )
                response = await service.handle_incoming_message(
                    ADMIN_ID, "for @alex cancel", channel="telegram"
                )

        assert "only book on someone else's behalf" in response
        assert admin.pending_proxy_target is None

    @pytest.mark.asyncio
    async def test_admin_cannot_book_for_themselves(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """The guard beneath the conversation: refused even called directly."""
        with pytest.raises(ValueError, match="no Walden login of its own"):
            await service.create_booking(ADMIN_ID, self._booking_intent().tee_time_request)

    @pytest.mark.asyncio
    async def test_ordinary_user_is_untouched_by_a_for_clause(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """Only the admin ID proxies; for everyone else "for ..." is just words."""
        alex = UserSession(phone_number=ALEX_ID, channel="telegram")
        sessions = _FakeSessions(alex)
        db_patch, cred_patch = self._patched(sessions, [self._owner()])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                echo = await service.handle_incoming_message(
                    ALEX_ID, "for @sam book 9/12 at 8a", channel="telegram"
                )

                # Passed through whole - no clause peeled off for a non-admin.
                assert gemini.parse_message.await_args.args[0] == "for @sam book 9/12 at 8a"

        assert alex.pending_proxy_target is None
        assert "Alex" not in echo
        assert alex.state == ConversationState.AWAITING_CONFIRMATION

    @pytest.mark.asyncio
    async def test_friend_with_no_session_gets_a_handle_from_their_stored_username(
        self, service: BookingService, admin_configured: None
    ) -> None:
        """A friend who has never spoken in a group still gets named on the result."""
        admin = UserSession(phone_number=ADMIN_ID, channel="telegram", origin_channel_id="-100")
        sessions = _FakeSessions(admin)  # no session for Alex
        db_patch, cred_patch = self._patched(sessions, [self._owner()])

        with db_patch, cred_patch:
            with patch("app.services.booking_service.gemini_service") as gemini:
                gemini.parse_message = AsyncMock(return_value=self._booking_intent())
                await service.handle_incoming_message(
                    ADMIN_ID, "for @alex book 9/12 at 8a", channel="telegram"
                )

            with patch.object(service, "create_booking", AsyncMock()) as create:
                create.return_value = AsyncMock(
                    id="abc123",
                    status="scheduled",
                    request=self._booking_intent().tee_time_request,
                    actual_booked_time=None,
                    scheduled_execution_time=None,
                )
                await service.handle_incoming_message(ADMIN_ID, "yes", channel="telegram")

        assert create.await_args.args[0] == ALEX_ID
        # Not the admin's chat: with no session of Alex's to copy, the result
        # falls back to his private chat rather than the group Dax typed in.
        assert create.await_args.args[2] is None
        assert create.await_args.kwargs["requester_handle"] == "@alexenos "
