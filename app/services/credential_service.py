"""
Per-requester Walden Golf credential resolution (issue #179).

Each friend already has their own Walden membership; Dax adds their login
here himself (see scripts/add_walden_credential.py) once, encrypted at rest.
A requester with no row of their own falls back to the single shared account
in settings.walden_member_number / walden_password, so the existing flow
keeps working while friends are added incrementally.

Two things here serve admin proxy booking (issue #185) rather than that flow:
find_by_name_or_telegram_username, which resolves the admin's "for @X" to a
friend, and the ProxyAdminHasNoCredentialError guard, which makes the admin's
own identity the one requester that never resolves to a credential at all -
not even the shared fallback.
"""

from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.models.database import AsyncSessionLocal, WaldenCredentialRecord
from app.services import credential_crypto
from app.services.proxy_booking import is_proxy_admin, normalize_target


class ProxyAdminHasNoCredentialError(RuntimeError):
    """Raised when something tries to book under the proxy admin's own identity.

    The admin account is proxy-only by design (issue #185): it has no row in
    walden_credentials and must not quietly borrow the shared global account
    either, because a booking made that way would be attributed to nobody real
    and would burn the shared membership's one-round-per-day slot. Every path
    that resolves credentials goes through get_dedicated_credentials, so
    raising there closes all of them at once - and every caller already treats
    an exception from that lookup as a loud booking failure.
    """


@dataclass(frozen=True)
class WaldenCredentials:
    """A resolved Walden Golf login - never persisted or logged as a whole."""

    member_number: str
    password: str


@dataclass(frozen=True)
class CredentialOwner:
    """Who a stored credential belongs to, without the credential itself.

    Returned by the proxy-target lookup, which resolves "for @alex" to a
    requester identity. Carries no secrets: the caller only needs to know whose
    booking this becomes, and the login is fetched later on the booking path
    like any other requester's.
    """

    phone_number: str
    name: str | None
    telegram_username: str | None

    @property
    def display_name(self) -> str:
        """What to call this friend when talking to the admin about them."""
        return self.name or (
            f"@{self.telegram_username}" if self.telegram_username else self.phone_number
        )


class CredentialService:
    """Reads and writes the admin-managed per-requester credential store."""

    async def get_dedicated_credentials(self, phone_number: str) -> WaldenCredentials | None:
        """This requester's own admin-added login, or None if they have none.

        Distinct from resolve() below: this never falls back to the shared
        default, so callers deciding whether a requester needs their own
        provider instance (see BookingService._provider_for) can tell "has
        their own login" apart from "uses the shared one".

        Raises ProxyAdminHasNoCredentialError for the proxy admin's own
        identity. Returning None there would be worse than wrong: None means
        "use the shared account", which is exactly the silent fallback the
        admin account exists to avoid.
        """
        if is_proxy_admin(phone_number):
            raise ProxyAdminHasNoCredentialError(
                "The admin account books on a friend's behalf and has no Walden login of "
                "its own. This booking is not attributed to anyone with a credential."
            )

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

    async def get_owner(self, phone_number: str) -> CredentialOwner | None:
        """The naming fields on this requester's credential row, without secrets.

        Used to say who a proxy booking is for (issue #185) - the admin typed
        "@alex", and the reply should read "for Alex" rather than echoing a
        Telegram user ID back. None when this requester has no stored
        credential, which on the proxy path cannot happen: the target was
        resolved from this very table.
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

        return CredentialOwner(
            phone_number=str(record.phone_number),
            name=str(record.name) if record.name else None,
            telegram_username=str(record.telegram_username) if record.telegram_username else None,
        )

    async def find_by_name_or_telegram_username(self, target: str) -> list[CredentialOwner]:
        """Every friend whose stored name or Telegram handle matches "@X".

        Used only to resolve a proxy admin's "for @X" (issue #185); nothing on
        the normal booking path resolves a credential by anything but the
        requester's own identity.

        Returns every match rather than picking one. A caller that got two
        matches - a name colliding with someone else's handle - must refuse and
        say so, because booking under the wrong friend's membership is the one
        outcome this feature has to make impossible. An empty list likewise
        means "say who you meant", never "use the shared account".

        Matching is done in Python over the whole (tiny - one row per friend)
        table rather than in SQL: case folding, the optional leading "@", and
        the two-column match are all easier to keep consistent in one place
        than across SQLite and Postgres collations. This runs on the admin's
        interactive diagnostic path, never inside the 6:30 race.
        """
        wanted = normalize_target(target)
        if not wanted:
            return []

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(WaldenCredentialRecord))
            records = result.scalars().all()

        matches: list[CredentialOwner] = []
        for record in records:
            name = str(record.name) if record.name else None
            username = str(record.telegram_username) if record.telegram_username else None
            candidates = {normalize_target(value) for value in (name, username) if value}
            if wanted in candidates:
                matches.append(
                    CredentialOwner(
                        phone_number=str(record.phone_number),
                        name=name,
                        telegram_username=username,
                    )
                )
        return matches

    async def set_credentials(
        self,
        phone_number: str,
        member_number: str,
        password: str,
        label: str | None = None,
        name: str | None = None,
        telegram_username: str | None = None,
    ) -> None:
        """Add or update a friend's Walden login. Admin-only - see the script.

        ``name`` and ``telegram_username`` are what a proxy admin's "for @X"
        matches against (issue #185); ``label`` remains a free-text note that
        resolves nothing. Each of the three is left untouched when not passed,
        so updating a friend's password does not silently erase the name that
        makes them addressable.
        """
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
            if label is not None:
                record.label = label  # type: ignore[assignment]
            if name is not None:
                record.name = name  # type: ignore[assignment]
            if telegram_username is not None:
                # Stored without the "@" so it reads the same however the admin
                # typed it; normalize_target strips one on the way in too.
                record.telegram_username = telegram_username.strip().lstrip("@") or None  # type: ignore[assignment]

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
