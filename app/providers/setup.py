"""Which reservation provider a process books through.

Shared by the service (``app/main.py``) and the racer job (``app/racer/``) so
the two cannot drift: a racer container that installed the mock while the
service installed the real provider would report fake grants to members.
"""

import logging

from app.config import settings
from app.providers.walden_provider import MockWaldenProvider, WaldenGolfProvider
from app.services.booking_service import BookingService

logger = logging.getLogger(__name__)


def install_reservation_provider(service: BookingService) -> None:
    """Install the real per-requester provider in a deployment, the mock otherwise.

    WALDEN_MEMBER_NUMBER/WALDEN_PASSWORD no longer log anything in: every
    booking runs under the requester's own stored login, and a requester without
    one is refused rather than borrowing this account. They survive only as the
    signal for "this is a real deployment, not a laptop", which is why the class
    - not an instance built from them - is what gets installed. Retiring the two
    secrets is a follow-up: dropping them from terraform's list would have
    Terraform delete them from Secret Manager, so that wants its own change.
    """
    if settings.walden_member_number and settings.walden_password:
        logger.info("Walden Golf configured - bookings run under each requester's own login")
        service.set_reservation_provider_factory(WaldenGolfProvider)
    else:
        logger.warning(
            "Walden Golf credentials not configured - using MockWaldenProvider. "
            "Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD for real bookings."
        )
        service.set_reservation_provider(MockWaldenProvider())
