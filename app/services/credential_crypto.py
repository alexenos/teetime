"""
Encryption for per-friend Walden Golf credentials at rest (issue #179).

Fernet (symmetric, authenticated) rather than GCP Secret Manager: credential
resolution sits on the path to every booking attempt, including the 6:30am
race decided in hundreds of milliseconds, so it must not add a network call.
The key itself is deployed the same way as every other secret this project
has (see terraform/main.tf) - a Cloud Run env var backed by Secret Manager -
so this only adds one more secret, not a new access pattern.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CredentialEncryptionError(RuntimeError):
    """Raised when a per-user credential can't be encrypted or decrypted."""


def _fernet() -> Fernet:
    """Build a Fernet cipher from CREDENTIAL_ENCRYPTION_KEY, or raise."""
    if not settings.credential_encryption_key:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not configured; cannot read or write "
            "per-user Walden credentials. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(settings.credential_encryption_key.encode())
    except (ValueError, TypeError) as e:
        raise CredentialEncryptionError(f"CREDENTIAL_ENCRYPTION_KEY is invalid: {e}") from e


def encrypt(value: str) -> str:
    """Encrypt a plaintext credential field for storage."""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a credential field read from storage."""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise CredentialEncryptionError(
            "Stored credential could not be decrypted - CREDENTIAL_ENCRYPTION_KEY "
            "may have changed since it was written."
        ) from e
