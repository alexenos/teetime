"""
A member connects their own Walden login (issue #240).

Before this, the admin typed every member's login into
scripts/add_walden_credential.py, so the admin saw every password. Now the
member does it themselves, in a form that opens inside Telegram (a Mini App
served by this service - see app/api/onboarding.py). The password goes from the
member's phone straight to this service over HTTPS. It is never a chat message,
so it is never in chat history, on Telegram's servers, or in front of the admin.

The flow:

1. /start (or the welcome message's "?start=setup" deep link) in a private chat
   replies with a button that opens the form, and pins the form to the chat's
   menu button.
2. The form posts the login, the password, and the init data Telegram signed
   when it opened the form. The init data is the only thing that says who the
   member is (validate_init_data), so a member can only ever save their own.
3. The login is checked with Walden over direct HTTP (#241) and saved only if
   Walden accepted it, with the time it was checked.

/status says whether a login is on file and when it was checked, never the
login itself. /login re-opens the form, for after a password change. /forget
deletes the login and cancels bookings that have not started.

These commands are answered here, deterministically, before the language model
parser ever sees them - a setup step must not depend on how a model reads it.
"""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from app.config import settings
from app.providers.walden_http_login import LoginOutcome, check_login
from app.services import pseudonyms
from app.services.credential_service import credential_service
from app.services.sms_service import sms_service
from app.services.telegram_members import (
    forget_member,
    is_authorized,
    member_lock,
    still_in_group,
)

logger = logging.getLogger(__name__)

SETUP_PATH = "/onboarding/walden"
SETUP_COMMANDS = frozenset({"start", "login", "status", "forget"})
BUTTON_TEXT = "Connect Walden account"
MENU_BUTTON_TEXT = "Walden login"

# Walden's lockout policy for failed logins is unknown, so a member gets few
# wrong guesses before being asked to wait: each one is a failed login against
# their real account. Unanswered checks are limited separately, more loosely,
# so a Walden outage cannot be used to hammer it either.
MAX_REJECTED_PER_HOUR = 3
MAX_ATTEMPTS_PER_HOUR = 10
_HOUR_S = 3600.0

MAX_LOGIN_LEN = 128
MAX_PASSWORD_LEN = 256

# user ID -> monotonic times of recent attempts. In memory: the service runs as
# a single instance (cloud_run_max_instances = 1), and a restart forgetting the
# count only ever gives a member back attempts they were already allowed.
_rejected: defaultdict[str, deque[float]] = defaultdict(deque)
_attempts: defaultdict[str, deque[float]] = defaultdict(deque)


def clear_rate_limits() -> None:
    """Forget every recorded attempt. For tests."""
    _rejected.clear()
    _attempts.clear()


def setup_url() -> str | None:
    """Where the setup form is served, or None when there is no public URL.

    Telegram only opens Mini Apps over HTTPS, so a plain-HTTP base URL (local
    development) counts as none.
    """
    base = settings.telegram_webhook_base_url.strip().rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}{SETUP_PATH}"


@dataclass(frozen=True)
class CommandReply:
    """What to answer a setup command with.

    ``open_form`` asks the caller to attach the button that opens the setup
    form, which only works in a private chat.
    """

    text: str
    open_form: bool = False


def _private_chat_link(bot_username: str | None) -> str:
    if bot_username:
        return f"https://t.me/{bot_username}?start=setup"
    return "a private chat with me"


async def handle_command(
    command: str, args: str, user_id: str, private: bool, bot_username: str | None
) -> CommandReply:
    """Answer /start, /login, /status or /forget."""
    if not private:
        # The form's button only works in a private chat, and a login's status
        # is nobody else's business.
        return CommandReply(
            f"Let's do that privately: {_private_chat_link(bot_username)} - send /{command} there."
        )

    if command in ("start", "login"):
        if setup_url() is None:
            return CommandReply(
                "Connecting a Walden login isn't available right now. Please try again later."
            )
        owner = await credential_service.get_owner(user_id)
        if command == "start" and owner is not None:
            return CommandReply(
                "Your Walden login is already connected. To book, just tell me what you want, "
                'e.g. "Book Saturday 8am for 4 players". If you\'ve changed your Walden '
                "password, tap the button to update it.",
                open_form=True,
            )
        return CommandReply(
            "Tap the button to connect your Walden account. Your login goes straight from "
            "your phone to the booking service, encrypted - it's never a chat message, and "
            "nobody else sees it. I check it with Walden before saving it.",
            open_form=True,
        )

    if command == "status":
        owner = await credential_service.get_owner(user_id)
        if owner is None:
            return CommandReply(
                "You don't have a Walden login connected. Send /start to connect one."
            )
        if owner.invalid_since is not None:
            # Rejected at a booking attempt (#244); nothing books until replaced.
            return CommandReply(
                f"Walden rejected your saved login on {owner.invalid_since:%Y-%m-%d} (UTC), "
                "so I'm not booking with it - your password may have changed. Send /login to "
                "update it."
            )
        if owner.verified_at is None:
            checked = "It was added before logins were checked when saved."
        else:
            checked = f"Walden accepted it on {owner.verified_at:%Y-%m-%d} (UTC)."
        return CommandReply(
            f"Your Walden login is connected. {checked} Send /login to update it, or /forget "
            "to delete it."
        )

    if command == "forget":
        if args.strip().lower() != "confirm":
            return CommandReply(
                "This deletes the Walden login you saved with me and cancels any booking "
                "requests I haven't made yet. Tee times already reserved at the club stay "
                "yours. To go ahead, send: /forget confirm"
            )
        result = await forget_member(user_id)
        logger.info(
            f"Telegram user {user_id} asked to be forgotten: {result.cancelled} booking(s) "
            f"cancelled, login {'deleted' if result.login_deleted else 'not on file'}"
        )
        if not result.login_deleted and not result.cancelled:
            return CommandReply("There was nothing to delete: you have no Walden login connected.")
        parts = []
        if result.login_deleted:
            parts.append("Deleted your Walden login.")
        if result.cancelled:
            plural = "" if result.cancelled == 1 else "s"
            parts.append(f"Cancelled {result.cancelled} pending booking request{plural}.")
        parts.append("Send /start any time to connect a login again.")
        return CommandReply(" ".join(parts))

    return CommandReply("I don't know that command.")


class SubmitStatus(str, Enum):
    """How a setup form submission ended."""

    SAVED = "saved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    RATE_LIMITED = "rate_limited"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class SubmitResult:
    """What the form shows the member. ``message`` is always written here, never echoed."""

    status: SubmitStatus
    message: str


def _recent(times: deque[float], now: float) -> int:
    while times and now - times[0] > _HOUR_S:
        times.popleft()
    return len(times)


async def submit_login(
    user: dict[str, object], login: str, password: str, consent: bool
) -> SubmitResult:
    """Check a member's Walden login and save it if Walden accepts it.

    ``user`` is the Telegram user from validated init data - the caller must
    have checked the signature; nothing here trusts the form to say who the
    member is. Neither the login nor the password is logged, returned, or
    put in an exception.
    """
    user_id = str(user["id"])
    if not await is_authorized(user_id, bool(user.get("is_bot"))):
        logger.info(f"Setup form submitted by unauthorized Telegram user {user_id}")
        return SubmitResult(
            SubmitStatus.FORBIDDEN,
            "This account isn't a member of the tee time group yet. Ask to join it first.",
        )

    login = login.strip()
    if not consent:
        return SubmitResult(SubmitStatus.INVALID, "Please tick the box to agree first.")
    if not login or not password:
        return SubmitResult(SubmitStatus.INVALID, "Enter both your Walden login and password.")
    if len(login) > MAX_LOGIN_LEN or len(password) > MAX_PASSWORD_LEN:
        return SubmitResult(SubmitStatus.INVALID, "That login or password is too long.")

    lock = member_lock(user_id)
    if lock.locked():
        # A second submission while one is with Walden. Refused rather than
        # queued: it would pass the rejection limit below before the first
        # one's rejection was recorded, and it is almost certainly a double tap.
        return SubmitResult(
            SubmitStatus.RATE_LIMITED, "Still checking your last try with Walden - one moment."
        )
    async with lock:
        return await _check_and_save(user, user_id, login, password)


async def _check_and_save(
    user: dict[str, object], user_id: str, login: str, password: str
) -> SubmitResult:
    """The rate limits, the Walden check and the save, under the member's lock."""
    now = time.monotonic()
    if _recent(_rejected[user_id], now) >= MAX_REJECTED_PER_HOUR:
        return SubmitResult(
            SubmitStatus.RATE_LIMITED,
            "Walden turned down several tries in the last hour. To keep your Walden account "
            "from being locked, please wait an hour, or check your login on the Walden "
            "website first.",
        )
    if _recent(_attempts[user_id], now) >= MAX_ATTEMPTS_PER_HOUR:
        return SubmitResult(
            SubmitStatus.RATE_LIMITED, "That's a lot of tries for one hour. Please wait a while."
        )
    _attempts[user_id].append(now)

    check = await check_login(login, password)
    # No requester ID in these lines: a first-time member has no pseudonym yet,
    # so the log filter (#256) could not hide it.
    logger.info(f"Setup form login check: {check.outcome.value}")

    if check.outcome is LoginOutcome.REJECTED:
        _rejected[user_id].append(now)
        return SubmitResult(
            SubmitStatus.REJECTED,
            "Walden didn't accept that login. Check it on the Walden website, then try again.",
        )
    if check.outcome is LoginOutcome.UNKNOWN:
        return SubmitResult(
            SubmitStatus.UNKNOWN,
            "I couldn't get an answer from Walden just now, so nothing was saved. "
            "Please try again in a few minutes.",
        )

    if not await still_in_group(user_id):
        # They left, or were removed, while Walden was checking. Possibly
        # handled by the other revision during a deploy, which deleted their
        # login; saving now would bring it back.
        logger.info("A member left the group during a login check; not saved")
        return SubmitResult(
            SubmitStatus.FORBIDDEN,
            "You're no longer in the tee time group, so your login wasn't saved.",
        )

    # Someone who already had a login stored predates automatic pseudonyms and
    # may already be labelled by hand in the registry (#256).
    had_login = await credential_service.get_owner(user_id) is not None
    first_name = user.get("first_name")
    username = user.get("username")
    await credential_service.set_credentials(
        user_id,
        login,
        password,
        name=first_name if isinstance(first_name, str) and first_name else None,
        telegram_username=username if isinstance(username, str) and username else None,
        verified_at=_utcnow(),
    )
    logger.info("Saved a verified Walden login")

    # The login is committed. Nothing after this may turn the answer into a
    # failure: the endpoint would tell the member "nothing was saved" while
    # their login sits in the database, and they would not know to /forget it.
    # Each step has its own guard, so one failing never skips the others - the
    # pseudonym above all, which keeps this member's ID out of later logs.
    await _after_save(
        "confirmation message",
        sms_service.send_sms(
            user_id,
            "You're connected - Walden accepted your login. To book, just tell me what you want, "
            'e.g. "Book Saturday 8am for 4 players". Reservations open 7 days ahead at 6:30am CT.',
            channel="telegram",
        ),
    )
    await _after_save("pseudonym", _assign_pseudonym(user_id, user, had_login))
    await _after_save("capacity warning", _warn_if_at_racer_ceiling())
    return SubmitResult(SubmitStatus.SAVED, "Connected. Walden accepted your login.")


async def _after_save(step: str, work: Awaitable[object]) -> None:
    """Run one post-save step, logging rather than raising if it fails."""
    try:
        await work
    except Exception:
        logger.exception(f"Post-save step failed ({step}); the login is saved")


def _utcnow() -> datetime:
    """Naive UTC, matching how the database stores every other timestamp."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _assign_pseudonym(user_id: str, user: dict[str, object], had_login: bool) -> None:
    """Give a newly connected member their label for logs and reports (#256).

    Only a member with no login stored before this save is labelled
    automatically. Anyone who already had one predates automatic pseudonyms
    and may already be "Member A" in the hand-kept registry: an automatic label
    would be a second one, and labels are never changed. The admin is asked to
    map them with set-pseudonym instead.

    The admin is told either way, because the registry still needs the
    member's name forms and handle - which the admin sees in the group - for
    the race-report name check. Never fails the save: the login is already
    stored, and a member without a label is only logged by ID, as before.
    """
    admin = settings.telegram_admin_id()
    if had_login:
        if admin and await pseudonyms.label_for(user_id) is None:
            await sms_service.send_sms(
                admin,
                f"{_who(user)} re-saved their Walden login but has no pseudonym yet, so none "
                "was assigned automatically - they may already have one in the registry. Map "
                "it with: scripts/add_walden_credential.py set-pseudonym <their requester id> "
                '"Member X"',
                channel="telegram",
            )
        return
    try:
        label, new = await pseudonyms.assign(user_id)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning(f"Could not assign a pseudonym: {type(exc).__name__}")
        return
    if not new or not admin:
        return
    await sms_service.send_sms(
        admin,
        f"{_who(user)} connected a Walden login and is {label} in logs and race reports. Add "
        f"them to MEMBER_PSEUDONYM_REGISTRY and MEMBER_PSEUDONYM_LABELS as {label}, with "
        "their name forms and handle, so the race-report name check knows them.",
        channel="telegram",
    )


def _who(user: dict[str, object]) -> str:
    """How to name this member to the admin: first name and @handle."""
    first_name = user.get("first_name")
    username = user.get("username")
    return (
        " ".join(
            part
            for part in (
                first_name if isinstance(first_name, str) else "",
                f"(@{username})" if isinstance(username, str) and username else "",
            )
            if part
        )
        or "A member"
    )


async def _warn_if_at_racer_ceiling() -> None:
    """Tell the admin when stored logins reach the number of racer tasks.

    Each stored login is a requester the 6:30 race may have to run at once, and
    the racer starts racer_max_requesters tasks. Past that, extra requesters
    are raced late. Self-service means the count can grow without the admin
    doing anything, so the admin is told rather than left to notice a late race.
    """
    admin = settings.telegram_admin_id()
    if not admin:
        return
    stored = await credential_service.count()
    ceiling = settings.racer_max_requesters
    if stored >= ceiling:
        await sms_service.send_sms(
            admin,
            f"{stored} members now have Walden logins connected, and the racer starts "
            f"{ceiling} tasks (racer_max_requesters). If more than {ceiling} race on the same "
            "morning, the extras race late. Raise racer_max_requesters in "
            "terraform/variables.tf.",
            channel="telegram",
        )
