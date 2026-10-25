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
* **/forget breaks the link but keeps the label (#276).** The member's finished
  bookings are rewritten to their label, now and then nightly as the rest
  finish; once none is left, their Telegram ID on the label row is replaced by
  a placeholder. The label stays taken, and a member who rejoins after that
  starts as a new member with a new label.
* **Logs carry the label, not the Telegram ID.** app.log_safety rewrites every
  known member's ID to their label in every log line, in the service, the racer
  and the observer, so a report quoting a log line quotes no identifier.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.log_safety import set_member_pseudonyms
from app.models.database import (
    AsyncSessionLocal,
    BookingRecord,
    MemberPseudonymRecord,
    SessionRecord,
)
from app.models.schemas import BookingStatus
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

LABEL_PATTERN = re.compile(r"^Member [A-Z]+$")

# What a forgotten member's requester_id becomes once no booking of theirs is
# left to rewrite. Unique per row, fits the column, and identifies nobody.
FORGOTTEN_ID_PREFIX = "forgotten-"
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
            # A member who rejoins before the nightly sweep has broken the link
            # is simply a member again: clearing the mark stops the sweep from
            # rewriting their new bookings.
            await _clear_forgotten(requester_id)
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


async def _clear_forgotten(requester_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(MemberPseudonymRecord)
            .where(
                MemberPseudonymRecord.requester_id == requester_id,
                MemberPseudonymRecord.forgotten_at.is_not(None),
            )
            .values(forgotten_at=None)
        )
        await session.commit()


def _finished(today: date) -> ColumnElement[bool]:
    """Bookings nothing will act on again: failed or cancelled, or won and played.

    The racer, the reconciler and the result message all need the real
    requester ID, so anything else waits until it finishes.
    """
    return or_(
        BookingRecord.status.in_([BookingStatus.FAILED, BookingStatus.CANCELLED]),
        and_(BookingRecord.status == BookingStatus.SUCCESS, BookingRecord.requested_date < today),
    )


async def _rewrite_finished(
    session: AsyncSession, requester_id: str, label: str, today: date
) -> int:
    """Rewrite a member's finished bookings to their label. Returns the count.

    The private chat's ID is the member's own Telegram ID, so it goes; a group
    chat's identifies nobody and stays. Error text is cleared because nothing
    checks what it can contain.
    """
    finished = and_(BookingRecord.phone_number == requester_id, _finished(today))
    await session.execute(
        update(BookingRecord)
        .where(finished, BookingRecord.origin_channel_id == requester_id)
        .values(origin_channel_id=None)
    )
    result = await session.execute(
        update(BookingRecord)
        .where(finished)
        .values(phone_number=label, requester_handle=None, error_message=None)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


@dataclass
class ForgetOutcome:
    """What forget did: the member's label (None if they had none) and the rewrites."""

    label: str | None
    bookings_rewritten: int


async def forget(requester_id: str, today: date | None = None) -> ForgetOutcome:
    """Take a member's identity off their booking history (issue #276).

    Marks their label forgotten, rewrites their finished bookings to it, and
    deletes their conversation session. Bookings still to come are rewritten
    by sweep_forgotten once they finish. A member with no bookings and no
    label gets none: there is nothing to keep, and nothing links them. Their
    session is deleted either way.

    Returns the label and how many bookings were rewritten now.
    """
    today = today or CTDateTime.now().date()
    async with AsyncSessionLocal() as session:
        has_bookings = (
            await session.execute(
                select(BookingRecord.id).where(BookingRecord.phone_number == requester_id).limit(1)
            )
        ).first() is not None
    label = await label_for(requester_id)
    if label is None:
        if not has_bookings:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    delete(SessionRecord).where(SessionRecord.phone_number == requester_id)
                )
                await session.commit()
            return ForgetOutcome(label=None, bookings_rewritten=0)
        label, _ = await assign(requester_id)

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(MemberPseudonymRecord)
            .where(MemberPseudonymRecord.requester_id == requester_id)
            .values(forgotten_at=_utcnow())
        )
        rewritten = await _rewrite_finished(session, requester_id, label, today)
        await session.execute(
            delete(SessionRecord).where(SessionRecord.phone_number == requester_id)
        )
        await session.commit()
    logger.info(f"Forgot {label}: {rewritten} finished booking(s) now under the label")
    return ForgetOutcome(label=label, bookings_rewritten=rewritten)


@dataclass
class SweepResult:
    """What sweep_forgotten did, or on a dry run would do."""

    bookings_rewritten: int = 0
    links_released: int = 0


async def sweep_forgotten(dry_run: bool = False, today: date | None = None) -> SweepResult:
    """Finish forgetting: rewrite bookings that have finished since, then break the link.

    Run nightly by the retention purge. For each forgotten member whose link is
    still in place, their newly finished bookings are rewritten to their label
    and any new session is deleted. Once no booking under their Telegram ID is
    left, requester_id is replaced by a placeholder.
    """
    today = today or CTDateTime.now().date()
    result = SweepResult()
    async with AsyncSessionLocal() as session:
        forgotten = (
            (
                await session.execute(
                    select(MemberPseudonymRecord).where(
                        MemberPseudonymRecord.forgotten_at.is_not(None),
                        MemberPseudonymRecord.requester_id.not_like(f"{FORGOTTEN_ID_PREFIX}%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        for record in forgotten:
            requester_id, label = str(record.requester_id), str(record.label)
            owned = BookingRecord.phone_number == requester_id
            if dry_run:
                finished = await session.scalar(
                    select(func.count()).select_from(BookingRecord).where(owned, _finished(today))
                )
                unfinished = await session.scalar(
                    select(func.count()).select_from(BookingRecord).where(owned, ~_finished(today))
                )
                result.bookings_rewritten += int(finished or 0)
                result.links_released += int(not unfinished)
                continue
            result.bookings_rewritten += await _rewrite_finished(
                session, requester_id, label, today
            )
            await session.execute(
                delete(SessionRecord).where(SessionRecord.phone_number == requester_id)
            )
            remaining = await session.scalar(
                select(func.count()).select_from(BookingRecord).where(owned)
            )
            if not remaining:
                record.requester_id = f"{FORGOTTEN_ID_PREFIX}{record.id}"  # type: ignore[assignment]
                result.links_released += 1
                logger.info(f"{label}: no booking left to rewrite; requester ID removed")
        if not dry_run:
            await session.commit()
    if result.links_released and not dry_run:
        await refresh_log_filter()
    return result
