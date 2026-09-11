"""
Admin proxy booking: one designated Telegram account booking *as* a friend.

Dax needs to book, re-book, or test a tee time under a specific friend's
Walden account without being that friend - to diagnose a booking failure, to
verify a newly-added credential actually works, or just to help someone out
(issue #185). Exactly one Telegram ID may do this
(settings.telegram_admin_user_id); every other user keeps booking only for
themselves.

Two rules shape everything here:

  * The admin account has no Walden login of its own, and must never fall back
    to the shared global account. Every proxy booking is attributed to a real
    friend or it does not happen - see is_proxy_admin, which the credential
    lookup uses to refuse the admin outright.
  * The target is resolved from the credential store's own ``name`` /
    ``telegram_username`` fields, and an unresolved or ambiguous target fails
    loudly. Guessing which friend was meant would book under the wrong
    person's membership, which is worse than not booking at all.

This module holds only the parts with no I/O: recognizing the "for @X" clause
and deciding who the admin is. The database side lives in
app/services/credential_service.py, and the conversation side in
app/services/booking_service.py.
"""

import re

from app.config import settings

# "for @alex book 9/12 at 8a", "For Alex, book ...", "for alex: book ...".
#
# Anchored at the start rather than matched anywhere in the message: "for" is
# an ordinary English word that a booking request is very likely to contain
# already ("book 9/12 at 8a for 4 players"), and a floating match would read
# "for 4 players" as a target named "4". A leading clause is unambiguous, it is
# how the issue's examples are written, and it mirrors strip_bot_prefix, which
# also only peels addressing off the front.
#
# The "@" is optional because a friend's stored name is not a Telegram handle
# and typing "@" in front of it is unnatural; the separator after the target is
# optional too, so both "for @alex book ..." and "for Alex, book ..." work.
_PROXY_TARGET_RE = re.compile(
    r"""^\s*for\s+          # the clause keyword
        @?                  # "@" optional: a name is not a handle
        (?P<target>[^\s,:]+) # the target itself - one word, no separators
        \s*[,:]?\s*         # an optional separator the admin may have typed
        (?P<rest>.*)$       # the actual request
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def is_proxy_admin(requester: str) -> bool:
    """Whether this requester identity is the single configured proxy admin.

    ``requester`` is the same identity string carried in ``phone_number``
    throughout the app, which for Telegram is the numeric user ID.

    Unset TELEGRAM_ADMIN_USER_ID means nobody, not everybody: the whole
    feature fails closed, the same way the allowlist does.
    """
    admin = settings.telegram_admin_id()
    return bool(admin) and requester.strip() == admin


def normalize_target(target: str) -> str:
    """Fold a typed "@X" down to the form stored names are compared in.

    Case-insensitive with an optional leading "@", which resolves the issue's
    open question in the forgiving direction: Telegram itself renders handles
    case-preserved but matches them case-insensitively, and an admin typing a
    friend's name from memory should not have to reproduce its capitalization.
    Forgiving matching cannot book under the wrong account here - an input that
    folds onto two different friends is reported as ambiguous rather than
    resolved to one of them.
    """
    return target.strip().lstrip("@").strip().casefold()


def split_proxy_target(text: str) -> tuple[str | None, str]:
    """Peel a leading "for @X" clause off a message.

    Returns ``(target, remaining_text)``, with ``target`` None when the message
    carries no such clause - in which case the text is handed back untouched
    for the normal parser to read.

    Deliberately a plain grammar rather than another job for the LLM parser:
    this decides whose Walden membership a booking runs under, and the parser
    is a floating "latest" model alias that has already been retired out from
    under this project once. A regex that fails to match sends the admin down
    the "for which user?" path, which is recoverable; a model that hallucinates
    a target books under someone else's account, which is not.
    """
    match = _PROXY_TARGET_RE.match(text)
    if not match:
        return None, text

    target = match.group("target").strip()
    rest = match.group("rest").strip()

    # "for" with nothing usable after it is not a proxy clause. Handing back
    # the original text lets the message be read as an ordinary request.
    if not normalize_target(target):
        return None, text

    return target, rest
