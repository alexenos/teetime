"""
Per-requester Walden Golf credential resolution (issue #179).

Each member already has their own Walden membership, and connects their own
login through the setup form (#240, app/services/member_setup.py), encrypted
at rest. scripts/add_walden_credential.py remains as the admin's fallback.

There is no shared account any more. A requester with no row of their own is
refused - see WaldenCredentialRequiredError - rather than falling back to
settings.walden_member_number / walden_password the way #179 originally had
it. That fallback was a deliberate migration aid, meant to keep existing
users working while friends were added one at a time, but it meant an
unconfigured friend silently booked under somebody else's membership: no
error, no warning, and under the club's one-round-per-member-per-day rule it
could consume the slot the real booking needed. Every booking now runs under
a login belonging to the person it is for, or it does not run.

Two things here serve admin proxy booking (issue #185) rather than that flow:
find_by_name_or_telegram_username, which resolves the admin's "for @X" to a
friend, and the ProxyAdminHasNoCredentialError guard, which makes the admin's
own identity the one requester that never resolves to a credential at all -
not even the shared fallback.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.models.database import AsyncSessionLocal, WaldenCredentialRecord
from app.services import credential_crypto
from app.services.proxy_booking import is_proxy_admin, normalize_target

logger = logging.getLogger(__name__)


class WaldenCredentialRequiredError(RuntimeError):
    """Raised when a booking has no Walden login of its own to run under.

    Every requester needs their own row in walden_credentials. There is
    deliberately nothing to fall back to: booking under a login that is not
    the requester's own is the failure this prevents, and it is worse than not
    booking at all, because the club allows each member one round per day and a
    misattributed booking spends somebody else's.

    Raised from the lookup path, where it is a loud booking failure. Callers
    that are still in a conversation with the user refuse earlier and more
    kindly - see BookingService.create_booking, which turns "you have no login
    on file" into a message the user can act on instead of a failed booking.
    """


class WaldenCredentialInvalidError(RuntimeError):
    """Raised when the requester's stored login is marked as rejected by Walden (#244).

    Its message is written for the member, because it reaches them as the
    failure reason: the booking was not attempted, and saving a new login with
    /login is what fixes it. Not attempting it is the point - each attempt with
    a login Walden has already rejected is another failed login on the
    member's real account, whose lockout policy is unknown.
    """


class ProxyAdminHasNoCredentialError(WaldenCredentialRequiredError):
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
    # When Walden rejected this login at a booking (#244), or None. Carried
    # here so a caller that already holds the login needs no second lookup.
    invalid_since: datetime | None = None


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
    verified_at: datetime | None = None
    invalid_since: datetime | None = None

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

        try:
            return WaldenCredentials(
                member_number=await credential_crypto.decrypt(
                    str(record.member_number_encrypted), context=f"{phone_number}:member_number"
                ),
                password=await credential_crypto.decrypt(
                    str(record.password_encrypted), context=f"{phone_number}:password"
                ),
                invalid_since=record.invalid_since,  # type: ignore[arg-type]
            )
        except credential_crypto.CredentialEncryptionError as primary_error:
            if not (record.member_number_fallback and record.password_fallback):
                raise
            # The migration safety net (#242): KMS failed, and this row still
            # carries a Fernet copy. Loud, so a race report sees that the
            # morning ran on the fallback rather than on KMS.
            logger.warning(
                "CREDENTIAL_FALLBACK: KMS decrypt failed for requester %s (%s); "
                "using the Fernet fallback copy",
                phone_number,
                primary_error,
            )
            return WaldenCredentials(
                member_number=await credential_crypto.decrypt(
                    str(record.member_number_fallback), context=""
                ),
                password=await credential_crypto.decrypt(str(record.password_fallback), context=""),
                invalid_since=record.invalid_since,  # type: ignore[arg-type]
            )

    async def require_credentials(self, phone_number: str) -> WaldenCredentials:
        """This requester's own login, or refuse.

        Replaces #179's resolve(), whose entire purpose was the shared-account
        fallback this removes. There is no second choice to return, so the
        return type is no longer optional: either the caller gets a login
        belonging to the person the booking is for, or it raises.
        """
        dedicated = await self.get_dedicated_credentials(phone_number)
        if dedicated is None:
            raise WaldenCredentialRequiredError(
                f"No Walden login is on file for requester {phone_number}, and there is "
                "no shared account to fall back on. They connect their own with /start."
            )
        if dedicated.invalid_since is not None:
            raise WaldenCredentialInvalidError(
                "Not attempted: Walden rejected your saved login on "
                f"{dedicated.invalid_since:%B %d}. Send /login to me in a private chat to "
                "update it."
            )
        return dedicated

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
            verified_at=record.verified_at,  # type: ignore[arg-type]
            invalid_since=record.invalid_since,  # type: ignore[arg-type]
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
        verified_at: datetime | None = None,
    ) -> None:
        """Add or update a member's Walden login.

        Written by the member themselves through the setup form (#240), which
        passes ``verified_at`` because it checked the login with Walden first,
        or by the admin script, which does not.

        ``name`` and ``telegram_username`` are what a proxy admin's "for @X"
        matches against (issue #185); ``label`` remains a free-text note that
        resolves nothing. Each of the three is left untouched when not passed,
        so updating a friend's password does not silently erase the name that
        makes them addressable.
        """
        # The context binds each KMS ciphertext to this row and field, so it
        # cannot be copied onto another member's row and still decrypt.
        member_number_encrypted = await credential_crypto.encrypt(
            member_number, context=f"{phone_number}:member_number"
        )
        password_encrypted = await credential_crypto.encrypt(
            password, context=f"{phone_number}:password"
        )

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
            # Always overwritten, never left as it was: a new login replaces
            # the one that was verified, so an old date would vouch for it.
            record.verified_at = verified_at  # type: ignore[assignment]
            # A new login is a fresh start: whatever Walden rejected was the old one.
            record.invalid_since = None  # type: ignore[assignment]
            # Rewritten every time, never left: a copy of the old login must
            # not outlive the login it was a copy of.
            record.member_number_fallback = credential_crypto.fallback_encrypt(member_number)  # type: ignore[assignment]
            record.password_fallback = credential_crypto.fallback_encrypt(password)  # type: ignore[assignment]
            if label is not None:
                record.label = label  # type: ignore[assignment]
            if name is not None:
                record.name = name  # type: ignore[assignment]
            if telegram_username is not None:
                # Stored without the "@" so it reads the same however the admin
                # typed it; normalize_target strips one on the way in too.
                record.telegram_username = telegram_username.strip().lstrip("@") or None  # type: ignore[assignment]

            await session.commit()

    async def mark_invalid(
        self, phone_number: str, *, unchanged_since: datetime | None = None
    ) -> bool:
        """Record that Walden rejected this requester's login (#244).

        Returns whether a row was marked. The first rejection's time is kept:
        later attempts are refused before reaching Walden, so a second one
        should not happen, and the original date is what the member is told.

        ``unchanged_since`` is when the rejected attempt began. If the row has
        been written since - the member saved a new login with /login while the
        attempt was still running - the rejection was of the old login, so the
        new one is left alone.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(WaldenCredentialRecord).where(
                    WaldenCredentialRecord.phone_number == phone_number
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return False
            if (
                unchanged_since is not None
                and record.updated_at is not None
                and record.updated_at > unchanged_since
            ):
                logger.info(
                    "Not marking requester %s's login invalid: it was replaced after the "
                    "rejected attempt began",
                    phone_number,
                )
                return False
            if record.invalid_since is None:
                record.invalid_since = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
            return True

    async def count(self) -> int:
        """How many members have a stored login - each one a requester the racer may race."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(func.count()).select_from(WaldenCredentialRecord))
            return int(result.scalar_one())

    async def clear_fallbacks(self) -> int:
        """Delete every Fernet fallback copy (#242). Returns how many rows had one.

        Run when CREDENTIAL_ENCRYPTION_KEY is retired. Only copies whose row's
        primary value is KMS are cleared - on a row still written with Fernet
        there is no copy to begin with - so this can never strand a login.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(WaldenCredentialRecord).where(
                    WaldenCredentialRecord.password_fallback.is_not(None)
                )
            )
            cleared = 0
            for record in result.scalars().all():
                if credential_crypto.scheme_of(str(record.password_encrypted)) != "kms":
                    continue
                record.member_number_fallback = None  # type: ignore[assignment]
                record.password_fallback = None  # type: ignore[assignment]
                cleared += 1
            await session.commit()
            return cleared

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
