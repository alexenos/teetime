"""
Member pseudonyms: the label a member is called by in logs and race reports (#256).

Race reports are public and refer to people by label - ``Member A`` for the
people the bot books for, ``Rival 1`` for everyone else - never by name,
handle, Telegram ID or member number (CLAUDE.md, race-report skill §8a). The
labels were all assigned by hand in Secret Manager (MEMBER_PSEUDONYM_REGISTRY),
which self-service onboarding (#239, #240) never touches. So:

* **Member labels now live in the database** (member_pseudonyms), assigned the
  first time a member saves a login through the setup form. The registry keeps
  rivals, and everyone's name forms, which only a person can supply.
* **Hand-assigned labels are never handed out again.** MEMBER_PSEUDONYMS_RESERVED
  lists them, and the assigner skips those and every label already stored.
* **A label is never reused.** Offboarding deletes a member's login but not
  their label, and a member who rejoins keeps the one they had.
* **Logs carry the label, not the Telegram ID.** app.log_safety rewrites every
  known member's ID to their label in every log line, in the service, the racer
  and the observer, so a report quoting a log line quotes no identifier.
"""

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.log_safety import set_member_pseudonyms
from app.models.database import AsyncSessionLocal, MemberPseudonymRecord

logger = logging.getLogger(__name__)

LABEL_PATTERN = re.compile(r"^Member [A-Z]+$")
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class PseudonymError(ValueError):
    """A label that cannot be assigned: malformed, or already someone else's."""


def _suffix(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA, the way spreadsheet columns count."""
    out = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = _ALPHABET[rem] + out
    return out


def next_label(taken: set[str]) -> str:
    """The first ``Member <letters>`` label not in ``taken``."""
    n = 0
    while True:
        label = f"Member {_suffix(n)}"
        if label not in taken:
            return label
        n += 1


async def load_all() -> dict[str, str]:
    """Every stored member's label, keyed by requester ID."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MemberPseudonymRecord))
        return {str(r.requester_id): str(r.label) for r in result.scalars().all()}


async def refresh_log_filter() -> dict[str, str]:
    """Load every label and hand them to the log filter. Called at startup.

    Never fails a caller: a database error leaves the filter as it was, and
    logs the requester IDs it could not hide - better than refusing to start a
    6:30 race over log hygiene.
    """
    try:
        mapping = await load_all()
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning(f"Could not load member pseudonyms for the log filter: {type(exc).__name__}")
        return {}
    set_member_pseudonyms(mapping)
    return mapping


async def label_for(requester_id: str) -> str | None:
    """This requester's label, or None if they have none yet."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MemberPseudonymRecord).where(MemberPseudonymRecord.requester_id == requester_id)
        )
        record = result.scalar_one_or_none()
    return str(record.label) if record else None


async def assign(requester_id: str) -> tuple[str, bool]:
    """This requester's label, assigning the next free one if they have none.

    Returns the label and whether it was assigned just now. Two assignments
    racing for the same next label cannot both win - the column is unique - so
    the loser picks again.
    """
    for _ in range(5):
        existing = await label_for(requester_id)
        if existing:
            return existing, False
        taken = set((await load_all()).values()) | set(settings.reserved_member_pseudonyms())
        label = next_label(taken)
        try:
            await _insert(requester_id, label)
        except IntegrityError:
            continue  # someone took this label, or this requester, in between
        await refresh_log_filter()
        logger.info(f"Assigned pseudonym {label} to a new member")
        return label, True
    raise PseudonymError(f"Could not assign a pseudonym to requester {requester_id}")


async def set_label(requester_id: str, label: str) -> None:
    """Give a requester a specific label - for mapping existing members by hand.

    Used by scripts/add_walden_credential.py set-pseudonym, to record that the
    members labelled in the registry before this existed are who they are.
    Refuses a malformed label, one already held by someone else, and any change
    to a requester's existing label, which would free the old one for reuse.
    """
    if not LABEL_PATTERN.match(label):
        raise PseudonymError(f'A member label looks like "Member A"; got {label!r}')
    async with AsyncSessionLocal() as session:
        holder = (
            await session.execute(
                select(MemberPseudonymRecord).where(MemberPseudonymRecord.label == label)
            )
        ).scalar_one_or_none()
        if holder is not None and str(holder.requester_id) != requester_id:
            raise PseudonymError(f"{label} already belongs to another requester")
        record = (
            await session.execute(
                select(MemberPseudonymRecord).where(
                    MemberPseudonymRecord.requester_id == requester_id
                )
            )
        ).scalar_one_or_none()
        if record is not None:
            if str(record.label) == label:
                return
            # Changing it would free the old label for assign() to hand to
            # someone else, and old reports would then mean two people by it.
            raise PseudonymError(
                f"This requester is already {record.label}. A label is never changed or "
                "reused once assigned."
            )
        session.add(
            MemberPseudonymRecord(requester_id=requester_id, label=label, assigned_at=_utcnow())
        )
        await session.commit()


async def _insert(requester_id: str, label: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            MemberPseudonymRecord(requester_id=requester_id, label=label, assigned_at=_utcnow())
        )
        await session.commit()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
