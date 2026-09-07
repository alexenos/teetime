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
from app.services.credential_service import CredentialService, WaldenCredentials

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


class TestCredentialServiceResolve:
    @pytest.mark.asyncio
    async def test_resolve_falls_back_to_global_default(
        self, credential_service: CredentialService, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "app.services.credential_service.settings.walden_member_number", "GLOBAL"
        )
        monkeypatch.setattr("app.services.credential_service.settings.walden_password", "globalpw")

        result = await credential_service.resolve("+15551234567")

        assert result == WaldenCredentials(member_number="GLOBAL", password="globalpw")

    @pytest.mark.asyncio
    async def test_resolve_returns_none_when_nothing_configured(
        self, credential_service: CredentialService, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.services.credential_service.settings.walden_member_number", "")
        monkeypatch.setattr("app.services.credential_service.settings.walden_password", "")

        result = await credential_service.resolve("+15551234567")

        assert result is None

    @pytest.mark.asyncio
    async def test_dedicated_credential_takes_precedence(
        self, credential_service: CredentialService, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "app.services.credential_service.settings.walden_member_number", "GLOBAL"
        )
        monkeypatch.setattr("app.services.credential_service.settings.walden_password", "globalpw")

        await credential_service.set_credentials(
            "+15551234567", "friend_member", "friend_pw", label="Alex"
        )

        resolved = await credential_service.resolve("+15551234567")
        dedicated = await credential_service.get_dedicated_credentials("+15551234567")

        assert resolved == WaldenCredentials(member_number="friend_member", password="friend_pw")
        assert dedicated == resolved

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
