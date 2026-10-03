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
