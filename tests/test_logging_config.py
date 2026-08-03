"""Tests for logging configuration.

These guard a credential leak rather than a formatting preference.
selenium.webdriver.remote.remote_connection logs every WebDriver command
payload at DEBUG, including the send_keys body that fills the Walden login
form. The muzzle used to be conditional on the app's own log level, so running
with LOG_LEVEL=DEBUG (which is how this runs in production, to get
BOOKING_DEBUG output) wrote the member number and password to Cloud Logging in
cleartext on every booking run.
"""

import logging
from collections.abc import Iterator

import pytest

from app.main import configure_logging

# Loggers that must never emit below WARNING, whatever LOG_LEVEL says.
WIRE_LOGGERS = ["selenium", "urllib3", "websockets", "httpcore"]


@pytest.fixture(autouse=True)
def isolated_logging() -> Iterator[None]:
    """Reset the loggers under test, then restore them afterwards.

    app.main calls configure_logging() at import time, which already pins these
    to WARNING. Without clearing them first a test would observe that leftover
    state rather than what the call under test actually did - and would pass
    even against a configure_logging() that never touched them.

    Handlers are saved too: configure_logging() calls basicConfig(force=True),
    which detaches every existing root handler - including pytest's capture
    handler. Restoring only levels would leave later tests in the session
    running against a root logger this fixture had quietly rewired.
    """
    names = [*WIRE_LOGGERS, "app", ""]
    saved_levels = {name: logging.getLogger(name).level for name in names}
    root = logging.getLogger()
    saved_handlers = root.handlers[:]

    for name in names:
        logging.getLogger(name).setLevel(logging.NOTSET)
    try:
        yield
    finally:
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        root.handlers[:] = saved_handlers


@pytest.mark.parametrize("log_level", ["DEBUG", "INFO", "WARNING"])
@pytest.mark.parametrize("wire_logger", WIRE_LOGGERS)
def test_wire_loggers_never_emit_below_warning(
    monkeypatch: pytest.MonkeyPatch, log_level: str, wire_logger: str
) -> None:
    """Wire loggers stay at WARNING even when the app is at DEBUG."""
    monkeypatch.setattr("app.main.settings.log_level", log_level)

    configure_logging()

    assert logging.getLogger(wire_logger).level == logging.WARNING


def test_selenium_command_payloads_are_not_logged_at_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact logger that leaked credentials is silenced at DEBUG.

    remote_connection is a child of the "selenium" logger, so setting the
    parent to WARNING must suppress its DEBUG records through inheritance.
    """
    monkeypatch.setattr("app.main.settings.log_level", "DEBUG")

    configure_logging()

    leaky = logging.getLogger("selenium.webdriver.remote.remote_connection")
    assert not leaky.isEnabledFor(logging.DEBUG)
    assert not leaky.isEnabledFor(logging.INFO)


def test_app_loggers_still_honour_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silencing the wire loggers must not cost us BOOKING_DEBUG output."""
    monkeypatch.setattr("app.main.settings.log_level", "DEBUG")

    configure_logging()

    assert logging.getLogger("app.providers.walden_provider").isEnabledFor(logging.DEBUG)
