"""
Admin tool for the per-friend Walden Golf credential store (issue #179).

Each friend already has their own Walden membership; there is deliberately no
self-service onboarding flow (a credential must never transit chat history),
so Dax runs this locally to add, update, remove, or list them. Values are
encrypted at rest with CREDENTIAL_ENCRYPTION_KEY (see
app/services/credential_crypto.py) - set that in .env or the environment
before running this. Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Usage:

    poetry run python scripts/add_walden_credential.py set <phone_number> --label "Alex"
    (prompts for the member number and password rather than taking them as
    arguments, so neither lands in shell history; --member-number is still
    accepted for scripted use)

    poetry run python scripts/add_walden_credential.py list
    poetry run python scripts/add_walden_credential.py remove <phone_number>

<phone_number> is whatever identity the requester's bookings already use -
the same phone number, Discord snowflake, or Telegram user ID recorded on
their SessionRecord/BookingRecord rows.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, WaldenCredentialRecord, init_db
from app.services.credential_service import credential_service


async def _set(phone_number: str, member_number: str | None, label: str | None) -> None:
    """Prompt for whatever wasn't passed on the command line, then save.

    The member number is part of the Walden login, same as the password, so
    it's optional on the command line for the same reason: an argument lands
    in shell history and is visible to anyone who can list processes on this
    machine. Prompted with getpass, not input(), so it isn't echoed to the
    terminal either. Passing it explicitly still works, for scripted use.
    """
    if not member_number:
        member_number = getpass.getpass("Walden member number: ").strip()
    if not member_number:
        raise SystemExit("Member number cannot be empty.")

    password = getpass.getpass("Walden password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")

    await credential_service.set_credentials(phone_number, member_number, password, label)
    print(f"Saved Walden credentials for {phone_number}" + (f" ({label})" if label else ""))


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

    if not records:
        print("No per-friend Walden credentials stored.")
        return

    for record in records:
        label = f" ({record.label})" if record.label else ""
        print(
            f"{record.phone_number}{label} - added {record.created_at}, updated {record.updated_at}"
        )


async def _main() -> None:
    """Parse the subcommand and dispatch to set/remove/list."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set", help="Add or update a friend's Walden login")
    set_parser.add_argument("phone_number", help="Requester identity used on their bookings")
    set_parser.add_argument(
        "--member-number",
        default=None,
        help="Walden member number (prompted for if omitted, to avoid shell history)",
    )
    set_parser.add_argument("--label", default=None, help="Optional note, e.g. a friend's name")

    remove_parser = subparsers.add_parser("remove", help="Delete a stored credential")
    remove_parser.add_argument("phone_number")

    subparsers.add_parser(
        "list", help="List requesters with a stored credential (no secrets shown)"
    )

    args = parser.parse_args()

    await init_db()

    if args.command == "set":
        await _set(args.phone_number, args.member_number, args.label)
    elif args.command == "remove":
        await _remove(args.phone_number)
    elif args.command == "list":
        await _list()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(130)
