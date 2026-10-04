"""Tests for recovering from a stored Walden login that stops working (issue #244)."""

from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from selenium.common.exceptions import WebDriverException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.database import Base
from app.models.schemas import BookingStatus, TeeTimeBooking, TeeTimeRequest
from app.providers.base import BatchBookingItemResult, BatchBookingResult, BookingResult
from app.providers.walden_http_login import LOGIN_REJECTED_MESSAGE
from app.providers.walden_provider import WaldenGolfProvider
from app.services.booking_service import BookingService
from app.services.credential_service import WaldenCredentialInvalidError, credential_service

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_PAGE = (FIXTURES / "walden_login_page.html").read_text(encoding="utf-8")
REJECTED_PAGE = (FIXTURES / "walden_login_rejected.html").read_text(encoding="utf-8")
LOGIN_URL = "https://www.waldengolf.com/web/pages/login"
MEMBER = "5550001234"


class FakeDriver:
    def __init__(self, page: str | None, url: str = LOGIN_URL) -> None:
        self._page = page
        self._url = url

    @property
    def page_source(self) -> str:
        if self._page is None:
            raise WebDriverException("chrome died")
        return self._page

    @property
    def current_url(self) -> str:
        return self._url


class TestClassifyingAFailedChromeLogin:
    """Same rule as the HTTP check (#241): only Walden's own rejection counts."""

    def test_walden_rejection_page(self) -> None:
        provider = WaldenGolfProvider("m", "p")
        assert provider._walden_rejected_login(FakeDriver(REJECTED_PAGE)) is True

    def test_login_page_without_an_error_is_not_a_rejection(self) -> None:
        provider = WaldenGolfProvider("m", "p")
        assert provider._walden_rejected_login(FakeDriver(LOGIN_PAGE)) is False

    def test_a_dead_driver_is_not_a_rejection(self) -> None:
        provider = WaldenGolfProvider("m", "p")
        assert provider._walden_rejected_login(FakeDriver(None)) is False

    def test_failure_message_follows_the_classification(self) -> None:
        provider = WaldenGolfProvider("m", "p")
        assert provider.login_failure_message() == "Failed to log in to Walden Golf"
        provider._login_rejected = True
        assert provider.login_failure_message() == LOGIN_REJECTED_MESSAGE

    def test_the_rejection_message_tells_the_member_what_to_do(self) -> None:
        assert "/login" in LOGIN_REJECTED_MESSAGE
        assert len(LOGIN_REJECTED_MESSAGE) <= 200  # MEMBER_ERROR_MAX_LEN


@pytest_asyncio.fixture
async def store(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.services.credential_service.AsyncSessionLocal", session_local)
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "credential_kms_key", "")
    yield
    await engine.dispose()


class TestTheInvalidMark:
    async def test_marking_is_refused_at_race_time(self, store: None) -> None:
        await credential_service.set_credentials(MEMBER, "M-1", "old-password")
        assert await credential_service.mark_invalid(MEMBER) is True

        with pytest.raises(WaldenCredentialInvalidError, match="/login"):
            await credential_service.require_credentials(MEMBER)

    async def test_a_new_login_clears_the_mark(self, store: None) -> None:
        await credential_service.set_credentials(MEMBER, "M-1", "old-password")
        await credential_service.mark_invalid(MEMBER)

        await credential_service.set_credentials(MEMBER, "M-1", "new-password")

        owner = await credential_service.get_owner(MEMBER)
        assert owner is not None and owner.invalid_since is None
        assert (await credential_service.require_credentials(MEMBER)).password == "new-password"

    async def test_the_first_rejection_date_is_kept(self, store: None) -> None:
        await credential_service.set_credentials(MEMBER, "M-1", "pw")
        await credential_service.mark_invalid(MEMBER)
        first = (await credential_service.get_owner(MEMBER)).invalid_since  # type: ignore[union-attr]
        await credential_service.mark_invalid(MEMBER)
        assert (await credential_service.get_owner(MEMBER)).invalid_since == first  # type: ignore[union-attr]

    async def test_marking_with_no_stored_login(self, store: None) -> None:
        assert await credential_service.mark_invalid(MEMBER) is False


def _booking(booking_id: str) -> TeeTimeBooking:
    return TeeTimeBooking(
        id=booking_id,
        phone_number=MEMBER,
        request=TeeTimeRequest(requested_date=date(2026, 10, 10), requested_time=time(8, 0)),
        status=BookingStatus.SCHEDULED,
    )


def _provider_answering(error_message: str) -> MagicMock:
    async def book_multiple(target_date, requests, execute_at=None) -> BatchBookingResult:  # type: ignore[no-untyped-def]
        return BatchBookingResult(
            results=[
                BatchBookingItemResult(
                    booking_id=r.booking_id,
                    result=BookingResult(success=False, error_message=error_message),
                )
                for r in requests
            ],
            total_failed=len(requests),
        )

    provider = MagicMock()
    provider.book_multiple_tee_times = AsyncMock(side_effect=book_multiple)
    return provider


class TestTheRace:
    async def test_a_rejected_login_is_marked_and_reported(self, store: None) -> None:
        await credential_service.set_credentials(MEMBER, "M-1", "stale")
        service = BookingService()
        provider = _provider_answering(LOGIN_REJECTED_MESSAGE)
        service.set_reservation_provider(provider)
        booking = _booking("b1")

        with patch("app.services.booking_service.database_service") as db:
            db.update_booking = AsyncMock()
            results = dict(await service.execute_bookings_batch([booking]))

        assert results["b1"].error_message == LOGIN_REJECTED_MESSAGE
        assert booking.status == BookingStatus.FAILED
        owner = await credential_service.get_owner(MEMBER)
        assert owner is not None and owner.invalid_since is not None

    async def test_the_next_race_never_reaches_walden(self, store: None) -> None:
        """Each try with a rejected login is a failed login on the member's account."""
        await credential_service.set_credentials(MEMBER, "M-1", "stale")
        await credential_service.mark_invalid(MEMBER)
        service = BookingService()
        provider = _provider_answering("should not be called")
        service.set_reservation_provider(provider)
        booking = _booking("b2")

        with patch("app.services.booking_service.database_service") as db:
            db.update_booking = AsyncMock()
            results = dict(await service.execute_bookings_batch([booking]))

        provider.book_multiple_tee_times.assert_not_awaited()
        assert results["b2"].success is False
        # The member-facing reason, not "Could not resolve booking credentials".
        assert (results["b2"].error_message or "").startswith("Not attempted: Walden rejected")
        assert "/login" in (results["b2"].error_message or "")

    async def test_a_login_that_merely_failed_is_not_marked(self, store: None) -> None:
        """A timeout or a changed page must not lock the member out of booking."""
        await credential_service.set_credentials(MEMBER, "M-1", "fine")
        service = BookingService()
        service.set_reservation_provider(_provider_answering("Failed to log in to Walden Golf"))

        with patch("app.services.booking_service.database_service") as db:
            db.update_booking = AsyncMock()
            await service.execute_bookings_batch([_booking("b3")])

        owner = await credential_service.get_owner(MEMBER)
        assert owner is not None and owner.invalid_since is None


class TestNewBookings:
    async def test_refused_while_the_login_is_marked(self, store: None) -> None:
        await credential_service.set_credentials(MEMBER, "M-1", "stale")
        await credential_service.mark_invalid(MEMBER)
        service = BookingService()
        request = TeeTimeRequest(requested_date=date(2026, 10, 10), requested_time=time(8, 0))

        with pytest.raises(ValueError, match="Send /login"):
            await service.create_booking(MEMBER, request)


class TestStatus:
    async def test_status_says_the_login_was_rejected(self, store: None) -> None:
        from app.services.member_setup import handle_command

        await credential_service.set_credentials(
            MEMBER, "M-1", "stale", verified_at=datetime(2026, 9, 1)
        )
        await credential_service.mark_invalid(MEMBER)

        reply = await handle_command("status", "", MEMBER, True, "teetimebot")

        assert "Walden rejected your saved login" in reply.text
        assert "/login" in reply.text
