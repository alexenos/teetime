"""Tests for member logins encrypted with Cloud KMS (issue #242).

KMS is played by FakeKms on an httpx.MockTransport. Like the real service, it
refuses to decrypt unless the additional authenticated data matches what the
ciphertext was written with, which is what binds a ciphertext to one member's
row and field.
"""

import base64
import json

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.database import Base, WaldenCredentialRecord
from app.services import credential_crypto
from app.services.credential_crypto import CredentialEncryptionError, decrypt, encrypt, scheme_of
from app.services.credential_service import CredentialService

KEY = "projects/p/locations/us-central1/keyRings/teetime-credentials/cryptoKeys/walden-logins"
SECRET = "unit-test-password-42"


class FakeKms:
    """Encrypt and decrypt the way KMS's REST API does, recording every call."""

    def __init__(self, fail_with: int | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_with = fail_with

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        operation = request.url.path.rsplit(":", 1)[-1]
        self.calls.append((operation, body))
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.url.path == f"/v1/{KEY}:{operation}"
        if self.fail_with:
            # A real KMS error body describes the request; it must never be surfaced.
            return httpx.Response(self.fail_with, json={"error": {"message": json.dumps(body)}})
        if operation == "encrypt":
            sealed = {"aad": body["additionalAuthenticatedData"], "p": body["plaintext"]}
            ciphertext = base64.b64encode(json.dumps(sealed).encode()).decode()
            return httpx.Response(
                200, json={"name": f"{KEY}/cryptoKeyVersions/1", "ciphertext": ciphertext}
            )
        sealed = json.loads(base64.b64decode(body["ciphertext"]))
        if sealed["aad"] != body["additionalAuthenticatedData"]:
            return httpx.Response(400, json={"error": {"message": "Decryption failed"}})
        return httpx.Response(200, json={"plaintext": sealed["p"]})


@pytest.fixture
def kms(monkeypatch: pytest.MonkeyPatch) -> FakeKms:
    fake = FakeKms()
    monkeypatch.setattr(settings, "credential_kms_key", KEY)
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(credential_crypto, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(credential_crypto, "_access_token", lambda: "test-token")
    return fake


class TestKmsScheme:
    async def test_round_trip(self, kms: FakeKms) -> None:
        stored = await encrypt(SECRET, context="555:password")
        assert stored.startswith("kms1:")
        assert scheme_of(stored) == "kms"
        assert SECRET not in stored
        assert await decrypt(stored, context="555:password") == SECRET
        assert [op for op, _ in kms.calls] == ["encrypt", "decrypt"]

    async def test_ciphertext_is_bound_to_its_row_and_field(self, kms: FakeKms) -> None:
        stored = await encrypt(SECRET, context="555:password")
        with pytest.raises(CredentialEncryptionError):
            await decrypt(stored, context="777:password")  # another member's row
        with pytest.raises(CredentialEncryptionError):
            await decrypt(stored, context="555:member_number")  # another column

    async def test_fernet_rows_still_decrypt_once_kms_is_on(
        self, kms: FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing logins keep working until their members re-enter them."""
        monkeypatch.setattr(settings, "credential_kms_key", "")
        legacy = await encrypt(SECRET, context="555:password")
        assert scheme_of(legacy) == "fernet"

        monkeypatch.setattr(settings, "credential_kms_key", KEY)
        assert await decrypt(legacy, context="555:password") == SECRET
        assert kms.calls == []

    async def test_kms_row_without_the_key_configured(
        self, kms: FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored = await encrypt(SECRET, context="555:password")
        monkeypatch.setattr(settings, "credential_kms_key", "")
        with pytest.raises(CredentialEncryptionError, match="CREDENTIAL_KMS_KEY"):
            await decrypt(stored, context="555:password")

    @pytest.mark.parametrize("status", [400, 403, 500, 503])
    async def test_kms_errors_never_carry_the_request(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        fake = FakeKms(fail_with=status)
        monkeypatch.setattr(settings, "credential_kms_key", KEY)
        monkeypatch.setattr(credential_crypto, "_transport", httpx.MockTransport(fake))
        monkeypatch.setattr(credential_crypto, "_access_token", lambda: "test-token")
        with pytest.raises(CredentialEncryptionError) as raised:
            await encrypt(SECRET, context="555:password")
        assert str(status) in str(raised.value)
        encoded = base64.b64encode(SECRET.encode()).decode()
        assert SECRET not in str(raised.value) and encoded not in str(raised.value)

    async def test_kms_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(settings, "credential_kms_key", KEY)
        monkeypatch.setattr(credential_crypto, "_transport", httpx.MockTransport(handler))
        monkeypatch.setattr(credential_crypto, "_access_token", lambda: "test-token")
        with pytest.raises(CredentialEncryptionError, match="unreachable"):
            await encrypt(SECRET, context="555:password")

    def test_scheme_of_reads_only_the_prefix(self) -> None:
        assert scheme_of("kms1:anything") == "kms"
        assert scheme_of("gAAAAABlegacyfernet") == "fernet"


@pytest_asyncio.fixture
async def service(kms: FakeKms, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.credential_service.AsyncSessionLocal", session_local)
    yield CredentialService(), session_local
    await engine.dispose()


class TestStoreWithKms:
    async def test_saved_and_read_back_through_kms(self, service) -> None:  # type: ignore[no-untyped-def]
        store, session_local = service
        await store.set_credentials("555", "M-555", SECRET)

        async with session_local() as session:
            record = (await session.execute(select(WaldenCredentialRecord))).scalar_one()
        assert scheme_of(str(record.password_encrypted)) == "kms"
        assert scheme_of(str(record.member_number_encrypted)) == "kms"

        creds = await store.require_credentials("555")
        assert (creds.member_number, creds.password) == ("M-555", SECRET)

    async def test_a_ciphertext_moved_to_another_member_does_not_decrypt(self, service) -> None:  # type: ignore[no-untyped-def]
        """Someone with database write cannot hand member A's login to member B."""
        store, session_local = service
        await store.set_credentials("555", "M-555", SECRET)
        await store.set_credentials("777", "M-777", "other-password")

        async with session_local() as session:
            rows = {
                r.phone_number: r
                for r in (await session.execute(select(WaldenCredentialRecord))).scalars()
            }
            await session.execute(
                update(WaldenCredentialRecord)
                .where(WaldenCredentialRecord.phone_number == "777")
                .values(password_encrypted=rows["555"].password_encrypted)
            )
            await session.commit()

        with pytest.raises(CredentialEncryptionError):
            await store.require_credentials("777")
