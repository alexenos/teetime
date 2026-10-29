"""Suite-wide fixtures."""

from collections.abc import Iterator

import pytest

from app.config import settings
from app.services.sms_service import sms_service

# Settings reads .env, so a developer machine with live credentials would hand
# them to every test. A test that reached the real sms_service then delivered:
# on 2026-10-29 a full local run DMed the maintainer two "I don't know whether
# your tee time ... was booked" messages for a fixture booking (#381).
_MESSAGING_CREDENTIALS = (
    "discord_bot_token",
    "discord_user_id",
    "discord_channel_id",
    "telegram_bot_token",
    "twilio_account_sid",
    "twilio_auth_token",
    "twilio_phone_number",
)


@pytest.fixture(autouse=True)
def _no_live_messaging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Blank every messaging credential, so an unmocked send goes to Mock.

    A test that needs a token sets its own with monkeypatch, which runs after
    this. The provider cache is cleared on both sides, so a provider built from
    one test's credentials is not reused by the next.
    """
    for name in _MESSAGING_CREDENTIALS:
        monkeypatch.setattr(settings, name, "")
    sms_service._providers.clear()
    yield
    sms_service._providers.clear()
