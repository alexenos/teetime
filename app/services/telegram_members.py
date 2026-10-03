"""
The members' Telegram group as the way in (issue #239).

Before this, letting someone use the bot meant finding their numeric Telegram
ID, adding a new version of the TELEGRAM_ALLOWED_USER_IDS secret, and
redeploying, because Cloud Run resolves secrets when it creates a revision.
Now anyone currently in the members group (TELEGRAM_MEMBERS_CHAT_ID) may talk
to the bot as well, so the maintainer's whole job for a new member is approving
their request to join. The allowlist still works alongside it, so a
misconfigured group ID cannot lock out the people already using the bot.

Leaving the group, or being removed from it, is offboarding: pending bookings
are cancelled and the stored Walden login is deleted, so nothing keeps running
under a membership whose owner has left. That is driven by Telegram's
chat_member updates, which it only sends to a bot that is an admin of the
group - see operations/telegram-setup.md for the group's one-time setup.

Membership is read with getChatMember and cached briefly, so a conversation is
not a Telegram round trip per message. The cache is updated directly from
chat_member updates, so an approval takes effect on the next message rather
than when a cached "not a member" expires. A lookup that fails is never cached
and always refuses: this decides who may book under a Walden membership, so an
outage must fail closed, but must not keep failing after it ends.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from app.config import settings
from app.models.schemas import BookingStatus
from app.providers.telegram_provider import (
    ChatMemberLookupError,
    TelegramProvider,
    addressee_prefix,
    is_authorized_user,
    is_member_status,
)
from app.services.booking_service import booking_service
from app.services.credential_service import credential_service
from app.services.sms_service import sms_service
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

# How long a getChatMember answer is trusted. A removal is normally applied at
# once from its chat_member update; this bounds how long a removed member keeps
# access if that update never arrives.
MEMBER_CACHE_SECONDS = 300.0
# Shorter for "not a member", so that if the join update is missed, someone
# whose request was just approved waits a minute rather than five.
NON_MEMBER_CACHE_SECONDS = 60.0

# user ID -> (is a member, monotonic time the answer expires)
_membership_cache: dict[str, tuple[bool, float]] = {}


def record_membership(user_id: str, is_member: bool) -> None:
    """Cache a definitive membership answer for this user."""
    ttl = MEMBER_CACHE_SECONDS if is_member else NON_MEMBER_CACHE_SECONDS
    _membership_cache[user_id] = (is_member, time.monotonic() + ttl)


def clear_membership_cache() -> None:
    """Forget every cached answer. For tests."""
    _membership_cache.clear()


async def is_group_member(user_id: str) -> bool:
    """Whether this user is currently in the members group.

    False when group access is off, when Telegram says they are not in it, and
    when Telegram could not be asked - the last one uncached (see the module
    docstring).
    """
    chat_id = settings.telegram_members_chat()
    if not chat_id:
        return False

    cached = _membership_cache.get(user_id)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]

    try:
        member = await TelegramProvider().get_chat_member(chat_id, user_id)
    except ChatMemberLookupError as exc:
        logger.warning(f"Could not check members-group membership for {user_id}: {exc}")
        return False

    is_member = member is not None and is_member_status(member)
    record_membership(user_id, is_member)
    return is_member


async def is_authorized(user_id: str, is_bot: bool) -> bool:
    """Whether an inbound Telegram update from this user should be handled.

    The allowlist is checked first and needs no API call, so the people
    already using the bot never depend on Telegram answering getChatMember.
    """
    if is_bot:
        return False
    if is_authorized_user(user_id, is_bot):
        return True
    return await is_group_member(user_id)


def _display_name(user: dict[str, object]) -> str:
    """What to call this user when telling the admin about them."""
    first_name = user.get("first_name")
    if isinstance(first_name, str) and first_name:
        return first_name
    username = user.get("username")
    if isinstance(username, str) and username:
        return f"@{username}"
    return str(user.get("id", "someone"))


async def handle_chat_member_update(update: dict[str, object]) -> str:
    """Act on a chat_member update: welcome a joiner, offboard a leaver.

    Returns "ok" when it acted and "ignored" otherwise - an update for some
    other chat the bot administers, a bot, or a change that is neither a join
    nor a leave (a promotion, say).
    """
    chat = update.get("chat")
    old = update.get("old_chat_member")
    new = update.get("new_chat_member")
    if not isinstance(chat, dict) or not isinstance(old, dict) or not isinstance(new, dict):
        return "ignored"

    members_chat = settings.telegram_members_chat()
    if not members_chat or str(chat.get("id", "")) != members_chat:
        return "ignored"

    user = new.get("user")
    if not isinstance(user, dict) or user.get("is_bot") or not user.get("id"):
        return "ignored"
    user_id = str(user["id"])

    was_member = is_member_status(old)
    is_member = is_member_status(new)
    if was_member == is_member:
        # A promotion, a restriction, a title change. Nothing to act on, and
        # nothing cached either: a late one could otherwise re-authorize
        # someone whose leave was already processed.
        return "ignored"

    # Updates can arrive late and out of order: a failed one is redelivered
    # (the webhook answers 500 so offboarding is retried), and by then the
    # person may have left or rejoined. So each one is checked against what
    # Telegram says right now before it is acted on or cached.
    current = await _current_membership(members_chat, user_id)

    if is_member:
        if current is False:
            logger.info(
                f"Ignoring a stale join update for Telegram user {user_id}: not in the group now"
            )
            record_membership(user_id, False)
            return "ignored"
        if current is True:
            record_membership(user_id, True)
        # Unknown: welcome them, but cache nothing. The next message asks
        # Telegram again rather than trusting an unconfirmed join.
        await _welcome(members_chat, user)
        return "ok"

    if current is True:
        logger.info(
            f"Ignoring a stale leave update for Telegram user {user_id}: in the group again"
        )
        record_membership(user_id, True)
        return "ignored"
    # False or unknown: offboard. The update says they left, and keeping a
    # departed member's login is the worse mistake.
    record_membership(user_id, False)
    await offboard(user)
    return "ok"


async def _current_membership(chat_id: str, user_id: str) -> bool | None:
    """What Telegram says about this user's membership right now, bypassing the cache.

    None when Telegram could not be asked.
    """
    try:
        member = await TelegramProvider().get_chat_member(chat_id, user_id)
    except ChatMemberLookupError as exc:
        logger.warning(f"Could not re-check members-group membership for {user_id}: {exc}")
        return None
    return member is not None and is_member_status(member)


async def _welcome(chat_id: str, user: dict[str, object]) -> None:
    """Greet a new member in the group and point them at a private chat.

    A bot cannot open a private chat itself, so the deep link is how the
    member starts one; "?start=setup" is where connecting their Walden login
    begins (#240).
    """
    user_id = str(user["id"])
    logger.info(f"Telegram user {user_id} joined the members group")

    bot_username = await TelegramProvider().get_bot_username()
    if bot_username:
        where = f"open a private chat with me: https://t.me/{bot_username}?start=setup"
    else:
        where = "open a private chat with me and send /start"
    message = (
        f"{addressee_prefix(user)}welcome! I book tee times at Northgate. "
        f"To connect your Walden account, {where}"
    )
    await sms_service.send_sms(user_id, message, origin_channel_id=chat_id, channel="telegram")


async def offboard(user: dict[str, object]) -> None:
    """Stop booking for someone who has left the members group.

    Cancels their bookings that have not started yet and deletes their stored
    Walden login, then tells them and the admin what was done. A booking
    already reserved at the club is left alone - it is the member's own tee
    time, and with their login deleted it could not be cancelled there anyway.
    An attempt already in progress is left to finish.

    Someone still on the allowlist keeps access after leaving, so nothing of
    theirs is deleted; the admin is told instead.

    Safe to run twice: a second run finds nothing pending and no login.
    """
    user_id = str(user["id"])
    name = _display_name(user)

    if is_authorized_user(user_id, is_bot=False):
        logger.warning(
            f"Telegram user {user_id} left the members group but is on "
            "TELEGRAM_ALLOWED_USER_IDS; access kept, nothing deleted"
        )
        await _tell_admin(
            f"{name} left the members group but is still on TELEGRAM_ALLOWED_USER_IDS, so "
            "they can still use the bot. Nothing was cancelled or deleted."
        )
        return

    result = await forget_member(user_id)
    cancelled, still_reserved, login_deleted = (
        result.cancelled,
        result.still_reserved,
        result.login_deleted,
    )
    logger.info(
        f"Offboarded Telegram user {user_id}: {cancelled} booking(s) cancelled, "
        f"login {'deleted' if login_deleted else 'not on file'}"
    )

    lines = ["You've left the Northgate tee time group, so I've stopped booking for you."]
    if cancelled:
        lines.append(f"I cancelled {_bookings(cancelled, 'pending booking request')}.")
    if login_deleted:
        lines.append("I deleted the Walden login you'd saved with me.")
    if still_reserved:
        lines.append(
            f"{_bookings(still_reserved, 'tee time').capitalize()} already reserved at the club "
            "stay yours - cancel those with the club if you need to."
        )
    lines.append("Ask to rejoin the group any time.")
    # A private chat only exists if they ever started one; if not, Telegram
    # refuses the send and send_sms reports that rather than raising.
    await sms_service.send_sms(user_id, " ".join(lines), channel="telegram")

    await _tell_admin(
        f"{name} left the members group. Cancelled {_bookings(cancelled, 'pending booking')}; "
        + ("deleted their Walden login." if login_deleted else "no Walden login was stored.")
    )


# One lock per member, held across anything that writes or deletes their
# stored login: the setup form's check-then-save (#240) and forget_member.
# Without it, a /forget or a leave landing while Walden is checking a login is
# undone a second later, when the check succeeds and saves the login again.
# In-process: the service runs as a single instance (cloud_run_max_instances).
_member_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def member_lock(user_id: str) -> asyncio.Lock:
    """The lock guarding this member's stored login."""
    return _member_locks[user_id]


@dataclass(frozen=True)
class ForgetResult:
    """What forget_member did."""

    cancelled: int
    still_reserved: int
    login_deleted: bool


async def forget_member(user_id: str) -> ForgetResult:
    """Cancel this member's bookings that have not started and delete their login.

    Shared by offboarding (they left the group) and /forget (they asked, #240).
    A tee time already reserved at the club is counted rather than touched: it
    is the member's own, and with their login deleted it could not be
    cancelled there anyway. An attempt in progress is left to finish.
    """
    # Waits for a setup-form check in flight, so what it saves is deleted here
    # rather than written back afterwards.
    async with member_lock(user_id):
        return await _forget_member_locked(user_id)


async def _forget_member_locked(user_id: str) -> ForgetResult:
    today = CTDateTime.now().date()
    cancelled = 0
    still_reserved = 0
    for booking in await booking_service.get_bookings(phone_number=user_id):
        if booking.status in (BookingStatus.PENDING, BookingStatus.SCHEDULED):
            if booking.id and await booking_service.cancel_booking(booking.id) is not None:
                cancelled += 1
        elif booking.status == BookingStatus.SUCCESS and booking.request.requested_date >= today:
            still_reserved += 1
    login_deleted = await credential_service.remove_credentials(user_id)
    return ForgetResult(cancelled, still_reserved, login_deleted)


def _bookings(count: int, noun: str = "booking") -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


async def _tell_admin(message: str) -> None:
    """Send the proxy admin a note, if one is configured."""
    admin = settings.telegram_admin_id()
    if admin:
        await sms_service.send_sms(admin, message, channel="telegram")
