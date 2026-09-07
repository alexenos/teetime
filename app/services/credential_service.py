"""
Per-requester Walden Golf credential resolution (issue #179).

Each friend already has their own Walden membership; Dax adds their login
here himself (see scripts/add_walden_credential.py) once, encrypted at rest.
A requester with no row of their own falls back to the single shared account
in settings.walden_member_number / walden_password, so the existing flow
keeps working while friends are added incrementally.
"""

from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.models.database import AsyncSessionLocal, WaldenCredentialRecord
from app.services import credential_crypto


@dataclass(frozen=True)
class WaldenCredentials:
    """A resolved Walden Golf login - never persisted or logged as a whole."""

    member_number: str
    password: str


class CredentialService:
    """Reads and writes the admin-managed per-requester credential store."""

    async def get_dedicated_credentials(self, phone_number: str) -> WaldenCredentials | None:
        """This requester's own admin-added login, or None if they have none.

        Distinct from resolve() below: this never falls back to the shared
        default, so callers deciding whether a requester needs their own
        provider instance (see BookingService._provider_for) can tell "has
        their own login" apart from "uses the shared one".
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(WaldenCredentialRecord).where(
                    WaldenCredentialRecord.phone_number == phone_number
                )
            )
            record = result.scalar_one_or_none()

        if record is None:
            return None

        return WaldenCredentials(
            member_number=credential_crypto.decrypt(str(record.member_number_encrypted)),
            password=credential_crypto.decrypt(str(record.password_encrypted)),
        )

    async def resolve(self, phone_number: str) -> WaldenCredentials | None:
        """The credentials this requester's booking should run under.

        Their own admin-added login if one exists, else the single shared
        global account. None if neither is configured.
        """
        dedicated = await self.get_dedicated_credentials(phone_number)
        if dedicated is not None:
            return dedicated

        if settings.walden_member_number and settings.walden_password:
            return WaldenCredentials(
                member_number=settings.walden_member_number,
                password=settings.walden_password,
            )

        return None

    async def set_credentials(
        self,
        phone_number: str,
        member_number: str,
        password: str,
        label: str | None = None,
    ) -> None:
        """Add or update a friend's Walden login. Admin-only - see the script."""
        member_number_encrypted = credential_crypto.encrypt(member_number)
        password_encrypted = credential_crypto.encrypt(password)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(WaldenCredentialRecord).where(
                    WaldenCredentialRecord.phone_number == phone_number
                )
            )
            record = result.scalar_one_or_none()

            if record is None:
                record = WaldenCredentialRecord(phone_number=phone_number)
                session.add(record)

            record.member_number_encrypted = member_number_encrypted  # type: ignore[assignment]
            record.password_encrypted = password_encrypted  # type: ignore[assignment]
            record.label = label  # type: ignore[assignment]

            await session.commit()

    async def remove_credentials(self, phone_number: str) -> bool:
        """Delete a friend's stored login. Returns False if none existed."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(WaldenCredentialRecord).where(
                    WaldenCredentialRecord.phone_number == phone_number
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return False

            await session.delete(record)
            await session.commit()
            return True


credential_service = CredentialService()
