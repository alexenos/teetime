"""
Admin tool for the member Walden login store (issues #179, #242).

Members connect their own login through the bot (send it /start in a private
chat - issue #240), so the admin never sees it. This script can no longer add
or change one: `set` is retired (#242), because typing a member's password here
was exactly the exposure the setup form exists to remove.

What remains needs no key and shows no secret:

    poetry run python scripts/add_walden_credential.py list
    poetry run python scripts/add_walden_credential.py remove <requester id>
    poetry run python scripts/add_walden_credential.py set-pseudonym <requester id> "Member A"

`set-pseudonym` records which existing member a label from the hand-kept
registry belongs to (#256). New members get theirs automatically when they
connect a login, and a label is never changed once assigned.

`list` shows which encryption each row uses, and whether it keeps a Fernet
fallback copy (written during the move to KMS; `clear-fallbacks` deletes them
when the Fernet key is retired). Before CREDENTIAL_ENCRYPTION_KEY
is deleted, every row must say `kms` - a `fernet` row is a member who has not
re-entered their login since logins moved to Cloud KMS, and deleting the key
would make it unreadable.

<requester id> is whatever identity the member's bookings use - for Telegram,
their numeric user ID.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, WaldenCredentialRecord, init_db
from app.services import pseudonyms
from app.services.credential_crypto import scheme_of
from app.services.credential_service import credential_service


async def _remove(phone_number: str) -> None:
    """Delete this requester's stored credential, if one exists."""
    removed = await credential_service.remove_credentials(phone_number)
    print(
        f"Removed credential for {phone_number}"
        if removed
        else f"No credential found for {phone_number}"
    )


async def _list() -> None:
    """Print every requester with a stored credential (never the secrets themselves)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(WaldenCredentialRecord))
        records = result.scalars().all()

    labels = await pseudonyms.load_all()

    if not records:
        print("No per-friend Walden credentials stored.")
        return

    for record in records:
        # Name and handle are shown because they are what "for @X" matches;
        # an empty pair is the reason a proxy booking will not resolve.
        identity = ", ".join(
            part
            for part in (
                f"name={record.name}" if record.name else "",
                f"telegram=@{record.telegram_username}" if record.telegram_username else "",
                f"label={record.label}" if record.label else "",
            )
            if part
        )
        suffix = f" ({identity})" if identity else " (no name or handle - not proxy-bookable)"
        # Read from the stored values' prefixes; nothing is decrypted.
        schemes = {
            scheme_of(str(record.member_number_encrypted)),
            scheme_of(str(record.password_encrypted)),
        }
        encryption = schemes.pop() if len(schemes) == 1 else "mixed"
        fallback = "yes" if record.password_fallback else "no"
        pseudonym = labels.get(str(record.phone_number), "none - map with set-pseudonym")
        print(
            f"{record.phone_number}{suffix} - pseudonym={pseudonym}, "
            f"encryption={encryption}, fallback={fallback}, "
            f"added {record.created_at}, updated {record.updated_at}"
        )

    remaining = sum(
        1
        for r in records
        if "fernet"
        in {scheme_of(str(r.member_number_encrypted)), scheme_of(str(r.password_encrypted))}
    )
    print()
    if remaining:
        print(
            f"{remaining} row(s) still depend on CREDENTIAL_ENCRYPTION_KEY. Do not delete it "
            "until each of those members has re-entered their login (/start, then /login)."
        )
    else:
        print("No row depends on CREDENTIAL_ENCRYPTION_KEY.")

    copies = sum(1 for r in records if r.password_fallback)
    if copies:
        print(
            f"{copies} row(s) also keep a Fernet fallback copy, readable with that key. "
            "When the key is retired, delete them with: clear-fallbacks"
        )


async def _set_pseudonym(requester_id: str, label: str) -> None:
    """Record which existing member a hand-assigned registry label belongs to (#256)."""
    try:
        await pseudonyms.set_label(requester_id, label)
    except pseudonyms.PseudonymError as exc:
        raise SystemExit(str(exc)) from None
    print(f"{requester_id} is {label} in logs from the next deploy or job run on.")


async def _main() -> None:
    """Parse the subcommand and dispatch to remove/list."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("set", help="Retired (#242): members connect their own login with /start")
    subparsers.add_parser(
        "clear-fallbacks",
        help="Delete the Fernet fallback copies of KMS-encrypted logins (when retiring the key)",
    )
    pseudonym_parser = subparsers.add_parser(
        "set-pseudonym",
        help='Map an existing member to their registry label, e.g. "Member A" (#256)',
    )
    pseudonym_parser.add_argument("phone_number", help="The member's requester ID")
    pseudonym_parser.add_argument(
        "label", help='Their label in MEMBER_PSEUDONYM_REGISTRY, e.g. "Member A"'
    )

    remove_parser = subparsers.add_parser("remove", help="Delete a stored credential")
    remove_parser.add_argument("phone_number")

    subparsers.add_parser(
        "list", help="List requesters with a stored credential (no secrets shown)"
    )

    args = parser.parse_args()

    if args.command == "set":
        raise SystemExit(
            "`set` is retired (#242). Members connect their own Walden login: they send /start "
            "to the bot in a private chat. Typing a member's password here is what that "
            "replaced."
        )

    await init_db()

    if args.command == "remove":
        await _remove(args.phone_number)
    elif args.command == "list":
        await _list()
    elif args.command == "clear-fallbacks":
        cleared = await credential_service.clear_fallbacks()
        print(f"Deleted the Fernet fallback copies on {cleared} row(s).")
    elif args.command == "set-pseudonym":
        await _set_pseudonym(args.phone_number, args.label)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(130)
