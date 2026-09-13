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
"""

import logging

WIRE_LOGGERS = ("selenium", "urllib3", "websockets", "httpcore")


def silence_wire_loggers() -> None:
    """Pin the WebDriver wire loggers at WARNING.

    Call this after any ``logging.basicConfig``, which would otherwise reset
    them. Never gate it on LOG_LEVEL: the whole point is that it holds when the
    application itself is at DEBUG.
    """
    for name in WIRE_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
