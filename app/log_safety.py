"""Loggers that must never emit below WARNING, whatever LOG_LEVEL says.

``selenium.webdriver.remote.remote_connection`` logs every command payload at
DEBUG, and that includes the ``send_keys`` body used to fill the Walden login
form. Production runs with ``LOG_LEVEL=DEBUG`` - that is how BOOKING_DEBUG
output is obtained - so leaving these loggers alone writes the member number
and password to Cloud Logging in cleartext.

This lives outside ``app.main`` because every entry point that drives a browser
needs it, and importing ``app.main`` from the observer job would pull in the
whole FastAPI application. The observer shipped with its own ``basicConfig``
and without this guard, and leaked a member number and password on its first
run (2026-09-13).

``httpx`` is the exception: it is redacted, not silenced. Its INFO line
(``HTTP Request: POST <url> "HTTP/1.1 200 OK"``) is evidence the race report
reads - the gap between it and ``Reserve k -> ...`` is how post-response cost is
reconstructed (see ``.claude/skills/race-report/SKILL.md``). But the Telegram Bot
API puts the bot token in the URL path, so unredacted, the same line writes the
token to Cloud Logging on every Telegram call - which it did, in production.
"""

import logging
import re

WIRE_LOGGERS = ("selenium", "urllib3", "websockets", "httpcore")

# The Bot API URL is https://api.telegram.org/bot<token>/<method>. Matched on
# the host as well as "/bot" so no other URL's path is rewritten.
_TELEGRAM_TOKEN_IN_URL = re.compile(r"(api\.telegram\.org/bot)[^/\s\"']+")
TELEGRAM_TOKEN_REDACTED = "<redacted>"


def redact_telegram_token(text: str) -> str:
    """Replace the bot token in any Telegram Bot API URL within ``text``."""
    return _TELEGRAM_TOKEN_IN_URL.sub(rf"\g<1>{TELEGRAM_TOKEN_REDACTED}", text)


class RedactTelegramToken(logging.Filter):
    """Rewrites a record's message so a Telegram bot token never reaches a handler.

    The URL arrives as a format argument, not in ``msg``, so the message is
    rendered, redacted and stored back with ``args`` cleared. Records without a
    Telegram URL pass through untouched, keeping their original ``msg``/``args``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_telegram_token(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


# One shared instance: Logger.addFilter ignores an instance already present, so
# repeated silence_wire_loggers() calls (every entry point, and tests) do not
# stack copies.
_REDACT_TELEGRAM_TOKEN = RedactTelegramToken()


class MemberPseudonyms(logging.Filter):
    """Rewrites each known member's Telegram ID to their label (issue #256).

    Race reports are public and quote log lines; a Telegram ID identifies a
    person (race-report skill §8a). The service, racer and observer load every
    stored label at startup (app.services.pseudonyms.refresh_log_filter), and
    this replaces each ID, as a whole number, with "<Member C>" in every record
    - so the ~dozen log calls that name a requester, and any added later, never
    write the ID.

    Attached to the root logger's handlers rather than to loggers: a logger's
    filters only see records logged on that logger, and these come from
    everywhere. IDs of people who are not members (a stranger messaging the bot)
    are not known here and pass through, as before.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pattern: re.Pattern[str] | None = None
        self._labels: dict[str, str] = {}

    def set(self, mapping: dict[str, str]) -> None:
        """Merge these labels into the ones already known. Never removes one.

        Labels are never changed or reused once assigned, so the mapping only
        grows - which makes merging safe, and replacing unsafe: two refreshes
        overlapping could otherwise let an older database snapshot overwrite a
        newer one and drop a member, whose ID would then reach the logs.
        """
        # Only plausible Telegram IDs - long digit runs - so no short number in
        # a log line (a count, a port, a millisecond figure) is ever rewritten.
        added = {i: label for i, label in mapping.items() if i.isdigit() and len(i) >= 6}
        merged = {**self._labels, **added}
        ids = sorted(merged, key=len, reverse=True)
        self._labels = {i: merged[i] for i in ids}
        self._pattern = re.compile(r"(?<!\d)(" + "|".join(ids) + r")(?!\d)") if ids else None

    def clear(self) -> None:
        """Forget every label. For tests."""
        self._labels = {}
        self._pattern = None

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pattern is None:
            return True
        message = record.getMessage()
        rewritten = self._pattern.sub(lambda m: f"<{self._labels[m.group(1)]}>", message)
        if rewritten != message:
            record.msg = rewritten
            record.args = None
        return True


_MEMBER_PSEUDONYMS = MemberPseudonyms()


def set_member_pseudonyms(mapping: dict[str, str]) -> None:
    """Add members' labels, keyed by Telegram ID, to the log filter. Merges."""
    _MEMBER_PSEUDONYMS.set(mapping)


def clear_member_pseudonyms() -> None:
    """Forget every label the log filter knows. For tests."""
    _MEMBER_PSEUDONYMS.clear()


def silence_wire_loggers() -> None:
    """Pin the WebDriver wire loggers at WARNING, and redact httpx's URLs.

    Call this after any ``logging.basicConfig``, which would otherwise reset
    them. Never gate it on LOG_LEVEL: the whole point is that it holds when the
    application itself is at DEBUG.
    """
    for name in WIRE_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # A logger's filters see only records logged on that logger itself, and
    # httpx logs every request on "httpx" - not on a child.
    logging.getLogger("httpx").addFilter(_REDACT_TELEGRAM_TOKEN)
    # Member Telegram IDs become their labels (#256). On the root handlers,
    # because records from every logger pass through them.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_MEMBER_PSEUDONYMS)
