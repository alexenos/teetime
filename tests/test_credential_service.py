"""
Tests for the per-friend Walden credential store (issue #179).

Covers app/services/credential_crypto.py (encryption at rest) and
app/services/credential_service.py (resolution, admin writes), using an
in-memory SQLite database the same way test_database_service.py does.
"""

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.services import credential_crypto
from app.services.credential_service import (
    CredentialService,
    WaldenCredentialRequiredError,
    WaldenCredentials,
)

TEST_KEY = Fernet.generate_key().decode()


@pytest_asyncio.fixture
async def test_engine():
    """Create an in-memory SQLite engine with the app's schema for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session_local(test_engine):
    """Create a sessionmaker bound to the test engine."""
    return sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def credential_service(test_session_local, monkeypatch) -> CredentialService:
    """Create a CredentialService that uses the test database and a test encryption key."""
    monkeypatch.setattr("app.services.credential_service.AsyncSessionLocal", test_session_local)
    monkeypatch.setattr("app.config.settings.credential_encryption_key", TEST_KEY)
    return CredentialService()


class TestCredentialCrypto:
    def test_encrypt_decrypt_round_trip(self, monkeypatch) -> None:
        monkeypatch.setattr("app.config.settings.credential_encryption_key", TEST_KEY)
        ciphertext = credential_crypto.encrypt("hunter2")
        assert ciphertext != "hunter2"
        assert credential_crypto.decrypt(ciphertext) == "hunter2"

    def test_encrypt_without_key_raises(self, monkeypatch) -> None:
        monkeypatch.setattr("app.config.settings.credential_encryption_key", "")
        with pytest.raises(credential_crypto.CredentialEncryptionError):
            credential_crypto.encrypt("hunter2")

    def test_decrypt_with_wrong_key_raises(self, monkeypatch) -> None:
        monkeypatch.setattr("app.config.settings.credential_encryption_key", TEST_KEY)
        ciphertext = credential_crypto.encrypt("hunter2")

        monkeypatch.setattr(
            "app.config.settings.credential_encryption_key", Fernet.generate_key().decode()
        )
        with pytest.raises(credential_crypto.CredentialEncryptionError):
            credential_crypto.decrypt(ciphertext)

    def test_invalid_key_raises(self, monkeypatch) -> None:
        monkeypatch.setattr("app.config.settings.credential_encryption_key", "not-a-fernet-key")
        with pytest.raises(credential_crypto.CredentialEncryptionError):
            credential_crypto.encrypt("hunter2")


class TestCredentialServiceRequire:
    """require_credentials replaces #179's resolve().

    The three tests here previously asserted the shared-account fallback:
    that an unknown requester resolved to the global WALDEN_MEMBER_NUMBER,
    that resolve() returned None when even that was unset, and that a stored
    login merely took *precedence* over it. All three encoded the behaviour
    this change removes - there is no second account to rank against, so
    "precedence" no longer means anything and the only two outcomes are the
    requester's own login or a refusal.
    """

    @pytest.mark.asyncio
    async def test_unknown_requester_is_refused_not_given_the_global_account(
        self, credential_service: CredentialService, monkeypatch
    ) -> None:
        """The old test asserted this returned the global account."""
        monkeypatch.setattr("app.config.settings.walden_member_number", "GLOBAL")
        monkeypatch.setattr("app.config.settings.walden_password", "globalpw")

        with pytest.raises(WaldenCredentialRequiredError):
            await credential_service.require_credentials("+15551234567")

    @pytest.mark.asyncio
    async def test_refused_the_same_way_when_no_global_account_exists(
        self, credential_service: CredentialService, monkeypatch
    ) -> None:
        """One outcome, not two: the global settings no longer change anything."""
        monkeypatch.setattr("app.config.settings.walden_member_number", "")
        monkeypatch.setattr("app.config.settings.walden_password", "")

        with pytest.raises(WaldenCredentialRequiredError):
            await credential_service.require_credentials("+15551234567")

    @pytest.mark.asyncio
    async def test_returns_the_requesters_own_login(
        self, credential_service: CredentialService, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.config.settings.walden_member_number", "GLOBAL")
        monkeypatch.setattr("app.config.settings.walden_password", "globalpw")

        await credential_service.set_credentials(
            "+15551234567", "friend_member", "friend_pw", label="Alex"
        )

        required = await credential_service.require_credentials("+15551234567")

        assert required == WaldenCredentials(member_number="friend_member", password="friend_pw")
        assert required == await credential_service.get_dedicated_credentials("+15551234567")

    @pytest.mark.asyncio
    async def test_get_dedicated_credentials_none_for_unknown_requester(
        self, credential_service: CredentialService
    ) -> None:
        assert await credential_service.get_dedicated_credentials("+15559999999") is None

    @pytest.mark.asyncio
    async def test_set_credentials_upserts(self, credential_service: CredentialService) -> None:
        await credential_service.set_credentials("+15551234567", "member_v1", "pw_v1")
        await credential_service.set_credentials("+15551234567", "member_v2", "pw_v2")

        dedicated = await credential_service.get_dedicated_credentials("+15551234567")

        assert dedicated == WaldenCredentials(member_number="member_v2", password="pw_v2")

    @pytest.mark.asyncio
    async def test_remove_credentials(self, credential_service: CredentialService) -> None:
        await credential_service.set_credentials("+15551234567", "member", "pw")

        removed = await credential_service.remove_credentials("+15551234567")
        removed_again = await credential_service.remove_credentials("+15551234567")

        assert removed is True
        assert removed_again is False
        assert await credential_service.get_dedicated_credentials("+15551234567") is None
