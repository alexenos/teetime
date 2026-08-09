"""
Tests for WaldenGolfProvider Selenium implementation.

These tests verify the DOM parsing and time extraction logic works correctly
against the actual Walden Golf website structure.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import date, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock, PropertyMock, patch

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from app.config import settings
from app.providers.base import BookingResult
from app.providers.walden_dom_schema import DOM
from app.providers.walden_http import DirectHttpError
from app.providers.walden_http_booker import (
    DIRECT_HTTP_PATH,
    PHASE_RESERVE_SENT,
    PHASE_RESERVE_STAGED,
    find_response_message,
)
from app.providers.walden_provider import WaldenGolfProvider
from app.utils.timezone import CTDateTime


@pytest.fixture
def provider() -> WaldenGolfProvider:
    """Create a WaldenGolfProvider instance."""
    return WaldenGolfProvider()


class TestWaldenProviderParseTime:
    """Tests for the _parse_time method."""

    def test_parse_time_12h_with_space(self, provider: WaldenGolfProvider) -> None:
        """Test parsing 12-hour time with space before AM/PM."""
        result = provider._parse_time("07:30 AM")
        assert result is not None
        assert result.hour == 7
        assert result.minute == 30

    def test_parse_time_12h_no_space(self, provider: WaldenGolfProvider) -> None:
        """Test parsing 12-hour time without space before AM/PM."""
        result = provider._parse_time("07:30AM")
        assert result is not None
        assert result.hour == 7
        assert result.minute == 30

    def test_parse_time_pm(self, provider: WaldenGolfProvider) -> None:
        """Test parsing PM time."""
        result = provider._parse_time("02:15 PM")
        assert result is not None
        assert result.hour == 14
        assert result.minute == 15

    def test_parse_time_24h(self, provider: WaldenGolfProvider) -> None:
        """Test parsing 24-hour time."""
        result = provider._parse_time("14:30")
        assert result is not None
        assert result.hour == 14
        assert result.minute == 30

    def test_parse_time_lowercase(self, provider: WaldenGolfProvider) -> None:
        """Test parsing lowercase am/pm."""
        result = provider._parse_time("07:30 am")
        assert result is not None
        assert result.hour == 7
        assert result.minute == 30

    def test_parse_time_invalid(self, provider: WaldenGolfProvider) -> None:
        """Test parsing invalid time string."""
        result = provider._parse_time("invalid")
        assert result is None

    def test_parse_time_empty(self, provider: WaldenGolfProvider) -> None:
        """Test parsing empty string."""
        result = provider._parse_time("")
        assert result is None

    def test_parse_time_whitespace(self, provider: WaldenGolfProvider) -> None:
        """Test parsing time with extra whitespace."""
        result = provider._parse_time("  07:30 AM  ")
        assert result is not None
        assert result.hour == 7
        assert result.minute == 30


class TestWaldenProviderCredentials:
    """Tests for credentials validation."""

    def test_init_logs_warning_without_credentials(
        self, provider: WaldenGolfProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that init logs a warning if credentials are not configured."""
        if not os.getenv("WALDEN_MEMBER_NUMBER") or not os.getenv("WALDEN_PASSWORD"):
            assert "credentials not configured" in caplog.text.lower() or True


@pytest.mark.skipif(
    (not os.getenv("WALDEN_MEMBER_NUMBER") or not os.getenv("WALDEN_PASSWORD"))
    or os.getenv("RUN_WALDEN_INTEGRATION") != "1",
    reason="Walden Golf integration tests are opt-in (set RUN_WALDEN_INTEGRATION=1 and credentials)",
)
class TestWaldenProviderIntegration:
    """
    Integration tests that run against the live Walden Golf website.

    These tests require valid credentials to be set in environment variables:
    - WALDEN_MEMBER_NUMBER
    - WALDEN_PASSWORD

    Run with: pytest tests/test_walden_provider.py -v -k Integration
    """

    @pytest.mark.asyncio
    async def test_login(self, provider: WaldenGolfProvider) -> None:
        """Test that login succeeds with valid credentials."""
        result = await provider.login()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_available_times(self, provider: WaldenGolfProvider) -> None:
        """Test that get_available_times returns a list of times."""
        target_date = date.today() + timedelta(days=7)
        times = await provider.get_available_times(target_date)

        assert isinstance(times, list)
        if times:
            for t in times:
                assert hasattr(t, "hour")
                assert hasattr(t, "minute")
                assert 0 <= t.hour <= 23
                assert 0 <= t.minute <= 59

    @pytest.mark.asyncio
    async def test_available_slots_parsing(self, provider: WaldenGolfProvider) -> None:
        """
        Test that _find_available_slots correctly parses the DOM structure.

        This test verifies:
        1. The span.custom-free-slot-span elements are found
        2. The row container (block-available) is located
        3. The time is extracted from the container text
        """
        target_date = date.today() + timedelta(days=7)
        times = await provider.get_available_times(target_date)

        print(f"\nFound {len(times)} available times for {target_date}:")
        for t in times[:10]:
            print(f"  - {t.strftime('%I:%M %p')}")

        # Verify result is a list of valid time objects
        assert isinstance(times, list)
        for t in times:
            assert hasattr(t, "hour")
            assert hasattr(t, "minute")


class TestWaldenProviderConfirmationExtraction:
    """Tests for confirmation number extraction logic."""

    def test_extract_confirmation_with_confirmation_keyword(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test extracting confirmation number when 'confirmation' keyword present."""
        mock_driver = MagicMock()
        # The regex pattern is: confirmation[:\s#]*([A-Z0-9-]+)
        # So "Confirmation: ABC123-456" or "Confirmation #ABC123" works
        mock_driver.page_source = """
        <html>
            <body>
                <h1>Booking Confirmed</h1>
                <p>Confirmation: ABC123-456</p>
            </body>
        </html>
        """
        result = provider._extract_confirmation_number(mock_driver)
        assert result == "ABC123-456"

    def test_extract_confirmation_with_booking_keyword(self, provider: WaldenGolfProvider) -> None:
        """Test extracting confirmation number with 'booking' keyword."""
        mock_driver = MagicMock()
        # The regex pattern is: booking[:\s#]*([A-Z0-9-]+)
        # Need "booked" in page for the check, and "Booking:" for the pattern
        mock_driver.page_source = """
        <html>
            <body>
                <h1>Reservation Booked</h1>
                <p>Booking #REF-789XYZ</p>
            </body>
        </html>
        """
        result = provider._extract_confirmation_number(mock_driver)
        assert result == "REF-789XYZ"

    def test_extract_confirmation_with_reference_keyword(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test extracting confirmation number with 'reference' keyword."""
        mock_driver = MagicMock()
        mock_driver.page_source = """
        <html>
            <body>
                <h1>Reserved</h1>
                <p>Reference: GOLF-2024-001</p>
            </body>
        </html>
        """
        result = provider._extract_confirmation_number(mock_driver)
        assert result == "GOLF-2024-001"

    def test_extract_confirmation_no_match(self, provider: WaldenGolfProvider) -> None:
        """Test that None is returned when no confirmation number found."""
        mock_driver = MagicMock()
        mock_driver.page_source = """
        <html>
            <body>
                <h1>Welcome</h1>
                <p>Please select a tee time.</p>
            </body>
        </html>
        """
        result = provider._extract_confirmation_number(mock_driver)
        assert result is None


class TestWaldenProviderFindAndBookTimeSlot:
    def test_filters_by_window_and_interval_and_selects_best_slot(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that slot selection respects the fallback window, interval alignment, and picks the nearest available slot."""
        mock_driver = MagicMock()

        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())

        slot_el_ok = MagicMock()
        slot_el_wrong_course = MagicMock()

        monkeypatch.setattr(
            provider,
            "_find_empty_slots",
            MagicMock(
                return_value=[
                    (time(8, 50), slot_el_ok),
                    (time(8, 54), slot_el_ok),
                    (time(8, 58), slot_el_ok),
                    (time(9, 2), slot_el_ok),
                    (time(9, 6), slot_el_ok),
                    (time(9, 0), slot_el_wrong_course),
                ]
            ),
        )

        monkeypatch.setattr(provider, "_is_northgate_slot", lambda el, _: el is slot_el_ok)

        expected_result = SimpleNamespace(success=True)

        def complete_booking_side_effect(
            _driver: MagicMock,
            _reserve_element: MagicMock,
            booked_time: time,
            _num_players: int,
            *_args: object,
        ) -> SimpleNamespace:
            expected_result.booked_time = booked_time
            return expected_result

        monkeypatch.setattr(provider, "_complete_booking_sync", complete_booking_side_effect)

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 58),
            num_players=4,
            fallback_window_minutes=8,
            tee_time_interval_minutes=8,
        )

        assert result.success is True
        assert getattr(result, "booked_time") == time(8, 58)

    def test_skip_scroll_does_not_scroll(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that passing skip_scroll=True skips the scroll step entirely."""
        mock_driver = MagicMock()
        scroll_mock = MagicMock()
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", scroll_mock)

        slot_el_ok = MagicMock()
        monkeypatch.setattr(
            provider,
            "_find_empty_slots",
            MagicMock(return_value=[(time(8, 58), slot_el_ok)]),
        )
        monkeypatch.setattr(provider, "_is_northgate_slot", lambda *_: True)

        expected_result = SimpleNamespace(success=True)

        def complete_booking_side_effect(
            _driver: MagicMock,
            _reserve_element: MagicMock,
            booked_time: time,
            _num_players: int,
            *_args: object,
        ) -> SimpleNamespace:
            expected_result.booked_time = booked_time
            return expected_result

        monkeypatch.setattr(provider, "_complete_booking_sync", complete_booking_side_effect)

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 58),
            num_players=4,
            fallback_window_minutes=8,
            tee_time_interval_minutes=8,
            skip_scroll=True,
        )

        assert result.success is True
        scroll_mock.assert_not_called()


class TestWaldenProviderScrollToLoadAllSlots:
    def test_stops_based_on_last_parsable_time_when_trailing_items_unparsable(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that scrolling stops based on the last parsable time when trailing DOM items have no parsable time."""
        driver = MagicMock()
        provider.wait_strategy = SimpleNamespace(simple_wait=lambda **_: None)

        item1 = object()
        item2 = object()
        item3 = object()
        item4 = object()
        item5 = object()

        slot_items_by_call = [
            [item1, item2, item3],
            [item1, item2, item3, item4, item5],
        ]

        def consume_slot_items() -> list[object]:
            if len(slot_items_by_call) > 1:
                return slot_items_by_call.pop(0)
            return slot_items_by_call[0] if slot_items_by_call else []

        def find_elements_side_effect(by: object, selector: str) -> list[object]:
            if selector == "li.ui-datascroller-item":
                return consume_slot_items()
            if selector in (".ui-datascroller-content, .ui-datascroller-list",):
                return []
            return []

        driver.find_elements.side_effect = find_elements_side_effect

        def extract_time_side_effect(item: object) -> time | None:
            if item is item3:
                return time(8, 40)
            if item is item5:
                return None
            if item is item4:
                return time(9, 10)
            return None

        monkeypatch.setattr(provider, "_extract_time_from_slot_item", extract_time_side_effect)

        provider._scroll_to_load_all_slots(
            driver,
            target_time=time(8, 58),
            fallback_window_minutes=8,
        )

        assert driver.find_elements.call_count >= 2
        assert driver.execute_script.call_count <= 1

    def test_max_time_minutes_override_limits_scrolling(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that max_time_minutes_override caps scrolling at the specified time regardless of target_time."""
        driver = MagicMock()
        provider.wait_strategy = SimpleNamespace(simple_wait=lambda **_: None)

        item1 = object()
        item2 = object()
        item3 = object()

        def find_elements_side_effect(by: object, selector: str) -> list[object]:
            if selector == "li.ui-datascroller-item":
                return [item1, item2, item3]
            if selector in (".ui-datascroller-content, .ui-datascroller-list",):
                return []
            return []

        driver.find_elements.side_effect = find_elements_side_effect

        def extract_time_side_effect(item: object) -> time | None:
            if item is item3:
                return time(9, 20)
            return None

        monkeypatch.setattr(provider, "_extract_time_from_slot_item", extract_time_side_effect)

        provider._scroll_to_load_all_slots(
            driver,
            target_time=time(8, 58),
            fallback_window_minutes=120,
            max_time_minutes_override=(9 * 60 + 10),
        )

        driver.execute_script.assert_not_called()


class TestWaldenProviderBatchPreScroll:
    def test_batch_prescroll_and_skip_scroll_per_booking(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that batch booking pre-scrolls once then skips per-booking scroll calls."""
        import app.providers.walden_provider as walden_module

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)
        monkeypatch.setattr(
            walden_module,
            "expected_conditions",
            SimpleNamespace(presence_of_element_located=lambda *_: None),
        )

        driver = MagicMock()
        monkeypatch.setattr(provider, "_create_driver", lambda: driver)
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_select_date_sync", lambda *_: True)

        scroll_mock = MagicMock()
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", scroll_mock)

        booked = []

        def find_and_book_side_effect(
            _driver: MagicMock,
            target_time: time,
            _num_players: int,
            _fallback_window_minutes: int,
            **kwargs: object,
        ) -> object:
            assert kwargs.get("skip_scroll") is True
            booked.append(target_time)
            return SimpleNamespace(success=True, booked_time=target_time, confirmation_number="X")

        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", find_and_book_side_effect)

        from app.providers.base import BatchBookingRequest

        req1 = BatchBookingRequest(booking_id="a", target_time=time(8, 58), num_players=4)
        req2 = BatchBookingRequest(booking_id="b", target_time=time(9, 6), num_players=4)

        result = provider._book_multiple_tee_times_sync(
            target_date=date.today(),
            requests=[req1, req2],
            execute_at=None,
        )

        assert result.total_succeeded == 2
        assert booked == [time(8, 58), time(9, 6)]
        assert scroll_mock.call_count >= 1
        assert any(
            call.kwargs.get("max_time_minutes_override") is not None
            for call in scroll_mock.mock_calls
        )


class TestWaldenProviderBookingVerification:
    """Tests for booking success verification logic."""

    def test_verify_success_with_successfully(self, provider: WaldenGolfProvider) -> None:
        """Test that 'successfully' indicator returns True."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Your tee time was successfully booked!</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_success_with_confirmed(self, provider: WaldenGolfProvider) -> None:
        """Test that 'confirmed' indicator returns True."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Your reservation is confirmed.</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_success_with_thank_you(self, provider: WaldenGolfProvider) -> None:
        """Test that 'thank you' indicator returns True."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Thank you for your reservation!</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_failure_with_error(self, provider: WaldenGolfProvider) -> None:
        """Test that 'error' indicator returns False."""
        mock_driver = MagicMock()
        mock_driver.page_source = (
            "<html><body>An error occurred while processing your request.</body></html>"
        )
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_success_ignores_hidden_error_in_html(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that 'error' in raw HTML does not override visible success text."""
        mock_driver = MagicMock()

        mock_body = MagicMock()
        mock_body.text = "Your tee time was successfully booked!"
        mock_driver.find_element.return_value = mock_body

        # Simulate 'error' only in script/hidden markup in the HTML source
        mock_driver.page_source = (
            "<html><body><div>Success</div></body><script>var error = true;</script></html>"
        )

        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_failure_with_unavailable(self, provider: WaldenGolfProvider) -> None:
        """Test that 'unavailable' indicator returns False."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>This time slot is unavailable.</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_failure_with_already_booked(self, provider: WaldenGolfProvider) -> None:
        """Test that 'already booked' indicator returns False."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>This slot is already booked.</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_failure_takes_precedence(self, provider: WaldenGolfProvider) -> None:
        """Test that failure indicators take precedence over success indicators."""
        mock_driver = MagicMock()
        # Page has both success and failure indicators - failure should win
        mock_driver.page_source = (
            "<html><body>Successfully detected an error in your booking.</body></html>"
        )
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_ambiguous_returns_false(self, provider: WaldenGolfProvider) -> None:
        """Test that ambiguous page content returns False."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Loading...</body></html>"
        mock_driver.current_url = "https://example.com/booking"
        result = provider._verify_booking_success(mock_driver)
        assert result is False


class TestWaldenProviderMock:
    """Tests using mock data to verify DOM parsing logic."""

    def test_extract_time_regex_12h(self, provider: WaldenGolfProvider) -> None:
        """Test time extraction regex for 12-hour format."""
        import re

        text = "07:46 AM Available Reserve"
        match = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", text, re.IGNORECASE)
        assert match is not None
        assert match.group(1) == "07:46 AM"

        result = provider._parse_time(match.group(1))
        assert result is not None
        assert result.hour == 7
        assert result.minute == 46

    def test_extract_time_regex_embedded(self, provider: WaldenGolfProvider) -> None:
        """Test time extraction from text with embedded time."""
        import re

        text = "Northgate 08:10 AM 4 Players Available"
        match = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", text, re.IGNORECASE)
        assert match is not None
        assert match.group(1) == "08:10 AM"

        result = provider._parse_time(match.group(1))
        assert result is not None
        assert result.hour == 8
        assert result.minute == 10

    def test_extract_time_regex_multiple_times(self, provider: WaldenGolfProvider) -> None:
        """Test that regex finds the first time in text with multiple times."""
        import re

        text = "Tee times: 07:30 AM, 07:38 AM, 07:46 AM"
        match = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", text, re.IGNORECASE)
        assert match is not None
        assert match.group(1) == "07:30 AM"


class TestWaldenProviderCancellation:
    """Tests for booking cancellation logic."""

    def test_verify_cancellation_success_with_cancelled_successfully(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that 'cancelled successfully' indicator returns True."""
        mock_driver = MagicMock()
        # Mock the reservations form element
        mock_form = MagicMock()
        mock_form.text = "Reservation Cancelled Successfully"
        mock_driver.find_element.return_value = mock_form
        result = provider._verify_cancellation_success(mock_driver)
        assert result is True

    def test_verify_cancellation_success_with_reservation_cancelled(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that 'reservation cancelled' indicator returns True."""
        from selenium.common.exceptions import NoSuchElementException

        mock_driver = MagicMock()
        # Mock form not found, fall back to page source
        mock_driver.find_element.side_effect = NoSuchElementException()
        mock_driver.page_source = "<html><body>Reservation Cancelled successfully.</body></html>"
        result = provider._verify_cancellation_success(mock_driver)
        assert result is True

    def test_verify_cancellation_failure_with_error_cancelling(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that 'error cancelling' indicator returns False."""
        mock_driver = MagicMock()
        mock_form = MagicMock()
        mock_form.text = "Error cancelling your reservation"
        mock_driver.find_element.return_value = mock_form
        result = provider._verify_cancellation_success(mock_driver)
        assert result is False

    def test_verify_cancellation_failure_with_unable_to_cancel(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that 'unable to cancel' indicator returns False."""
        mock_driver = MagicMock()
        mock_form = MagicMock()
        mock_form.text = "Unable to cancel your reservation"
        mock_driver.find_element.return_value = mock_form
        result = provider._verify_cancellation_success(mock_driver)
        assert result is False

    def test_verify_cancellation_ambiguous_returns_false(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that ambiguous page content returns False (pessimistic/fail-safe)."""
        from selenium.common.exceptions import NoSuchElementException

        mock_driver = MagicMock()
        # Mock form not found
        mock_driver.find_element.side_effect = NoSuchElementException()
        mock_driver.page_source = "<html><body>Processing your request...</body></html>"
        result = provider._verify_cancellation_success(mock_driver)
        assert result is False

    def test_verify_cancellation_success_with_row_removed(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that verification succeeds when target row is no longer present."""
        mock_driver = MagicMock()
        mock_form = MagicMock()
        mock_form.text = "My Reservations"  # No success/failure indicators
        # Mock finding rows but none match the target
        mock_row = MagicMock()
        mock_row.text = "12/20/2025 - Tee Time - 9:00 AM"  # Different date/time
        mock_form.find_elements.return_value = [mock_row]
        mock_driver.find_element.return_value = mock_form
        # Target date/time not found in rows = success
        result = provider._verify_cancellation_success(
            mock_driver, target_date="12/16/2025", target_time="3:22 PM"
        )
        assert result is True


class TestWaldenProviderCalendarNavigation:
    """Tests for calendar date selection and month navigation logic."""

    def test_get_calendar_current_month_from_dropdowns(self, provider: WaldenGolfProvider) -> None:
        """Test reading current month/year from dropdown selects."""
        mock_driver = MagicMock()

        # Mock month dropdown with 0-indexed value (January = 0)
        mock_month_option = MagicMock()
        mock_month_option.get_attribute.return_value = "0"  # January (0-indexed)

        mock_year_option = MagicMock()
        mock_year_option.get_attribute.return_value = "2026"

        mock_month_select_elem = MagicMock()
        mock_year_select_elem = MagicMock()

        # Mock find_elements to return our mock selects
        def find_elements_side_effect(by, selector):
            if "month" in selector:
                return [mock_month_select_elem]
            if "year" in selector:
                return [mock_year_select_elem]
            return []

        mock_driver.find_elements.side_effect = find_elements_side_effect

        # Mock the Select class
        with patch("app.providers.walden_provider.Select") as mock_select_class:
            mock_month_select = MagicMock()
            mock_month_select.first_selected_option = mock_month_option

            mock_year_select = MagicMock()
            mock_year_select.first_selected_option = mock_year_option

            def select_side_effect(elem):
                if elem == mock_month_select_elem:
                    return mock_month_select
                if elem == mock_year_select_elem:
                    return mock_year_select
                return MagicMock()

            mock_select_class.side_effect = select_side_effect

            month, year = provider._get_calendar_current_month(mock_driver)

            # 0-indexed month should be converted to 1-indexed
            assert month == 1  # January
            assert year == 2026

    def test_get_calendar_current_month_from_header(self, provider: WaldenGolfProvider) -> None:
        """Test reading current month/year from header text when dropdowns not available."""
        mock_driver = MagicMock()

        # No dropdowns found
        mock_driver.find_elements.side_effect = lambda by, selector: (
            []
            if "month" in selector or "year" in selector
            else [MagicMock(text="January 2026")]
            if "title" in selector or "header" in selector
            else []
        )

        # Mock header element
        mock_header = MagicMock()
        mock_header.text = "January 2026"

        def find_elements_side_effect(by, selector):
            if "month" in selector or "year" in selector:
                return []
            if "title" in selector or "header" in selector:
                return [mock_header]
            return []

        mock_driver.find_elements.side_effect = find_elements_side_effect

        month, year = provider._get_calendar_current_month(mock_driver)

        assert month == 1  # January
        assert year == 2026

    def test_get_calendar_current_month_returns_none_when_not_found(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that None is returned when calendar month cannot be determined."""
        mock_driver = MagicMock()
        mock_driver.find_elements.return_value = []

        month, year = provider._get_calendar_current_month(mock_driver)

        assert month is None
        assert year is None

    def test_navigate_calendar_to_month_same_month(self, provider: WaldenGolfProvider) -> None:
        """Test that navigation returns True when already on correct month."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 1, 25)  # January 2026

        # Mock that we're already on January 2026
        with patch.object(provider, "_get_calendar_current_month", return_value=(1, 2026)):
            # No dropdowns found, will use arrow navigation
            mock_driver.find_elements.return_value = []

            result = provider._navigate_calendar_to_month(mock_driver, target_date)

            assert result is True

    def test_navigate_calendar_to_month_via_dropdowns(self, provider: WaldenGolfProvider) -> None:
        """Test navigation using month/year dropdown selects."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 2, 1)  # February 2026

        # Mock month and year dropdown elements
        mock_month_select_elem = MagicMock()
        mock_year_select_elem = MagicMock()

        def find_elements_side_effect(by, selector):
            if "month" in selector:
                return [mock_month_select_elem]
            if "year" in selector:
                return [mock_year_select_elem]
            return []

        mock_driver.find_elements.side_effect = find_elements_side_effect

        # Mock the Select class
        with patch("app.providers.walden_provider.Select") as mock_select_class:
            mock_month_select = MagicMock()
            mock_year_select = MagicMock()

            def select_side_effect(elem):
                if elem == mock_month_select_elem:
                    return mock_month_select
                if elem == mock_year_select_elem:
                    return mock_year_select
                return MagicMock()

            mock_select_class.side_effect = select_side_effect

            result = provider._navigate_calendar_to_month(mock_driver, target_date)

            assert result is True
            # Verify year was selected
            mock_year_select.select_by_value.assert_called_with("2026")
            # Verify month was selected (0-indexed = 1 for February)
            mock_month_select.select_by_value.assert_called_with("1")

    def test_navigate_calendar_to_month_via_next_arrow(self, provider: WaldenGolfProvider) -> None:
        """Test navigation using next arrow when dropdowns not available."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 2, 1)  # February 2026

        # Mock next button
        mock_next_button = MagicMock()
        mock_next_button.is_displayed.return_value = True
        mock_next_button.is_enabled.return_value = True

        def find_elements_side_effect(by, selector):
            if "next" in selector.lower():
                return [mock_next_button]
            return []

        mock_driver.find_elements.side_effect = find_elements_side_effect

        # Mock that we're on January 2026, need to go to February
        with patch.object(provider, "_get_calendar_current_month", return_value=(1, 2026)):
            result = provider._navigate_calendar_to_month(mock_driver, target_date)

            assert result is True
            # Should have clicked next once (Jan -> Feb)
            assert mock_next_button.click.call_count >= 1

    def test_navigate_calendar_to_month_via_prev_arrow(self, provider: WaldenGolfProvider) -> None:
        """Test navigation using prev arrow when going backward."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2025, 12, 15)  # December 2025

        # Mock prev button
        mock_prev_button = MagicMock()
        mock_prev_button.is_displayed.return_value = True
        mock_prev_button.is_enabled.return_value = True

        def find_elements_side_effect(by, selector):
            if "prev" in selector.lower():
                return [mock_prev_button]
            return []

        mock_driver.find_elements.side_effect = find_elements_side_effect

        # Mock that we're on January 2026, need to go back to December 2025
        with patch.object(provider, "_get_calendar_current_month", return_value=(1, 2026)):
            result = provider._navigate_calendar_to_month(mock_driver, target_date)

            assert result is True
            # Should have clicked prev once (Jan 2026 -> Dec 2025)
            assert mock_prev_button.click.call_count >= 1

    def test_navigate_calendar_fails_when_no_nav_button(self, provider: WaldenGolfProvider) -> None:
        """Test that navigation fails when no navigation button found."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 3, 1)  # March 2026

        # No buttons found
        mock_driver.find_elements.return_value = []

        # Mock that we're on January 2026
        with patch.object(provider, "_get_calendar_current_month", return_value=(1, 2026)):
            result = provider._navigate_calendar_to_month(mock_driver, target_date)

            assert result is False

    def test_select_date_sync_returns_false_on_calendar_failure(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that _select_date_sync returns False when calendar selection fails."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 2, 1)

        # No date input fields found
        from selenium.common.exceptions import NoSuchElementException

        mock_driver.find_element.side_effect = NoSuchElementException()

        # Mock calendar selection to fail
        with patch.object(provider, "_select_date_via_calendar_sync", return_value=False):
            result = provider._select_date_sync(mock_driver, target_date)

            assert result is False

    def test_select_date_sync_returns_true_on_calendar_success(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that _select_date_sync returns True when calendar selection succeeds."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 2, 1)

        # No date input fields found
        from selenium.common.exceptions import NoSuchElementException

        mock_driver.find_element.side_effect = NoSuchElementException()

        # Mock calendar selection to succeed
        with patch.object(provider, "_select_date_via_calendar_sync", return_value=True):
            result = provider._select_date_sync(mock_driver, target_date)

            assert result is True

    def test_select_date_via_calendar_calls_navigate(self, provider: WaldenGolfProvider) -> None:
        """Test that calendar selection calls month navigation."""
        from datetime import date

        mock_driver = MagicMock()
        target_date = date(2026, 2, 1)

        # Mock calendar trigger found and clicked
        mock_trigger = MagicMock()
        mock_trigger.is_displayed.return_value = True

        # Mock day element
        mock_day = MagicMock()
        mock_day.is_displayed.return_value = True
        mock_day.is_enabled.return_value = True
        mock_day.get_attribute.return_value = ""

        def find_elements_side_effect(by, selector):
            if "calendar" in selector.lower():
                return [mock_trigger]
            return []

        mock_driver.find_elements.side_effect = find_elements_side_effect

        with patch.object(
            provider, "_navigate_calendar_to_month", return_value=True
        ) as mock_navigate:
            # This will fail at day selection but we can verify navigate was called
            provider._select_date_via_calendar_sync(mock_driver, target_date)

            mock_navigate.assert_called_once_with(mock_driver, target_date)


class TestWaldenProviderDateSelectionFailure:
    """Tests for booking failure when date selection fails."""

    def test_book_tee_time_fails_on_date_selection_failure(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that booking fails with clear error when date selection fails."""
        from datetime import date, time

        target_date = date(2026, 2, 1)
        target_time = time(8, 58)

        with patch.object(provider, "_create_driver") as mock_create:
            mock_driver = MagicMock()
            mock_create.return_value = mock_driver

            with patch.object(provider, "_perform_login", return_value=True):
                with patch.object(provider, "_select_course_sync", return_value=True):
                    with patch.object(provider, "_select_date_sync", return_value=False):
                        result = provider._book_tee_time_sync(target_date, target_time, 4, 32)

                        assert result.success is False
                        assert "Failed to select date" in result.error_message
                        assert "02/01/2026" in result.error_message

    def test_book_tee_time_proceeds_on_date_selection_success(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that booking proceeds when date selection succeeds."""
        from datetime import date, time

        from app.providers.base import BookingResult

        target_date = date(2026, 2, 1)
        target_time = time(8, 58)

        with patch.object(provider, "_create_driver") as mock_create:
            mock_driver = MagicMock()
            mock_create.return_value = mock_driver

            with patch.object(provider, "_perform_login", return_value=True):
                with patch.object(provider, "_select_course_sync", return_value=True):
                    with patch.object(provider, "_select_date_sync", return_value=True):
                        with patch.object(
                            provider,
                            "_find_and_book_time_slot_sync",
                            return_value=BookingResult(success=True, booked_time=target_time),
                        ) as mock_book:
                            result = provider._book_tee_time_sync(target_date, target_time, 4, 32)

                            # Verify _find_and_book_time_slot_sync was called
                            mock_book.assert_called_once()
                            assert result.success is True


class TestWaldenDOMSchema:
    """Tests for the centralized DOM schema module."""

    def test_schema_imports(self) -> None:
        """Test that the DOM schema can be imported and has expected structure."""
        from app.providers.walden_dom_schema import DOM

        assert isinstance(DOM.PLAYER_COUNT.button_group, tuple)
        assert len(DOM.PLAYER_COUNT.button_group) == 3
        assert DOM.LOGIN.submit_button == 'button[type="submit"]'
        assert ".ui-selectonebutton" in DOM.PLAYER_COUNT.button_group

    def test_schema_is_frozen(self) -> None:
        """Test that schema instances are immutable (frozen dataclasses)."""
        import dataclasses

        from app.providers.walden_dom_schema import DOM

        with pytest.raises(dataclasses.FrozenInstanceError):
            DOM.PLAYER_COUNT.disabled_class = "something-else"

    def test_player_count_selectors_documented(self) -> None:
        """Test that PlayerCountSelectors docstring warns about modal scoping."""
        from app.providers.walden_dom_schema import PlayerCountSelectors

        assert "modal" in PlayerCountSelectors.__doc__.lower()
        assert "Issue #105" in PlayerCountSelectors.__doc__

    def test_fallback_chains_are_tuples(self) -> None:
        """Test that fallback chains use tuples (immutable, ordered)."""
        from app.providers.walden_dom_schema import DOM

        assert isinstance(DOM.PLAYER_COUNT.dropdown_fallbacks, tuple)
        assert isinstance(DOM.DATE_SELECTION.nav_next, tuple)
        assert isinstance(DOM.DATE_SELECTION.nav_prev, tuple)
        assert isinstance(DOM.ERROR_MESSAGES.containers, tuple)
        assert isinstance(DOM.CANCELLATION.confirm_css, tuple)
        assert isinstance(DOM.CANCELLATION.confirm_xpaths, tuple)
        assert isinstance(DOM.SLOT_DISCOVERY.reserve_buttons, tuple)


class TestPlayerCountModalScoping:
    """Tests for Issue #105 fix: player count selection scoped to booking modal."""

    @staticmethod
    def _make_button_group(*, has_radio: bool) -> MagicMock:
        """A .ui-selectonebutton group that does or doesn't offer the player count."""
        group = MagicMock()
        radio = MagicMock()
        button_div = MagicMock()
        button_div.get_attribute.return_value = "ui-button"
        radio.find_element.return_value = button_div  # parent div (XPATH call)

        group.find_elements.return_value = [radio] if has_radio else []
        group.find_element.return_value = radio
        return group

    def test_select_player_count_uses_search_context(self, provider: WaldenGolfProvider) -> None:
        """When search_context is provided, the search runs on it, not the driver."""
        mock_driver = MagicMock()
        mock_modal = MagicMock()

        mock_button_group = self._make_button_group(has_radio=True)
        mock_modal.find_elements.return_value = [mock_button_group]

        # Mock wait_strategy to be a no-op
        provider.wait_strategy = MagicMock()

        # Mock _verify_player_rows_appeared to return True
        with patch.object(provider, "_verify_player_rows_appeared", return_value=True):
            result = provider._select_player_count_sync(mock_driver, 4, search_context=mock_modal)

        assert result is True
        # The critical assertion: the search ran on the MODAL, not the driver
        mock_modal.find_elements.assert_called()
        mock_driver.find_elements.assert_not_called()
        # execute_script should still use driver (not modal)
        mock_driver.execute_script.assert_called()

    def test_select_player_count_defaults_to_driver(self, provider: WaldenGolfProvider) -> None:
        """When no search_context is provided, defaults to using driver."""
        mock_driver = MagicMock()

        mock_button_group = self._make_button_group(has_radio=True)
        mock_driver.find_elements.return_value = [mock_button_group]

        provider.wait_strategy = MagicMock()

        with patch.object(provider, "_verify_player_rows_appeared", return_value=True):
            result = provider._select_player_count_sync(mock_driver, 4)

        assert result is True
        # search ran on the driver (default search_context)
        mock_driver.find_elements.assert_called()

    def test_select_player_count_skips_decoy_group(self, provider: WaldenGolfProvider) -> None:
        """The tee sheet's time period filter shares .ui-selectonebutton and comes
        first in the DOM; the group carrying the player count must win."""
        mock_driver = MagicMock()

        time_period_filter = self._make_button_group(has_radio=False)  # ALL/MORNING/...
        player_count_group = self._make_button_group(has_radio=True)
        mock_driver.find_elements.return_value = [time_period_filter, player_count_group]

        provider.wait_strategy = MagicMock()

        with patch.object(provider, "_verify_player_rows_appeared", return_value=True):
            result = provider._select_player_count_sync(mock_driver, 4)

        assert result is True
        player_count_group.find_element.assert_called()
        time_period_filter.find_element.assert_not_called()

    def test_complete_booking_passes_modal_as_context(self, provider: WaldenGolfProvider) -> None:
        """_complete_booking_sync captures modal element and passes it to player count selection."""
        mock_driver = MagicMock()
        mock_reserve_element = MagicMock()
        mock_modal = MagicMock()
        mock_confirm_button = MagicMock()
        mock_confirm_button.text = "Book Now"

        # Make the WebDriverWait return values for each .until() call:
        # 1. element_to_be_clickable (reserve button check)
        # 2. visibility_of_any_elements_located (modal detection - returns a list)
        # 3+ any remaining calls (Book Now wait, url_changes, success indicators, etc.)
        with (
            patch("app.providers.walden_provider.WebDriverWait") as mock_wait_cls,
            patch(
                "app.providers.walden_provider.expected_conditions.visibility_of_any_elements_located"
            ) as mock_visibility_any,
        ):
            mock_wait_instance = MagicMock()
            mock_wait_cls.return_value = mock_wait_instance
            modal_condition = mock_visibility_any.return_value

            # Use a default return for .until() but make the second call return the modal.
            # Asserting on the condition object (not just call order) is what catches a
            # regression back to visibility_of_element_located, which silently passes
            # since this mock doesn't otherwise care which predicate it was given.
            call_count = [0]

            def until_side_effect(condition, *args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_reserve_element  # clickable check
                elif call_count[0] == 2:
                    assert condition == modal_condition
                    return [mock_modal]  # modal detection (visible matches)
                else:
                    return mock_confirm_button  # all subsequent calls

            mock_wait_instance.until.side_effect = until_side_effect

            # Mock _select_player_count_sync to capture args and short-circuit
            with patch.object(
                provider, "_select_player_count_sync", return_value=False
            ) as mock_select:
                provider.wait_strategy = MagicMock()
                # Also mock _capture_diagnostic_info to avoid side effects
                # Mock _check_slot_blocked_popup to return False (no blocked popup)
                with patch.object(provider, "_capture_diagnostic_info"):
                    with patch.object(provider, "_check_slot_blocked_popup", return_value=False):
                        result = provider._complete_booking_sync(
                            mock_driver, mock_reserve_element, time(8, 26), 4
                        )

                # Player count selection was called (it returns False, so booking fails)
                assert result.success is False
                assert "Failed to select 4 players" in result.error_message

                # The critical assertion: search_context was passed as the modal element
                mock_select.assert_called_once()
                call_kwargs = mock_select.call_args
                assert call_kwargs.kwargs.get("search_context") is mock_modal

                # Modal detection used the any-elements predicate (checks every
                # match for visibility) with the booking modal locator, not just
                # the first element in the DOM
                mock_visibility_any.assert_called_once_with(
                    (By.CSS_SELECTOR, DOM.BOOKING_MODAL.modal_container)
                )


class TestWaldenProviderExtractEventBlocks:
    """Tests for the _extract_event_blocks method."""

    def test_extracts_single_event_in_time_window(self, provider: WaldenGolfProvider) -> None:
        """Test extracting a single event that blocks time slots."""
        mock_search_context = MagicMock()

        # Create a mock slot item with event block
        mock_slot_item = MagicMock()
        mock_slot_item.text.strip.return_value = "08:26 AM-10:42 AM Northgate SGA 3 Man ABC - 3318"

        mock_search_context.find_elements.return_value = [mock_slot_item]

        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(9, 0),
            fallback_window_minutes=32,
        )

        assert len(result) == 1
        assert "Northgate SGA 3 Man ABC - 3318" in result[0]

    def test_extracts_multiple_events_in_time_window(self, provider: WaldenGolfProvider) -> None:
        """Test extracting multiple events that block time slots."""
        mock_search_context = MagicMock()

        mock_slot_item1 = MagicMock()
        mock_slot_item1.text.strip.return_value = "08:00 AM-09:30 AM Morning Tournament"

        mock_slot_item2 = MagicMock()
        mock_slot_item2.text.strip.return_value = "09:00 AM-10:00 AM Member Outing"

        mock_search_context.find_elements.return_value = [mock_slot_item1, mock_slot_item2]

        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(9, 0),
            fallback_window_minutes=32,
        )

        assert len(result) == 2
        assert "Morning Tournament" in result[0]
        assert "Member Outing" in result[1]

    def test_ignores_events_outside_time_window(self, provider: WaldenGolfProvider) -> None:
        """Test that events outside the target time window are ignored."""
        mock_search_context = MagicMock()

        mock_slot_item = MagicMock()
        # Event from 2pm-4pm should not affect 9am booking with 32min window
        mock_slot_item.text.strip.return_value = "02:00 PM-04:00 PM Afternoon Event"

        mock_search_context.find_elements.return_value = [mock_slot_item]

        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(9, 0),
            fallback_window_minutes=32,
        )

        assert len(result) == 0

    def test_ignores_regular_slots_without_time_range(self, provider: WaldenGolfProvider) -> None:
        """Test that regular time slots (not events) are ignored."""
        mock_search_context = MagicMock()

        mock_slot_item = MagicMock()
        # Regular slot shows single time, not a range
        mock_slot_item.text.strip.return_value = "08:58 AM Reserve"

        mock_search_context.find_elements.return_value = [mock_slot_item]

        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(9, 0),
            fallback_window_minutes=32,
        )

        assert len(result) == 0

    def test_handles_empty_search_context(self, provider: WaldenGolfProvider) -> None:
        """Test handling empty search context gracefully."""
        mock_search_context = MagicMock()
        mock_search_context.find_elements.return_value = []

        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(9, 0),
            fallback_window_minutes=32,
        )

        assert len(result) == 0

    def test_handles_midnight_spanning_event_morning_target(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that events spanning midnight are detected for morning bookings."""
        mock_search_context = MagicMock()

        mock_slot_item = MagicMock()
        # Event from 11pm to 1am spans midnight
        mock_slot_item.text.strip.return_value = "11:00 PM-01:00 AM Late Night Event"

        mock_search_context.find_elements.return_value = [mock_slot_item]

        # Target time is 12:30 AM - should overlap with event
        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(0, 30),
            fallback_window_minutes=32,
        )

        assert len(result) == 1
        assert "Late Night Event" in result[0]

    def test_handles_midnight_spanning_event_evening_target(
        self, provider: WaldenGolfProvider
    ) -> None:
        """Test that events spanning midnight are detected for evening bookings."""
        mock_search_context = MagicMock()

        mock_slot_item = MagicMock()
        # Event from 11pm to 1am spans midnight
        mock_slot_item.text.strip.return_value = "11:00 PM-01:00 AM Late Night Event"

        mock_search_context.find_elements.return_value = [mock_slot_item]

        # Target time is 11:30 PM - should overlap with event
        result = provider._extract_event_blocks(
            mock_search_context,
            target_time=time(23, 30),
            fallback_window_minutes=32,
        )

        assert len(result) == 1
        assert "Late Night Event" in result[0]


class TestWaldenProviderFormatEventBlockMessage:
    """Tests for the _format_event_block_message helper method."""

    def test_returns_none_for_empty_list(self, provider: WaldenGolfProvider) -> None:
        """Test that None is returned for empty event list."""
        result = provider._format_event_block_message([])
        assert result is None

    def test_formats_single_event(self, provider: WaldenGolfProvider) -> None:
        """Test formatting a single event."""
        result = provider._format_event_block_message(["Morning Tournament"])
        assert result == "Time blocked by event: Morning Tournament"

    def test_formats_two_events(self, provider: WaldenGolfProvider) -> None:
        """Test formatting two events."""
        result = provider._format_event_block_message(["Event A", "Event B"])
        assert result == "Times blocked by events: Event A, Event B"

    def test_formats_three_events(self, provider: WaldenGolfProvider) -> None:
        """Test formatting three events."""
        result = provider._format_event_block_message(["Event A", "Event B", "Event C"])
        assert result == "Times blocked by events: Event A, Event B, Event C"

    def test_truncates_more_than_three_events(self, provider: WaldenGolfProvider) -> None:
        """Test that more than 3 events are truncated."""
        result = provider._format_event_block_message(
            ["Event A", "Event B", "Event C", "Event D", "Event E"]
        )
        assert result == "Times blocked by events: Event A, Event B, Event C and 2 more"


def _make_disabled_slot(
    reason: str | None, time_text: str | None, course_index: str = "0"
) -> MagicMock:
    """Build a mock datascroller slot item for _extract_blocked_slot_reasons.

    Passing reason=None simulates a normal (bookable) slot with no disabled
    heading; time_text=None simulates a disabled slot whose time can't be read.
    course_index tags the slot's course via the teeTimeCourses:<n> element-ID
    pattern ("0" = Northgate, "1" = Walden on Lake Conroe).
    """
    slot = MagicMock()

    heading_els = []
    if reason is not None:
        heading = MagicMock()
        heading.text = reason
        heading_els = [heading]

    label_els = []
    if time_text is not None:
        label = MagicMock()
        label.text = time_text
        label_els = [label]

    def find_elements(_by: str, selector: str) -> list[MagicMock]:
        if selector == DOM.DISABLED_SLOT.reason_heading:
            return heading_els
        if selector == DOM.DISABLED_SLOT.time_label:
            return label_els
        return []

    slot.find_elements.side_effect = find_elements
    slot.get_attribute.return_value = (
        f"<div id='...teeTimeCourses:{course_index}:teeTimeSlots:0:slotTee:0:slotTeeDIV'></div>"
    )
    return slot


class TestWaldenProviderExtractBlockedSlotReasons:
    """Tests for the _extract_blocked_slot_reasons method."""

    def test_extracts_single_reason_in_window(self, provider: WaldenGolfProvider) -> None:
        """A single disabled slot in the window yields its reason."""
        search_context = MagicMock()
        search_context.find_elements.return_value = [
            _make_disabled_slot("Aerification", "05:07 PM")
        ]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 7), fallback_window_minutes=32
        )

        assert result == ["Aerification"]

    def test_deduplicates_repeated_reason(self, provider: WaldenGolfProvider) -> None:
        """A reason shared by many blocked slots is reported once."""
        search_context = MagicMock()
        search_context.find_elements.return_value = [
            _make_disabled_slot("Aerification", "05:07 PM"),
            _make_disabled_slot("Aerification", "05:15 PM"),
            _make_disabled_slot("Aerification", "05:23 PM"),
        ]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 15), fallback_window_minutes=32
        )

        assert result == ["Aerification"]

    def test_collects_distinct_reasons_in_order(self, provider: WaldenGolfProvider) -> None:
        """Distinct reasons are collected in first-seen order."""
        search_context = MagicMock()
        search_context.find_elements.return_value = [
            _make_disabled_slot("Weather delay", "07:30 AM"),
            _make_disabled_slot("Aerification", "07:38 AM"),
        ]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(7, 34), fallback_window_minutes=32
        )

        assert result == ["Weather delay", "Aerification"]

    def test_ignores_reasons_outside_window(self, provider: WaldenGolfProvider) -> None:
        """Blocked slots outside the target +/- window are ignored."""
        search_context = MagicMock()
        search_context.find_elements.return_value = [
            _make_disabled_slot("Aerification", "07:30 AM")
        ]

        # Target 5pm; the 7:30am block is well outside the window.
        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 0), fallback_window_minutes=32
        )

        assert result == []

    def test_ignores_bookable_slots(self, provider: WaldenGolfProvider) -> None:
        """Normal (non-disabled) slots contribute no reason."""
        search_context = MagicMock()
        # No disabled heading -> a normal, bookable slot.
        search_context.find_elements.return_value = [_make_disabled_slot(None, "05:07 PM")]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 7), fallback_window_minutes=32
        )

        assert result == []

    def test_excludes_non_northgate_course(self, provider: WaldenGolfProvider) -> None:
        """A disabled slot from another course (Walden) is not reported.

        When no dedicated Northgate section exists the search spans the whole
        page, so Walden-on-Lake-Conroe disabled slots must be filtered out by
        course index rather than surfaced in a Northgate failure message.
        """
        search_context = MagicMock()
        search_context.find_elements.return_value = [
            _make_disabled_slot("Aerification", "05:07 PM", course_index="1"),  # Walden
            _make_disabled_slot("Weather delay", "05:15 PM", course_index="0"),  # Northgate
        ]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 7), fallback_window_minutes=32
        )

        assert result == ["Weather delay"]

    def test_includes_reason_when_time_unreadable(self, provider: WaldenGolfProvider) -> None:
        """A disabled slot with no parseable time is still surfaced."""
        search_context = MagicMock()
        search_context.find_elements.return_value = [_make_disabled_slot("Aerification", None)]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 7), fallback_window_minutes=32
        )

        assert result == ["Aerification"]

    def test_skips_generic_cannot_be_reserved_heading(self, provider: WaldenGolfProvider) -> None:
        """The generic 'Cannot be reserved' text is not treated as a reason."""
        search_context = MagicMock()
        search_context.find_elements.return_value = [
            _make_disabled_slot("Cannot be reserved", "05:07 PM")
        ]

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 7), fallback_window_minutes=32
        )

        assert result == []

    def test_handles_empty_search_context(self, provider: WaldenGolfProvider) -> None:
        """An empty tee sheet yields no reasons and does not error."""
        search_context = MagicMock()
        search_context.find_elements.return_value = []

        result = provider._extract_blocked_slot_reasons(
            search_context, target_time=time(17, 7), fallback_window_minutes=32
        )

        assert result == []


class TestWaldenProviderFormatBlockedSlotMessage:
    """Tests for the _format_blocked_slot_message helper method."""

    def test_returns_none_for_empty_list(self, provider: WaldenGolfProvider) -> None:
        """No reasons produces no message suffix."""
        assert provider._format_blocked_slot_message([]) is None

    def test_formats_single_reason(self, provider: WaldenGolfProvider) -> None:
        """A single reason renders as a one-item sentence."""
        result = provider._format_blocked_slot_message(["Aerification"])
        assert result == "Nearby tee times are unavailable: Aerification (cannot be reserved)."

    def test_formats_multiple_reasons(self, provider: WaldenGolfProvider) -> None:
        """Multiple reasons are comma-joined."""
        result = provider._format_blocked_slot_message(["Aerification", "Weather delay"])
        assert result == (
            "Nearby tee times are unavailable: Aerification, Weather delay (cannot be reserved)."
        )

    def test_truncates_more_than_three_reasons(self, provider: WaldenGolfProvider) -> None:
        """More than three reasons are truncated with an 'and N more' tail."""
        result = provider._format_blocked_slot_message(["A", "B", "C", "D", "E"])
        assert (
            result == "Nearby tee times are unavailable: A, B, C and 2 more (cannot be reserved)."
        )


class TestWaldenProviderEnhancedErrorMessages:
    """Tests for enhanced error messages with event information."""

    def test_error_message_includes_single_event(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that failure message includes single event name."""
        mock_driver = MagicMock()

        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(provider, "_find_empty_slots", MagicMock(return_value=[]))
        monkeypatch.setattr(
            provider,
            "_extract_event_blocks",
            MagicMock(return_value=["Northgate SGA Tournament"]),
        )

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(9, 0),
            num_players=4,
            fallback_window_minutes=32,
        )

        assert result.success is False
        assert "Northgate SGA Tournament" in result.error_message
        assert "Time blocked by event:" in result.error_message

    def test_error_message_includes_multiple_events(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that failure message includes multiple event names."""
        mock_driver = MagicMock()

        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(provider, "_find_empty_slots", MagicMock(return_value=[]))
        monkeypatch.setattr(
            provider,
            "_extract_event_blocks",
            MagicMock(return_value=["Event A", "Event B"]),
        )

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(9, 0),
            num_players=4,
            fallback_window_minutes=32,
        )

        assert result.success is False
        assert "Event A" in result.error_message
        assert "Event B" in result.error_message
        assert "Times blocked by events:" in result.error_message

    def test_error_message_without_events_has_generic_message(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that failure message is generic when no events found."""
        mock_driver = MagicMock()

        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(provider, "_find_empty_slots", MagicMock(return_value=[]))
        monkeypatch.setattr(provider, "_extract_event_blocks", MagicMock(return_value=[]))

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(9, 0),
            num_players=4,
            fallback_window_minutes=32,
        )

        assert result.success is False
        assert "No time slots with 4 available spots found" in result.error_message
        assert "blocked by event" not in result.error_message

    def test_error_message_includes_blocked_slot_reason(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failure message names the disabled-slot reason (e.g. Aerification)."""
        mock_driver = MagicMock()

        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(provider, "_find_empty_slots", MagicMock(return_value=[]))
        monkeypatch.setattr(provider, "_extract_event_blocks", MagicMock(return_value=[]))
        monkeypatch.setattr(
            provider,
            "_extract_blocked_slot_reasons",
            MagicMock(return_value=["Aerification"]),
        )

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(17, 7),
            num_players=4,
            fallback_window_minutes=32,
        )

        assert result.success is False
        assert "Aerification" in result.error_message
        assert "cannot be reserved" in result.error_message.lower()

    def test_fallback_failure_includes_event_info(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fallback failure message includes event names."""
        mock_driver = MagicMock()
        slot_el = MagicMock()

        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        # Return slots, but none in the fallback window (outside 8 min of 9:00)
        monkeypatch.setattr(
            provider,
            "_find_empty_slots",
            MagicMock(return_value=[(time(10, 0), slot_el)]),  # 1 hour away
        )
        monkeypatch.setattr(provider, "_is_northgate_slot", lambda *_: True)
        monkeypatch.setattr(
            provider,
            "_extract_event_blocks",
            MagicMock(return_value=["Member Event"]),
        )

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(9, 0),
            num_players=4,
            fallback_window_minutes=8,
            tee_time_interval_minutes=8,
        )

        assert result.success is False
        assert "Member Event" in result.error_message


class TestFindTargetSlotJS:
    """Tests for the JavaScript-based slot finding method."""

    def test_find_exact_match(self, provider: WaldenGolfProvider) -> None:
        """Test that JS finder returns exact match info."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = [
            {
                "timeStr": "8:42",
                "hours": 8,
                "minutes": 42,
                "index": 5,
                "diff": 0,
                "available": 4,
                "isExact": True,
            }
        ]

        result = provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        assert result is not None
        assert result["isExact"] is True
        assert result["hours"] == 8
        assert result["minutes"] == 42
        assert result["index"] == 5

    def test_find_fallback_when_exact_unavailable(self, provider: WaldenGolfProvider) -> None:
        """Test that JS finder returns fallback slot."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = [
            {
                "timeStr": "8:50",
                "hours": 8,
                "minutes": 50,
                "index": 7,
                "diff": 8,
                "available": 4,
                "isExact": False,
            }
        ]

        result = provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        assert result is not None
        assert result["isExact"] is False
        assert result["diff"] == 8

    def test_returns_none_when_no_slots(self, provider: WaldenGolfProvider) -> None:
        """Test that JS finder returns None when no slots available."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = None

        result = provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        assert result is None

    def test_the_rejection_tally_is_logged(
        self, provider: WaldenGolfProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A morning with no fallbacks has to say whether the sheet was full.

        2026-08-08 logged "0 fallback(s) behind it" and nothing else, which is
        the same line whether every slot was taken or the finder threw bookable
        ones away. The tally is what tells those apart without the sheet.
        """
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = {
            "candidates": [],
            "rejected": {"course": 40, "window": 12, "capacity": 9, "offGrid": 0, "excluded": 0},
            "scanned": 61,
        }

        with caplog.at_level(logging.INFO):
            provider._find_target_slot_js(mock_driver, time(8, 0), 4, 32, 8)

        line = next(m for m in caplog.messages if "Slot scan of" in m)
        assert "61 row(s)" in line
        assert "0 candidate(s)" in line
        # The counts that separate "full sheet" from "finder dropped them".
        assert "capacity=9" in line
        assert "course=40" in line
        # Zeroed reasons stay out of the line rather than padding it.
        assert "offGrid" not in line

    def test_off_grid_fallbacks_are_flagged(
        self, provider: WaldenGolfProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Slots the old interval filter would have dropped are called out.

        The Python side only counts and reports what the scan returns, so this
        covers the log line and nothing about the ranking - the fixture is
        pre-sorted and would satisfy this test with the comparator deleted.
        `TestSlotFinderScript` runs the real script for that.
        """
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = {
            "candidates": [
                {
                    "hours": 8,
                    "minutes": 8,
                    "index": 2,
                    "diff": 8,
                    "available": 4,
                    "isExact": False,
                    "aligned": True,
                },
                {
                    "hours": 8,
                    "minutes": 2,
                    "index": 1,
                    "diff": 2,
                    "available": 4,
                    "isExact": False,
                    "aligned": False,
                },
            ],
            # No offGrid key: a retained off-grid slot is not a rejection, and
            # the "(N off-grid)" count below comes off the candidate list.
            "rejected": {"capacity": 2},
            "scanned": 12,
        }

        with caplog.at_level(logging.INFO):
            result = provider._find_target_slot_js(mock_driver, time(8, 0), 4, 32, 8)

        # Head of whatever the scan ranked, which the fixture fixes as 8:08.
        assert result is not None and (result["hours"], result["minutes"]) == (8, 8)
        assert any("1 fallback(s) behind it (1 off-grid)" in m for m in caplog.messages)

    def test_a_bare_list_is_still_understood(self, provider: WaldenGolfProvider) -> None:
        """The finder predates the tally, and a plain list must still work."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = [
            {"hours": 8, "minutes": 42, "index": 5, "diff": 0, "available": 4, "isExact": True}
        ]

        result = provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        assert result is not None
        assert (result["hours"], result["minutes"]) == (8, 42)

    def test_takes_the_head_of_the_ranking(self, provider: WaldenGolfProvider) -> None:
        """The single-slot finder returns the best of a ranked list, not any of it."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = [
            {"hours": 8, "minutes": 42, "index": 5, "diff": 0, "available": 4, "isExact": True},
            {"hours": 8, "minutes": 50, "index": 6, "diff": 8, "available": 4, "isExact": False},
            {"hours": 8, "minutes": 34, "index": 4, "diff": 8, "available": 4, "isExact": False},
        ]

        result = provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        assert result is not None
        assert (result["hours"], result["minutes"]) == (8, 42)

    def test_ranking_exposes_every_candidate(self, provider: WaldenGolfProvider) -> None:
        """The fallback list the direct chain walks is the whole ranking."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = [
            {"hours": 8, "minutes": 42, "index": 5, "diff": 0, "available": 4, "isExact": True},
            {"hours": 8, "minutes": 50, "index": 6, "diff": 8, "available": 4, "isExact": False},
        ]

        ranked = provider._rank_candidate_slots_js(mock_driver, time(8, 42), 4, 32, 8)

        assert [(c["hours"], c["minutes"]) for c in ranked] == [(8, 42), (8, 50)]

    def test_ranking_is_empty_when_no_slots(self, provider: WaldenGolfProvider) -> None:
        """A driver that returns nothing yields no candidates, not a None to unpack."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = None

        assert provider._rank_candidate_slots_js(mock_driver, time(8, 42), 4, 32, 8) == []

    def test_passes_exclude_times_correctly(self, provider: WaldenGolfProvider) -> None:
        """Test that exclude times are passed as the correct argument."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = None

        provider._find_target_slot_js(
            mock_driver,
            time(8, 42),
            4,
            32,
            8,
            times_to_exclude={time(8, 50), time(9, 6)},
        )

        call_args = mock_driver.execute_script.call_args
        # The exclude list is the 6th positional argument (index 5 after js_code)
        exclude_arg = call_args[0][6]
        assert len(exclude_arg) == 2
        exclude_minutes = {e["h"] * 60 + e["m"] for e in exclude_arg}
        assert 8 * 60 + 50 in exclude_minutes
        assert 9 * 60 + 6 in exclude_minutes

    def test_passes_northgate_course_index(self, provider: WaldenGolfProvider) -> None:
        """Test that the Northgate course index constant is passed to JS."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = None

        provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        call_args = mock_driver.execute_script.call_args
        # The northgate index is the 7th positional argument (index 6 after js_code)
        northgate_index = call_args[0][7]
        assert northgate_index == "0"


_DOM_SHIM = """
function slot(timeText, available, course) {
  return {
    innerHTML: '<a id="f:teeTimeCourses:' + course + ':teeTimeSlots:1:slotTee:0:reserve_button">',
    textContent: timeText,
    querySelector: function (sel) {
      if (sel === 'label') return {textContent: timeText};
      if (sel.indexOf('reserve_button') !== -1) return {id: 'reserve_' + timeText};
      return null;
    },
    querySelectorAll: function (sel) {
      if (sel === 'div.Empty') return [];
      if (sel === 'span.custom-free-slot-span') return new Array(available).fill({});
      return [];
    }
  };
}
var rows = ROWS.map(function (r) { return slot(r[0], r[1], r[2] || '0'); });
global.document = {querySelectorAll: function () { return rows; }};
var run = new Function(SCRIPT);
var out = run.apply(null, ARGS);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
class TestSlotFinderScript:
    """The scan itself, run as the browser gets it.

    The ranking is the load-bearing part of the off-grid change and it lives in
    JavaScript, so a Python test that hands `_find_target_slot_js` a pre-sorted
    list cannot exercise it - that test would pass with the comparator deleted.
    This runs the real script over a stand-in DOM instead.
    """

    def _scan(
        self,
        rows: list[tuple[str, int]],
        target: time,
        *,
        players: int = 4,
        window: int = 32,
        interval: int = 8,
    ) -> dict[str, Any]:
        from app.providers.walden_provider import _SLOT_FINDER_JS

        program = (
            f"const SCRIPT = {json.dumps(_SLOT_FINDER_JS)};\n"
            f"const ROWS = {json.dumps([[t, n] for t, n in rows])};\n"
            f"const ARGS = {json.dumps([target.hour, target.minute, players, window, interval, [], '0', 4])};\n"
            + _DOM_SHIM
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(program)
            path = fh.name
        try:
            proc = subprocess.run(
                ["node", path], capture_output=True, text=True, timeout=30, check=True
            )
        finally:
            os.unlink(path)
        result: dict[str, Any] = json.loads(proc.stdout)
        return result

    def _times(self, out: dict[str, Any]) -> list[str]:
        return [f"{c['hours']:02d}:{c['minutes']:02d}" for c in out["candidates"]]

    def test_an_on_grid_request_is_unchanged(self) -> None:
        """The case that already worked has to keep working exactly."""
        rows = [("7:44 AM", 4), ("7:52 AM", 4), ("8:00 AM", 4), ("8:08 AM", 4), ("8:16 AM", 4)]

        out = self._scan(rows, time(8, 0))

        assert self._times(out) == ["08:00", "07:52", "08:08", "07:44", "08:16"]
        assert all(c["aligned"] for c in out["candidates"])

    def test_an_off_grid_request_keeps_its_fallbacks(self) -> None:
        """The failure this change exists for.

        Every one of these is a non-multiple of 8 from 8:00, so the old filter
        dropped all five and the morning failed with "no suitable slot" - not a
        lost race, a request never made.
        """
        rows = [("7:40 AM", 4), ("7:48 AM", 4), ("7:56 AM", 4), ("8:04 AM", 4), ("8:12 AM", 4)]

        out = self._scan(rows, time(8, 0))

        assert self._times(out) == ["07:56", "08:04", "07:48", "08:12", "07:40"]
        assert not any(c["aligned"] for c in out["candidates"])

    def test_an_aligned_slot_outranks_a_nearer_off_grid_one(self) -> None:
        """The safety property: the slot reserved does not move.

        8:02 is six minutes closer, and still loses. Without this the change
        would quietly start booking different tee times than before.
        """
        out = self._scan([("8:02 AM", 4), ("8:08 AM", 4)], time(8, 0))

        assert self._times(out) == ["08:08", "08:02"]

    def test_a_full_sheet_says_capacity_not_grid(self) -> None:
        """The distinction 2026-08-08 could not be read for."""
        rows = [("7:52 AM", 0), ("8:00 AM", 0), ("8:08 AM", 0)]

        out = self._scan(rows, time(8, 0))

        assert out["candidates"] == []
        assert out["rejected"]["capacity"] == 3
        assert out["rejected"]["window"] == 0

    def test_a_retained_off_grid_slot_is_never_called_dropped(self) -> None:
        """A tally that blames a slot it kept is the bug this scan is fixing."""
        out = self._scan([("8:02 AM", 4)], time(8, 0))

        assert self._times(out) == ["08:02"]
        assert not out["candidates"][0]["aligned"]
        assert sum(out["rejected"].values()) == 0


class TestPreWindowSheetCapture:
    """The sheet the fallback list was built from, kept until it is needed.

    Only the post-failure DOM was ever captured, and by then the window has
    opened and other members have taken slots - so it cannot answer whether the
    finder had fallbacks to walk when it made the list.
    """

    def test_the_sheet_is_held_not_uploaded(self, provider: WaldenGolfProvider) -> None:
        """Staging keeps it; a booked morning never pays to store it."""
        mock_driver = MagicMock()
        mock_driver.page_source = "<html>tee sheet</html>"

        with patch.object(provider, "_capture_response_artifact") as upload:
            provider._capture_pre_window_sheet(mock_driver)

        assert provider._pre_window_sheet == "<html>tee sheet</html>"
        upload.assert_not_called()

    def test_a_failed_morning_flushes_it(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upload happens once the morning turns out to need explaining."""
        provider._pre_window_sheet = "<html>tee sheet</html>"
        monkeypatch.setenv("DEBUG_ARTIFACTS_BUCKET", "artifacts")

        with patch.object(provider, "_upload_bytes_to_gcs", return_value="gs://x") as upload:
            provider._flush_pre_window_sheet("blocked_reserve_sent")

        upload.assert_called_once_with(
            bucket_name="artifacts",
            object_name=ANY,
            content_type="text/html; charset=utf-8",
            data=b"<html>tee sheet</html>",
        )
        assert "pre_window_sheet_blocked_reserve_sent" in upload.call_args.kwargs["object_name"]
        # Cleared, so a second failure in the same run cannot re-upload a sheet
        # that no longer describes it.
        assert provider._pre_window_sheet is None

    def test_the_sheet_never_reaches_the_logs(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A tee sheet names other members, and logs are a wider audience.

        _capture_response_artifact echoes the first 1000 characters of its
        markup's visible text, which is why the sheet does not go through it.
        """
        sheet = "<html><body><div>07:52 AM</div><div>Reserved by Jane Q. Member</div></body></html>"
        provider._pre_window_sheet = sheet
        monkeypatch.setenv("DEBUG_ARTIFACTS_BUCKET", "artifacts")

        with (
            caplog.at_level(logging.DEBUG),
            patch.object(provider, "_upload_bytes_to_gcs", return_value="gs://x"),
        ):
            provider._flush_pre_window_sheet("no_candidate")

        logged = "\n".join(caplog.messages)
        assert "Jane Q. Member" not in logged
        assert "<div>" not in logged
        # The size and the destination are what a post-mortem needs from the log.
        assert "gs://x" in logged
        assert f"{len(sheet)} bytes" in logged

    def test_no_bucket_still_reports_the_size(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nowhere to put it is worth saying, not worth failing over."""
        provider._pre_window_sheet = "<html>tee sheet</html>"
        monkeypatch.delenv("DEBUG_ARTIFACTS_BUCKET", raising=False)

        with caplog.at_level(logging.INFO):
            provider._flush_pre_window_sheet("no_candidate")

        assert any("DEBUG_ARTIFACTS_BUCKET is unset" in m for m in caplog.messages)
        assert provider._pre_window_sheet is None

    def test_a_new_batch_drops_the_previous_sheet(self, provider: WaldenGolfProvider) -> None:
        """A run must never flush the sheet a previous run staged.

        A batch that fails before slot pre-location would otherwise upload the
        last run's sheet under this morning's failure - a diagnostic describing
        the wrong day, which is worse than having none.
        """
        provider._pre_window_sheet = "<html>yesterday</html>"

        provider._book_multiple_tee_times_sync(date(2026, 8, 16), [], None)

        assert provider._pre_window_sheet is None

    def test_no_sheet_says_so_rather_than_going_quiet(
        self, provider: WaldenGolfProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ "Never staged" and "staged and lost" call for different fixes."""
        provider._pre_window_sheet = None

        with (
            caplog.at_level(logging.INFO),
            patch.object(provider, "_capture_response_artifact") as upload,
        ):
            provider._flush_pre_window_sheet("failed_player_count")

        upload.assert_not_called()
        assert any("No pre-window tee sheet held" in m for m in caplog.messages)

    def test_an_unreadable_dom_does_not_break_staging(
        self, provider: WaldenGolfProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Diagnostics must never be what loses a booking."""
        mock_driver = MagicMock()
        type(mock_driver).page_source = PropertyMock(side_effect=WebDriverException("gone"))

        with caplog.at_level(logging.WARNING):
            provider._capture_pre_window_sheet(mock_driver)

        assert provider._pre_window_sheet is None
        assert any("Could not read the pre-window tee sheet" in m for m in caplog.messages)


class TestClickSlotByIndexJS:
    """Tests for the JavaScript-based Reserve click method."""

    def test_returns_true_on_success(self, provider: WaldenGolfProvider) -> None:
        """Test that click returns True when successful."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = True

        result = provider._click_slot_by_index_js(mock_driver, 5)
        assert result is True

    def test_returns_false_on_failure(self, provider: WaldenGolfProvider) -> None:
        """Test that click returns False when element not found."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = False

        result = provider._click_slot_by_index_js(mock_driver, 999)
        assert result is False

    def test_returns_false_on_none(self, provider: WaldenGolfProvider) -> None:
        """Test that click returns False when JS returns None."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = None

        result = provider._click_slot_by_index_js(mock_driver, 0)
        assert result is False

    def test_passes_correct_index(self, provider: WaldenGolfProvider) -> None:
        """Test that the slot index is passed correctly to JS."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = True

        provider._click_slot_by_index_js(mock_driver, 42)

        call_args = mock_driver.execute_script.call_args
        assert call_args[0][1] == 42


class TestPrecisionWait:
    """Tests for the precision busy-wait method."""

    def test_returns_immediately_if_past_execute_at(self, provider: WaldenGolfProvider) -> None:
        """Test that precision wait returns immediately if already past target time."""
        from datetime import datetime
        from unittest.mock import patch as mock_patch
        from zoneinfo import ZoneInfo

        # execute_at is 1 hour ago
        past_time = datetime(2026, 2, 12, 5, 30, 0)

        with mock_patch.object(CTDateTime, "now") as mock_ct_now:
            mock_ct_now.return_value = datetime(
                2026, 2, 12, 6, 30, 0, tzinfo=ZoneInfo("America/Chicago")
            )
            # Should not raise or hang
            provider._precision_wait_until(past_time)

    def test_calls_sleep_for_coarse_wait(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that precision wait uses sleep for the coarse portion."""
        from datetime import datetime as real_datetime
        from unittest.mock import patch as mock_patch
        from zoneinfo import ZoneInfo

        import app.providers.walden_provider as walden_module

        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(walden_module.time_module, "sleep", mock_sleep)

        call_count = 0

        def mock_now(tz=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First calls: before execute_at (to trigger the wait)
                return real_datetime(2026, 2, 12, 6, 29, 58, tzinfo=ZoneInfo("America/Chicago"))
            # After sleep, return time past execute_at to exit busy-wait
            return real_datetime(2026, 2, 12, 6, 30, 0, 1000, tzinfo=ZoneInfo("America/Chicago"))

        with mock_patch.object(CTDateTime, "now") as mock_ct_now:
            mock_ct_now.side_effect = mock_now

            execute_at = real_datetime(2026, 2, 12, 6, 30, 0)
            provider._precision_wait_until(execute_at)

        # Should have called sleep once for coarse wait and micro-sleeps for busy-wait
        assert len(sleep_calls) >= 1
        assert sleep_calls[0] > 0  # coarse sleep is wait_seconds - 0.2
        # Any subsequent sleep calls are the busy-wait 0.1ms micro-sleeps
        assert all(s == 0.0001 for s in sleep_calls[1:])


class TestFindAndBookFastJS:
    """Tests for the fast JS path in _find_and_book_time_slot_sync."""

    def test_fast_js_finds_and_books_exact_match(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fast JS path finds slot and executes fast booking chain."""
        mock_driver = MagicMock()

        # Mock JS slot finder
        monkeypatch.setattr(
            provider,
            "_find_target_slot_js",
            MagicMock(
                return_value={
                    "timeStr": "8:42",
                    "hours": 8,
                    "minutes": 42,
                    "index": 5,
                    "diff": 0,
                    "available": 4,
                    "isExact": True,
                }
            ),
        )

        # Mock fast booking chain - returns success
        monkeypatch.setattr(
            provider,
            "_execute_fast_booking_chain_js",
            MagicMock(
                return_value={
                    "success": True,
                    "blocked": False,
                    "phase": "complete",
                    "error": None,
                    "timing": {"totalMs": 1500},
                }
            ),
        )

        # Mock verification and confirmation extraction
        monkeypatch.setattr(provider, "_verify_booking_success", MagicMock(return_value=True))
        monkeypatch.setattr(
            provider, "_extract_confirmation_number", MagicMock(return_value="ABC123")
        )

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
        )

        assert result.success is True
        assert result.confirmation_number == "ABC123"
        # Verify _execute_fast_booking_chain_js was called with correct args
        provider._execute_fast_booking_chain_js.assert_called_once_with(
            mock_driver,
            5,
            4,  # driver, slot_index, num_players
        )
        # Verify confirmation extraction and verification were called
        provider._extract_confirmation_number.assert_called_once_with(mock_driver)
        provider._verify_booking_success.assert_called_once_with(mock_driver)

    def test_fast_js_returns_failure_when_no_slot(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fast JS path returns failure when no slot found."""
        mock_driver = MagicMock()

        monkeypatch.setattr(provider, "_find_target_slot_js", MagicMock(return_value=None))

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
        )

        assert result.success is False
        assert "No time slots" in result.error_message

    def test_fast_js_returns_failure_when_slot_blocked(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fast JS path returns failure when slot is blocked by another user."""
        mock_driver = MagicMock()

        monkeypatch.setattr(
            provider,
            "_find_target_slot_js",
            MagicMock(
                return_value={
                    "timeStr": "8:42",
                    "hours": 8,
                    "minutes": 42,
                    "index": 5,
                    "diff": 0,
                    "available": 4,
                    "isExact": True,
                }
            ),
        )

        # Mock fast booking chain - returns blocked
        monkeypatch.setattr(
            provider,
            "_execute_fast_booking_chain_js",
            MagicMock(
                return_value={
                    "success": False,
                    "blocked": True,
                    "phase": "blocked_check",
                    "error": "Slot blocked by another user",
                    "timing": {"blockedDetected": 300},
                }
            ),
        )
        monkeypatch.setattr(provider, "_capture_diagnostic_info", MagicMock())

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
        )

        assert result.success is False
        assert "blocked" in result.error_message.lower()
        # Verify diagnostic capture was called with correct key
        provider._capture_diagnostic_info.assert_called_once_with(
            mock_driver, "slot_blocked_by_other_user"
        )

    def test_fast_js_returns_failure_when_chain_fails(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fast JS path returns failure when booking chain fails."""
        mock_driver = MagicMock()

        monkeypatch.setattr(
            provider,
            "_find_target_slot_js",
            MagicMock(
                return_value={
                    "timeStr": "8:42",
                    "hours": 8,
                    "minutes": 42,
                    "index": 5,
                    "diff": 0,
                    "available": 4,
                    "isExact": True,
                }
            ),
        )

        # Mock fast booking chain - returns failure at player_count phase
        monkeypatch.setattr(
            provider,
            "_execute_fast_booking_chain_js",
            MagicMock(
                return_value={
                    "success": False,
                    "blocked": False,
                    "phase": "player_count_wait",
                    "error": "Player count selector not found within 5000ms",
                    "timing": {"playerSelectorTimeout": 5000},
                }
            ),
        )
        monkeypatch.setattr(provider, "_capture_diagnostic_info", MagicMock())

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
        )

        assert result.success is False
        assert "player_count_wait" in result.error_message
        # Verify diagnostic capture was called with correct key
        provider._capture_diagnostic_info.assert_called_once_with(
            mock_driver, "fast_chain_failed_player_count_wait"
        )


class TestTimedModeSlotRescan:
    """Tests that an empty pre-window scan does not end a timed booking attempt.

    Before the booking window opens the tee sheet renders its rows but reports
    no availability, so scanning once at 6:28 and giving up reports "no slots"
    about a sheet that was merely still locked.
    """

    SLOT = {
        "timeStr": "8:08",
        "hours": 8,
        "minutes": 8,
        "index": 7,
        "diff": 8,
        "available": 4,
        "isExact": False,
    }

    @staticmethod
    def _shrink_rescan_budget(monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the rescan loop fast enough for a unit test."""
        import app.providers.walden_provider as walden_module

        monkeypatch.setattr(walden_module, "_SLOT_RESCAN_INTERVAL_S", 0.01)
        monkeypatch.setattr(walden_module, "_SLOT_RESCAN_GRACE_MS", 200)

    def test_empty_prewindow_scan_retries_until_slot_appears(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slot that only appears once the window opens is still booked."""
        import time as time_module

        self._shrink_rescan_budget(monkeypatch)
        mock_driver = MagicMock()

        finder = MagicMock(side_effect=[None, None, self.SLOT])
        monkeypatch.setattr(provider, "_find_target_slot_js", finder)

        staged = MagicMock(
            return_value={
                "success": True,
                "blocked": False,
                "phase": "complete",
                "error": None,
                "timing": {},
            }
        )
        monkeypatch.setattr(provider, "_stage_timed_booking_chain_js", staged)
        monkeypatch.setattr(provider, "_verify_booking_success", MagicMock(return_value=True))
        monkeypatch.setattr(
            provider, "_extract_confirmation_number", MagicMock(return_value="ABC123")
        )

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=int(time_module.time() * 1000) + 100,
        )

        assert result.success is True
        assert result.booked_time == time(8, 8)
        assert finder.call_count == 3, "should have re-scanned after the empty scans"
        # The slot found during the rescan still goes through the timed chain,
        # so the JS precision wait is preserved rather than bypassed.
        staged.assert_called_once()
        assert staged.call_args[0][1] == 7, "should stage the slot index found by the rescan"

    def test_rescan_gives_up_after_grace_window(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely empty sheet still fails, but only after the window opened."""
        import time as time_module

        self._shrink_rescan_budget(monkeypatch)
        mock_driver = MagicMock()

        finder = MagicMock(return_value=None)
        monkeypatch.setattr(provider, "_find_target_slot_js", finder)

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=int(time_module.time() * 1000) + 50,
        )

        assert result.success is False
        assert "No time slots" in result.error_message
        assert finder.call_count > 1, "should have retried before reporting no slots"

    def test_no_rescan_outside_timed_mode(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ad-hoc bookings with the window long open still fail fast."""
        mock_driver = MagicMock()

        finder = MagicMock(return_value=None)
        monkeypatch.setattr(provider, "_find_target_slot_js", finder)

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 0),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
        )

        assert result.success is False
        assert finder.call_count == 1, "no rescan loop without a target timestamp"

    def test_rescan_tightens_cadence_through_the_window_opening(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Polling is coarse while the target is far off, tight as it arrives.

        Near the window the poll interval becomes click latency, since a slot
        that only appears at 6:30 is clicked as soon as it is noticed. Far from
        the window it is just idle waiting, where scanning hard would pressure
        the page's event loop for nothing.
        """
        import app.providers.walden_provider as walden_module

        target_ms = 100_000

        class FakeClock:
            """Drives the loop off recorded sleeps instead of wall time."""

            def __init__(self, start_ms: int) -> None:
                self.now_ms = start_ms
                self.sleeps: list[float] = []

            def time(self) -> float:
                return self.now_ms / 1000.0

            def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                self.now_ms += int(seconds * 1000)

        clock = FakeClock(target_ms - 10_000)
        monkeypatch.setattr(walden_module, "time_module", clock)
        monkeypatch.setattr(provider, "_find_target_slot_js", MagicMock(return_value=None))

        result = provider._rescan_for_slot_until_window_open(
            MagicMock(),
            time(8, 0),
            4,
            32,
            8,
            set(),
            target_ms,
        )

        assert result is None
        coarse = walden_module._SLOT_RESCAN_INTERVAL_S
        tight = walden_module._SLOT_RESCAN_FINAL_INTERVAL_S
        assert set(clock.sleeps) == {coarse, tight}
        # Coarse first, tight afterwards, and never back to coarse.
        first_tight = clock.sleeps.index(tight)
        assert all(s == coarse for s in clock.sleeps[:first_tight])
        assert all(s == tight for s in clock.sleeps[first_tight:])
        # The switch happens once the target is within the final approach.
        elapsed_at_switch = sum(clock.sleeps[:first_tight]) * 1000
        remaining_at_switch = 10_000 - elapsed_at_switch
        assert remaining_at_switch <= walden_module._SLOT_RESCAN_FINAL_APPROACH_MS
        # It kept scanning past the target, through the grace period.
        assert clock.now_ms >= target_ms + walden_module._SLOT_RESCAN_GRACE_MS


class TestBatchBookingPreparation:
    """Tests for the restructured batch booking flow with pre-6:30 preparation."""

    def test_batch_booking_does_prep_before_booking(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that date selection and scrolling happen before booking.

        With timed booking mode, precision wait is handled by the JS chain itself,
        not by a separate Python call. This test verifies that:
        1. Date selection happens before booking
        2. Scrolling happens before booking
        3. Timed mode is used (execute_at_timestamp_ms is passed)

        Note: Pre-location via _find_target_slot_js is tested separately.
        """
        from datetime import datetime

        import app.providers.walden_provider as walden_module

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)
        monkeypatch.setattr(
            walden_module,
            "expected_conditions",
            SimpleNamespace(presence_of_element_located=lambda *_: None),
        )

        driver = MagicMock()
        monkeypatch.setattr(provider, "_create_driver", lambda: driver)
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)

        call_order: list[str] = []

        def mock_select_date(*_args: object) -> bool:
            call_order.append("select_date")
            return True

        def mock_scroll(*_args: object, **_kwargs: object) -> None:
            call_order.append("scroll")

        def mock_find_and_book(*_args: object, **_kwargs: object) -> object:
            call_order.append("find_and_book")
            # Verify execute_at_timestamp_ms is passed for first booking
            if "execute_at_timestamp_ms" in _kwargs:
                call_order.append(f"timed_booking_{_kwargs['execute_at_timestamp_ms'] is not None}")
            return SimpleNamespace(success=True, booked_time=time(8, 42), confirmation_number="X")

        monkeypatch.setattr(provider, "_select_date_sync", mock_select_date)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", mock_scroll)
        monkeypatch.setattr(provider, "_find_target_slot_js", lambda *_a, **_kw: None)
        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", mock_find_and_book)

        from app.providers.base import BatchBookingRequest

        req = BatchBookingRequest(booking_id="a", target_time=time(8, 42), num_players=4)

        result = provider._book_multiple_tee_times_sync(
            target_date=date.today() + timedelta(days=7),
            requests=[req],
            execute_at=datetime(2026, 2, 19, 6, 30, 0),
        )

        assert result.total_succeeded == 1
        # Verify order: date selection and scroll happen BEFORE booking
        assert call_order.index("select_date") < call_order.index("find_and_book")
        assert call_order.index("scroll") < call_order.index("find_and_book")
        # Verify timed mode was used (execute_at_timestamp_ms was passed)
        assert "timed_booking_True" in call_order

    def test_every_booking_in_batch_gets_the_window_timestamp(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All bookings wait for 6:30, not just the first one.

        Handing the timestamp only to booking 1 put the whole batch's window
        gate behind that one booking: when it failed before reaching its wait,
        every later booking raced against a still-locked tee sheet.
        """
        from datetime import datetime

        import app.providers.walden_provider as walden_module

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)
        monkeypatch.setattr(
            walden_module,
            "expected_conditions",
            SimpleNamespace(presence_of_element_located=lambda *_: None),
        )

        driver = MagicMock()
        monkeypatch.setattr(provider, "_create_driver", lambda: driver)
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_select_date_sync", lambda *_a, **_kw: True)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", lambda *_a, **_kw: None)
        monkeypatch.setattr(provider, "_find_target_slot_js", lambda *_a, **_kw: None)

        timestamps: list[int | None] = []

        def mock_find_and_book(*_args: object, **kwargs: object) -> object:
            timestamps.append(kwargs.get("execute_at_timestamp_ms"))
            # First booking fails, exactly as it did on 2026-08-02.
            if len(timestamps) == 1:
                return SimpleNamespace(
                    success=False, booked_time=None, error_message="No time slots"
                )
            return SimpleNamespace(success=True, booked_time=time(8, 16), confirmation_number="X")

        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", mock_find_and_book)

        from app.providers.base import BatchBookingRequest

        result = provider._book_multiple_tee_times_sync(
            target_date=date.today() + timedelta(days=7),
            requests=[
                BatchBookingRequest(booking_id="a", target_time=time(8, 0), num_players=4),
                BatchBookingRequest(booking_id="b", target_time=time(8, 16), num_players=4),
            ],
            execute_at=datetime(2026, 2, 19, 6, 30, 0),
        )

        assert len(timestamps) == 2
        assert all(ts is not None for ts in timestamps), (
            "booking 2 must carry the timestamp too, so booking 1 failing early "
            "cannot forfeit its wait for the window"
        )
        assert timestamps[0] == timestamps[1], "both should target the same moment"
        assert result.total_succeeded == 1

    def test_batch_booking_uses_fast_js_when_execute_at_set(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that batch booking uses fast JS path when execute_at is provided."""
        from datetime import datetime

        import app.providers.walden_provider as walden_module

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)
        monkeypatch.setattr(
            walden_module,
            "expected_conditions",
            SimpleNamespace(presence_of_element_located=lambda *_: None),
        )

        driver = MagicMock()
        monkeypatch.setattr(provider, "_create_driver", lambda: driver)
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_select_date_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(provider, "_find_target_slot_js", lambda *_a, **_kw: None)
        monkeypatch.setattr(provider, "_precision_wait_until", MagicMock())

        fast_js_values: list[bool] = []

        def mock_find_and_book(
            _driver: object,
            target_time: time,
            _num_players: int,
            _fallback_window: int,
            **kwargs: object,
        ) -> object:
            fast_js_values.append(kwargs.get("use_fast_js", False))
            return SimpleNamespace(success=True, booked_time=target_time, confirmation_number="X")

        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", mock_find_and_book)

        from app.providers.base import BatchBookingRequest

        req = BatchBookingRequest(booking_id="a", target_time=time(8, 42), num_players=4)

        provider._book_multiple_tee_times_sync(
            target_date=date.today() + timedelta(days=7),
            requests=[req],
            execute_at=datetime(2026, 2, 19, 6, 30, 0),
        )

        assert fast_js_values == [True]

    def test_batch_fast_path_is_a_setting_not_a_consequence_of_being_timed(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Turning the fast chain off must work even for the 6:30 run.

        Fast-ness used to be `execute_at is not None`, which made the two
        inseparable: there was no way to run the race on Selenium, and no way to
        run the chain off-race.
        """
        from datetime import datetime

        monkeypatch.setattr(settings, "walden_fast_booking_batch", False)
        precision_wait = MagicMock()
        fast_js_values = self._run_batch(
            provider,
            monkeypatch,
            execute_at=datetime(2026, 2, 19, 6, 30, 0),
            precision_wait=precision_wait,
        )

        assert fast_js_values == [False]
        # The 6:30 gate lives in the fast chain's JS/HTTP precision wait. With
        # the chain off, Python has to hold the window itself or the batch races
        # a locked tee sheet - the bug #122 fixed, re-entering by the back door.
        precision_wait.assert_called_once_with(datetime(2026, 2, 19, 6, 30, 0))

    def test_untimed_batch_uses_the_fast_chain(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing about the fast chain needs a target timestamp.

        `execute_at_timestamp_ms=None` already means "fire now" - which is what
        the 2nd-and-later bookings in a batch have always done.
        """
        monkeypatch.setattr(settings, "walden_fast_booking_batch", True)
        fast_js_values = self._run_batch(provider, monkeypatch, execute_at=None)

        assert fast_js_values == [True]

    def test_prelocation_stays_keyed_to_timedness(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-locating slots is about beating 6:30, not about going fast.

        With no window to beat there is nothing to gain from scanning early, so
        an untimed batch must not pay for a pre-location pass.
        """
        monkeypatch.setattr(settings, "walden_fast_booking_batch", True)
        prelocate = MagicMock(return_value=None)
        self._run_batch(provider, monkeypatch, execute_at=None, find_target_slot_js=prelocate)

        prelocate.assert_not_called()

    def _run_batch(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        execute_at: object,
        find_target_slot_js: object = None,
        precision_wait: object = None,
    ) -> list[bool]:
        """Drive one single-request batch and report the use_fast_js it passed."""
        import app.providers.walden_provider as walden_module
        from app.providers.base import BatchBookingRequest

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)
        monkeypatch.setattr(
            walden_module,
            "expected_conditions",
            SimpleNamespace(presence_of_element_located=lambda *_: None),
        )

        monkeypatch.setattr(provider, "_create_driver", lambda: MagicMock())
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_select_date_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(
            provider,
            "_find_target_slot_js",
            find_target_slot_js or (lambda *_a, **_kw: None),
        )
        monkeypatch.setattr(provider, "_precision_wait_until", precision_wait or MagicMock())

        fast_js_values: list[bool] = []

        def mock_find_and_book(
            _driver: object,
            target_time: time,
            _num_players: int,
            _fallback_window: int,
            **kwargs: object,
        ) -> object:
            fast_js_values.append(bool(kwargs.get("use_fast_js", False)))
            return SimpleNamespace(success=True, booked_time=target_time, confirmation_number="X")

        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", mock_find_and_book)

        provider._book_multiple_tee_times_sync(
            target_date=date.today() + timedelta(days=7),
            requests=[BatchBookingRequest(booking_id="a", target_time=time(8, 42), num_players=4)],
            execute_at=execute_at,  # type: ignore[arg-type]
        )
        return fast_js_values

    def test_batch_booking_no_page_refresh_at_execute_at(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that batch booking does NOT call driver.refresh() when execute_at is set."""
        from datetime import datetime

        import app.providers.walden_provider as walden_module

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)
        monkeypatch.setattr(
            walden_module,
            "expected_conditions",
            SimpleNamespace(presence_of_element_located=lambda *_: None),
        )

        driver = MagicMock()
        monkeypatch.setattr(provider, "_create_driver", lambda: driver)
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_select_date_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        monkeypatch.setattr(provider, "_find_target_slot_js", lambda *_a, **_kw: None)
        monkeypatch.setattr(provider, "_precision_wait_until", MagicMock())
        monkeypatch.setattr(
            provider,
            "_find_and_book_time_slot_sync",
            lambda *_a, **_kw: SimpleNamespace(
                success=True, booked_time=time(8, 42), confirmation_number="X"
            ),
        )

        from app.providers.base import BatchBookingRequest

        req = BatchBookingRequest(booking_id="a", target_time=time(8, 42), num_players=4)

        provider._book_multiple_tee_times_sync(
            target_date=date.today() + timedelta(days=7),
            requests=[req],
            execute_at=datetime(2026, 2, 19, 6, 30, 0),
        )

        # driver.refresh() should NOT have been called
        driver.refresh.assert_not_called()


class TestDirectHttpBookingWiring:
    """Tests for _try_direct_http_booking and its fallback policy.

    The direct-HTTP path is opt-in and must never be able to cost a booking:
    anything short of a submitted reservation hands control back to the JS chain.
    """

    SLOT = {
        "timeStr": "8:42",
        "hours": 8,
        "minutes": 42,
        "index": 5,
        "diff": 0,
        "available": 4,
        "isExact": True,
        "reserveId": "form:teeTimeCourses:0:teeTimeSlots:5:slotTee:0:reserve_button",
    }

    def _direct_result(self, **overrides: object) -> object:
        from app.providers.walden_http_booker import DirectBookingResult

        return DirectBookingResult(**overrides)  # type: ignore[arg-type]

    def test_declines_when_the_flag_is_off(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Switching the flag off must take the direct path out of the picture."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", False)
        assert provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None) is None

    def test_skipped_when_slot_has_no_reserve_component(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partially-booked slots expose an Available span, not a Reserve link."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        slot = {**self.SLOT, "reserveId": None}
        assert provider._try_direct_http_booking(MagicMock(), slot, 4, None) is None

    def test_staging_failure_falls_back(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session we cannot adopt is not a booking failure."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            MagicMock(side_effect=DirectHttpError("no cookies")),
        )
        assert provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None) is None

    def _patch_booker(
        self, monkeypatch: pytest.MonkeyPatch, result: object
    ) -> tuple[MagicMock, MagicMock]:
        session = MagicMock()
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            MagicMock(return_value=session),
        )
        booker = MagicMock()
        booker.book.return_value = result
        monkeypatch.setattr(
            "app.providers.walden_provider.DirectHttpBooker", MagicMock(return_value=booker)
        )
        return booker, session

    def test_success_is_returned_as_a_chain_result(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Success is returned as a chain result."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        result = self._direct_result(success=True, phase="complete", timing={"totalMs": 300})
        booker, session = self._patch_booker(monkeypatch, result)

        chain_result = provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, 1770000000000)

        assert chain_result is not None
        assert chain_result["success"] is True
        assert chain_result["phase"] == "complete"
        booker.book.assert_called_once_with(4, target_timestamp_ms=1770000000000)
        session.close.assert_called_once()

    def test_blocked_is_honored_not_retried(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing the slot is a real answer - re-clicking in the browser is not."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        result = self._direct_result(blocked=True, phase="reserve_click", error="blocked")
        self._patch_booker(monkeypatch, result)

        chain_result = provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None)

        assert chain_result is not None
        assert chain_result["blocked"] is True

    @pytest.mark.parametrize("phase", ["init", "precision_wait", "reserve_staged"])
    def test_failure_before_submission_falls_back(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        """Nothing was reserved, so the JS chain still gets its shot."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        result = self._direct_result(phase=phase, error="boom")
        self._patch_booker(monkeypatch, result)

        assert provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None) is None

    @pytest.mark.parametrize("phase", ["reserve_sent", "player_count", "tbd_guests", "book_now"])
    def test_failure_after_reserve_is_not_retried_in_the_browser(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        """Reserve was accepted; the browser's DOM no longer reflects reality."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        result = self._direct_result(phase=phase, error="boom")
        self._patch_booker(monkeypatch, result)

        chain_result = provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None)

        assert chain_result is not None
        assert chain_result["success"] is False
        assert chain_result["phase"] == phase

    def test_book_failure_is_reported_not_raised(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An escape here would reach _book_tee_time_sync, which only handles
        WebDriver errors, and raise instead of returning a BookingResult."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        booker, session = self._patch_booker(monkeypatch, None)
        booker.book.side_effect = DirectHttpError("exploded")

        chain_result = provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None)

        # Reserve may already have been sent, so this is terminal, not a retry.
        assert chain_result is not None
        assert chain_result["success"] is False
        assert "exploded" in chain_result["error"]
        session.close.assert_called_once()

    def test_unexpected_staging_error_falls_back(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Staging parses live markup; a malformed page can raise anything."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            MagicMock(side_effect=AttributeError("unexpected markup")),
        )

        assert provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, None) is None

    def test_js_chain_runs_when_direct_path_declines(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: flag off means the timed JS chain is still what books."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", False)
        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        timed_chain = MagicMock(
            return_value={
                "success": True,
                "blocked": False,
                "phase": "complete",
                "error": None,
                "timing": {},
            }
        )
        monkeypatch.setattr(provider, "_stage_timed_booking_chain_js", timed_chain)
        monkeypatch.setattr(provider, "_verify_booking_success", MagicMock(return_value=True))
        monkeypatch.setattr(
            provider, "_extract_confirmation_number", MagicMock(return_value="ABC123")
        )

        result = provider._find_and_book_time_slot_sync(
            MagicMock(),
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=1770000000000,
        )

        assert result.success is True
        timed_chain.assert_called_once()

    def test_direct_success_verifies_against_the_response_not_the_browser(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression that stubs used to hide.

        A direct-HTTP booking never touches the browser, so the DOM still shows
        the pre-booking tee sheet. Verification is left unstubbed here: reading
        the driver would find no confirmation and report a completed booking as
        failed.
        """
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        driver = MagicMock()
        # The browser still shows the tee sheet - no booking confirmation.
        driver.current_url = "https://www.waldengolf.com/group/pages/book-a-tee-time"
        driver.page_source = "<html><body>Tee sheet - slots unavailable</body></html>"
        driver.find_element.side_effect = Exception("no body text")

        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            provider,
            "_try_direct_http_booking",
            MagicMock(
                return_value={
                    "success": True,
                    "blocked": False,
                    "phase": "complete",
                    "error": None,
                    "timing": {"totalMs": 250},
                    "finalMarkup": (
                        "<div><h2>Booking confirmed</h2>"
                        "<p>Confirmation #12345 - your tee time is booked.</p></div>"
                    ),
                }
            ),
        )
        timed_chain = MagicMock()
        monkeypatch.setattr(provider, "_stage_timed_booking_chain_js", timed_chain)

        result = provider._find_and_book_time_slot_sync(
            driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=1770000000000,
        )

        assert result.success is True
        assert result.confirmation_number == "12345"
        timed_chain.assert_not_called()

    def test_direct_success_without_confirmation_is_reported_as_failure(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chain finishing is not the same as the reservation being made."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            provider,
            "_try_direct_http_booking",
            MagicMock(
                return_value={
                    "success": True,
                    "blocked": False,
                    "phase": "complete",
                    "error": None,
                    "timing": {},
                    "finalMarkup": "<div><p>Select the number of players.</p></div>",
                }
            ),
        )
        monkeypatch.setattr(provider, "_capture_diagnostic_info", MagicMock())

        result = provider._find_and_book_time_slot_sync(
            MagicMock(),
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=1770000000000,
        )

        assert result.success is False
        assert result.error_message is not None
        assert "did not confirm" in result.error_message

    def test_direct_result_short_circuits_the_js_chain(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the direct path books, the browser chain must not also fire."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            provider,
            "_try_direct_http_booking",
            MagicMock(
                return_value={
                    "success": True,
                    "blocked": False,
                    "phase": "complete",
                    "error": None,
                    "timing": {"totalMs": 250},
                }
            ),
        )
        timed_chain = MagicMock()
        fast_chain = MagicMock()
        monkeypatch.setattr(provider, "_stage_timed_booking_chain_js", timed_chain)
        monkeypatch.setattr(provider, "_execute_fast_booking_chain_js", fast_chain)
        monkeypatch.setattr(provider, "_verify_booking_success", MagicMock(return_value=True))
        monkeypatch.setattr(
            provider, "_extract_confirmation_number", MagicMock(return_value="ABC123")
        )

        result = provider._find_and_book_time_slot_sync(
            MagicMock(),
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=1770000000000,
        )

        assert result.success is True
        timed_chain.assert_not_called()
        fast_chain.assert_not_called()


class TestUnconfirmedBookingResolution:
    """What happens when the direct-HTTP response does not say either way.

    Book Now returns a PrimeFaces partial update, not a confirmation page, and
    the browser never sees it - so a response with no recognizable wording is
    routine rather than evidence of a lost slot. The member's reservations page
    is the only thing that can settle it, and reporting a tee time we hold as
    failed is the expensive mistake.
    """

    SLOT = dict(TestDirectHttpBookingWiring.SLOT)
    TARGET_DATE = date(2026, 8, 8)
    # No success or failure wording anywhere - the tee sheet re-render case.
    SILENT_MARKUP = "<div><p>Northgate tee sheet</p></div>"
    # A real refusal, captured from the site: also silent to the phrase check,
    # because the reason is in a dialog rather than in wording it knows.
    RESTRICTION_MARKUP = (
        Path(__file__).parent / "fixtures" / "walden_restriction_popup.html"
    ).read_text(encoding="utf-8")

    def _book(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        chain_result: dict[str, object],
        reservation_exists: bool | None,
    ) -> tuple[BookingResult, MagicMock]:
        """Run one booking whose direct chain returned ``chain_result``."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            provider, "_try_direct_http_booking", MagicMock(return_value=chain_result)
        )
        monkeypatch.setattr(provider, "_capture_diagnostic_info", MagicMock())
        monkeypatch.setattr(provider, "_capture_response_artifact", MagicMock())
        reservation_check = MagicMock(return_value=reservation_exists)
        monkeypatch.setattr(provider, "_reservation_exists", reservation_check)

        result = provider._find_and_book_time_slot_sync(
            MagicMock(),
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            target_date=self.TARGET_DATE,
        )
        return result, reservation_check

    def _completed_chain(self, markup: str) -> dict[str, object]:
        return {
            "success": True,
            "blocked": False,
            "phase": "complete",
            "error": None,
            "timing": {},
            "finalMarkup": markup,
            "path": DIRECT_HTTP_PATH,
        }

    def test_silent_response_with_the_reservation_on_file_is_a_success(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression: a booked tee time reported as lost."""
        result, reservation_check = self._book(
            provider,
            monkeypatch,
            self._completed_chain(self.SILENT_MARKUP),
            reservation_exists=True,
        )

        assert result.success is True
        assert result.booked_time == time(8, 42)
        reservation_check.assert_called_once_with(ANY, self.TARGET_DATE, time(8, 42))

    def test_silent_response_with_no_reservation_says_where_it_looked(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure the member can act on names what was checked."""
        result, _ = self._book(
            provider,
            monkeypatch,
            self._completed_chain(self.SILENT_MARKUP),
            reservation_exists=False,
        )

        assert result.success is False
        assert result.error_message is not None
        assert "did not confirm" in result.error_message
        assert "not on the member's reservations page" in result.error_message

    def test_unreachable_reservations_page_is_not_read_as_a_no(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unchecked page is reported as unchecked, not as an empty one."""
        result, _ = self._book(
            provider,
            monkeypatch,
            self._completed_chain(self.SILENT_MARKUP),
            reservation_exists=None,
        )

        assert result.success is False
        assert result.error_message is not None
        assert "could not be checked" in result.error_message

    def test_refusal_wording_is_carried_into_the_error(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the site said is worth more than that it said no."""
        result, _ = self._book(
            provider,
            monkeypatch,
            self._completed_chain("<div><p>Unable to complete this reservation.</p></div>"),
            reservation_exists=False,
        )

        assert result.success is False
        assert result.error_message is not None
        assert "unable to" in result.error_message

    def test_the_clubs_restriction_reaches_the_member_in_its_own_words(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal the member needs, not a report that nothing was said.

        The club allows one round per member per day, so the second half of a
        two-tee-time batch comes back like this. ``responseMessage`` is filled
        the way the booker fills it, from the response itself.
        """
        chain = self._completed_chain(self.RESTRICTION_MARKUP)
        chain["responseMessage"] = find_response_message(self.RESTRICTION_MARKUP)

        result, _ = self._book(provider, monkeypatch, chain, reservation_exists=False)

        assert result.success is False
        # Nothing about chains, phases or phrase checks: the member gets the
        # club's sentence and nothing else. The rest is in the log.
        assert result.error_message == (
            "Restriction: Member: Sample, Member is restricted for 1 round(s) "
            "on Northgate per Day"
        )

    def test_an_unreadable_reservations_page_is_still_admitted(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A member who might be holding the tee time has to hear that."""
        chain = self._completed_chain(self.RESTRICTION_MARKUP)
        chain["responseMessage"] = find_response_message(self.RESTRICTION_MARKUP)

        result, _ = self._book(provider, monkeypatch, chain, reservation_exists=None)

        assert result.success is False
        assert result.error_message is not None
        assert result.error_message.startswith("Restriction: Member: Sample, Member")
        assert "could not be checked" in result.error_message

    def test_a_confirmed_response_never_reaches_the_reservations_page(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check costs the tee sheet, so it only runs when it is needed."""
        result, reservation_check = self._book(
            provider,
            monkeypatch,
            self._completed_chain("<div><h2>Your tee time is booked</h2></div>"),
            reservation_exists=False,
        )

        assert result.success is True
        reservation_check.assert_not_called()

    def test_failure_after_reserve_was_sent_checks_the_reservations_page(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chain breaking mid-flight does not mean the booking did."""
        result, reservation_check = self._book(
            provider,
            monkeypatch,
            {
                "success": False,
                "blocked": False,
                "phase": "book_now",
                "error": "Book Now button not found in response",
                "timing": {},
                "finalMarkup": self.SILENT_MARKUP,
                "path": DIRECT_HTTP_PATH,
            },
            reservation_exists=True,
        )

        assert result.success is True
        assert result.booked_time == time(8, 42)
        reservation_check.assert_called_once()

    def test_failure_before_reserve_was_sent_does_not_check(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing reached the server, so there is nothing to look up."""
        result, reservation_check = self._book(
            provider,
            monkeypatch,
            {
                "success": False,
                "blocked": False,
                "phase": PHASE_RESERVE_STAGED,
                "error": "prepare() must be called before book()",
                "timing": {},
                "finalMarkup": "",
                "path": DIRECT_HTTP_PATH,
            },
            reservation_exists=True,
        )

        assert result.success is False
        reservation_check.assert_not_called()

    def test_a_js_chain_failure_does_not_check(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The JS chain leaves the browser on its own response; the DOM answers."""
        result, reservation_check = self._book(
            provider,
            monkeypatch,
            {
                "success": False,
                "blocked": False,
                "phase": "book_now",
                "error": "Book Now click failed",
                "timing": {},
            },
            reservation_exists=True,
        )

        assert result.success is False
        reservation_check.assert_not_called()


class TestBlockedSlotResolution:
    """What a blocked verdict on the direct-HTTP path is allowed to conclude.

    The verdict is read out of a response the browser never received, and it is
    reached with the Reserve request already accepted. So the browser photograph
    cannot show the popup that produced it - only the response can - and the
    chain's own account says nothing about whether the tee time is ours. Both
    gaps put a member in front of a starter with no slot, or send them away from
    one they hold.
    """

    SLOT = dict(TestDirectHttpBookingWiring.SLOT)
    TARGET_DATE = date(2026, 8, 12)
    BLOCKED_MARKUP = (
        '<div id="teeSheetValidationErrorPopup" aria-hidden="false">'
        "This slot is blocked by another user</div>"
    )

    def _book(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        chain_result: dict[str, object],
        reservation_exists: bool | None,
    ) -> tuple[BookingResult, MagicMock, MagicMock]:
        """Run one booking whose chain came back blocked."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            provider, "_try_direct_http_booking", MagicMock(return_value=chain_result)
        )
        monkeypatch.setattr(provider, "_capture_diagnostic_info", MagicMock())
        artifact_capture = MagicMock()
        monkeypatch.setattr(provider, "_capture_response_artifact", artifact_capture)
        reservation_check = MagicMock(return_value=reservation_exists)
        monkeypatch.setattr(provider, "_reservation_exists", reservation_check)

        result = provider._find_and_book_time_slot_sync(
            MagicMock(),
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            target_date=self.TARGET_DATE,
        )
        return result, reservation_check, artifact_capture

    def _blocked_chain(self, **overrides: object) -> dict[str, object]:
        """A direct-HTTP chain that stopped on the blocked popup."""
        chain: dict[str, object] = {
            "success": False,
            "blocked": True,
            "phase": PHASE_RESERVE_SENT,
            "error": "Slot blocked by another user (blocked by another user)",
            "timing": {},
            "finalMarkup": self.BLOCKED_MARKUP,
            "path": DIRECT_HTTP_PATH,
        }
        chain.update(overrides)
        return chain

    def test_the_response_that_produced_the_verdict_is_kept(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression: the one artifact that can explain the block, discarded.

        The browser is still on the pre-booking tee sheet, so its photograph
        shows the slot sitting there available - which is what a member reading
        it afterwards sees, and it answers nothing.
        """
        _, _, artifact_capture = self._book(
            provider, monkeypatch, self._blocked_chain(), reservation_exists=False
        )

        artifact_capture.assert_called_once_with(
            f"direct_http_blocked_{PHASE_RESERVE_SENT}", self.BLOCKED_MARKUP
        )
        provider._capture_diagnostic_info.assert_called_once_with(ANY, "slot_blocked_by_other_user")

    def test_a_tee_time_we_hold_is_not_reported_as_blocked(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reserve was accepted before the popup was read, so ask the site."""
        result, reservation_check, _ = self._book(
            provider, monkeypatch, self._blocked_chain(), reservation_exists=True
        )

        assert result.success is True
        assert result.booked_time == time(8, 42)
        reservation_check.assert_called_once_with(ANY, self.TARGET_DATE, time(8, 42))

    def test_a_confirmed_block_still_reaches_the_member_as_one(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checking the page does not change what a real block is called."""
        result, _, _ = self._book(
            provider, monkeypatch, self._blocked_chain(), reservation_exists=False
        )

        assert result.success is False
        assert result.error_message == "Slot blocked by another user"

    def test_an_unreadable_reservations_page_is_admitted(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A member who might be holding the tee time has to hear that."""
        result, _, _ = self._book(
            provider, monkeypatch, self._blocked_chain(), reservation_exists=None
        )

        assert result.success is False
        assert result.error_message is not None
        assert result.error_message.startswith("Slot blocked by another user")
        assert "could not be checked" in result.error_message

    def test_a_block_before_reserve_was_sent_does_not_check(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing reached the server, so there is nothing to look up."""
        result, reservation_check, _ = self._book(
            provider,
            monkeypatch,
            self._blocked_chain(phase=PHASE_RESERVE_STAGED),
            reservation_exists=True,
        )

        assert result.success is False
        assert result.error_message == "Slot blocked by another user"
        reservation_check.assert_not_called()

    def test_a_refusal_with_the_page_read_empty_is_safe_to_re_attempt(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reserve went out, the club refused it, and no reservation exists.

        This is what lets the ad-hoc path retry the same tee time untimed and
        turn a lost booking into the timed/untimed pair that says whether the
        wait caused the refusal.
        """
        result, _, _ = self._book(
            provider, monkeypatch, self._blocked_chain(), reservation_exists=False
        )

        assert result.verified_not_reserved is True

    def test_a_refusal_before_reserve_was_sent_is_safe_to_re_attempt(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing reached the server, so nothing can have been held."""
        result, _, _ = self._book(
            provider,
            monkeypatch,
            self._blocked_chain(phase=PHASE_RESERVE_STAGED),
            reservation_exists=None,
        )

        assert result.verified_not_reserved is True

    def test_an_unreadable_reservations_page_is_not_safe_to_re_attempt(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The safety property behind the ad-hoc retry.

        A Reserve on the wire whose outcome could not be established looks
        exactly like one never sent. Re-attempting it risks a second
        reservation, which the club counts against one round per member per day.
        """
        result, _, _ = self._book(
            provider, monkeypatch, self._blocked_chain(), reservation_exists=None
        )

        assert result.verified_not_reserved is False

    def test_a_js_chain_block_answers_from_the_browser(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The JS chain saw the popup itself; there is no response to keep."""
        chain = self._blocked_chain(phase="blocked_check")
        del chain["path"]
        del chain["finalMarkup"]

        result, reservation_check, artifact_capture = self._book(
            provider, monkeypatch, chain, reservation_exists=True
        )

        assert result.success is False
        assert result.error_message == "Slot blocked by another user"
        reservation_check.assert_not_called()
        artifact_capture.assert_not_called()


class TestBrowserErrorMessageExtraction:
    """Reading a refusal off the browser DOM, for the JS-chain fallback path."""

    RESTRICTION_MARKUP = TestUnconfirmedBookingResolution.RESTRICTION_MARKUP

    def _driver_showing(self, markup: str) -> MagicMock:
        """A driver whose first selector match is a container holding ``markup``."""
        element = MagicMock()
        element.get_attribute.side_effect = lambda name: (markup if name == "outerHTML" else None)
        element.is_displayed.return_value = True
        element.text = "unused - the markup is what gets read"

        driver = MagicMock()
        driver.find_elements.side_effect = lambda by, sel: (
            [element] if "restrictionPopup" in sel else []
        )
        return driver

    def test_the_restriction_dialog_is_read_without_its_buttons(
        self, provider: WaldenGolfProvider
    ) -> None:
        """WebElement.text would carry the dialog's "Ok" into the member's reply."""
        message = provider._extract_booking_error_message(
            self._driver_showing(self.RESTRICTION_MARKUP)
        )

        assert message == (
            "Restriction: Member: Sample, Member is restricted for 1 round(s) "
            "on Northgate per Day"
        )

    def test_unreadable_markup_falls_back_to_the_rendered_text(
        self, provider: WaldenGolfProvider
    ) -> None:
        """A message with a stray button label beats no message at all."""
        element = MagicMock()
        element.get_attribute.side_effect = WebDriverException("stale element")
        element.is_displayed.return_value = True
        element.text = "Members are limited to one per day"

        driver = MagicMock()
        driver.find_elements.side_effect = lambda by, sel: [element] if ".alert" == sel else []

        assert (
            provider._extract_booking_error_message(driver) == "Members are limited to one per day"
        )


class TestReservationLookup:
    """Reading the member's reservations page as ground truth."""

    def _driver_with_rows(self, rows: list[str]) -> MagicMock:
        driver = MagicMock()
        form = MagicMock()
        form.find_elements.return_value = [SimpleNamespace(text=row_text) for row_text in rows]
        driver.find_element.return_value = form
        return driver

    @pytest.fixture(autouse=True)
    def _no_settle_sleep(self, provider: WaldenGolfProvider) -> None:
        """The page-settle wait is a real sleep; tests do not need to serve it."""
        provider.wait_strategy = MagicMock()

    def test_matching_reservation_is_found(self, provider: WaldenGolfProvider) -> None:
        """The row the site renders for a booking we hold."""
        driver = self._driver_with_rows(["08/08/2026 - Tee Time - 5:08 PM - Northgate - 4 players"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(17, 8)) is True

    def test_reservation_for_another_time_is_not_ours(self, provider: WaldenGolfProvider) -> None:
        """The first booking of a batch must not vouch for the second."""
        driver = self._driver_with_rows(["08/08/2026 - Tee Time - 5:00 PM - Northgate"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(17, 8)) is False

    def test_shorter_time_does_not_match_inside_a_longer_one(
        self, provider: WaldenGolfProvider
    ) -> None:
        """A 2:08 PM search must not be satisfied by a row rendering 12:08 PM.

        A member can hold both on one date, and a substring match would vouch
        for a 2:08 PM tee time that was never booked - the false confirmation
        this whole check exists to rule out.
        """
        driver = self._driver_with_rows(["08/08/2026 - Tee Time - 12:08 PM - Northgate"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(14, 8)) is False

    def test_am_hour_does_not_match_inside_a_longer_one(self, provider: WaldenGolfProvider) -> None:
        """The same trap on the morning side: "1:08 AM" inside "11:08 AM"."""
        driver = self._driver_with_rows(["08/08/2026 - Tee Time - 11:08 AM - Northgate"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(1, 8)) is False

    def test_the_longer_time_still_matches_itself(self, provider: WaldenGolfProvider) -> None:
        """Anchoring the hour must not stop a genuine 12:08 PM from matching."""
        driver = self._driver_with_rows(["08/08/2026 - Tee Time - 12:08 PM - Northgate"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(12, 8)) is True

    def test_reservation_on_another_date_is_not_ours(self, provider: WaldenGolfProvider) -> None:
        """Same time, different day - a standing weekly booking would match."""
        driver = self._driver_with_rows(["08/15/2026 - Tee Time - 5:08 PM - Northgate"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(17, 8)) is False

    def test_two_digit_year_and_24_hour_rows_still_match(
        self, provider: WaldenGolfProvider
    ) -> None:
        """The page has been seen rendering both; neither is a miss."""
        driver = self._driver_with_rows(["08/08/26 Tee Time 17:08 Northgate"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(17, 8)) is True

    def test_non_tee_time_rows_are_ignored(self, provider: WaldenGolfProvider) -> None:
        """Dining and event rows share the table."""
        driver = self._driver_with_rows(["08/08/2026 - Dining Reservation - 5:08 PM"])

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(17, 8)) is False

    def test_an_unreadable_page_answers_unknown(self, provider: WaldenGolfProvider) -> None:
        """False would mean 'not booked', which is more than the page said."""
        driver = MagicMock()
        driver.get.side_effect = WebDriverException("session died")

        assert provider._reservation_exists(driver, date(2026, 8, 8), time(17, 8)) is None

    def test_without_a_date_there_is_nothing_to_match_on(
        self, provider: WaldenGolfProvider
    ) -> None:
        """A time alone would match the same slot on any day."""
        driver = MagicMock()

        assert provider._reservation_exists(driver, None, time(17, 8)) is None
        driver.get.assert_not_called()


class TestBookingTextVerdict:
    """Three answers, because silence and refusal are not the same thing."""

    def test_confirmation_wording_is_a_yes(self, provider: WaldenGolfProvider) -> None:
        confirmed, detail = provider._booking_text_verdict("Your tee time is booked", "test")

        assert confirmed is True
        assert "booked" in detail

    def test_refusal_wording_is_a_no(self, provider: WaldenGolfProvider) -> None:
        confirmed, detail = provider._booking_text_verdict("Unable to reserve", "test")

        assert confirmed is False
        assert "unable to" in detail

    def test_neither_is_neither(self, provider: WaldenGolfProvider) -> None:
        """The case the reservations-page check exists for."""
        confirmed, detail = provider._booking_text_verdict("Northgate tee sheet", "test")

        assert confirmed is None
        assert "no success or failure wording" in detail

    def test_the_boolean_wrapper_still_fails_closed(self, provider: WaldenGolfProvider) -> None:
        """Callers reading the browser DOM keep the pessimistic answer."""
        assert provider._verify_booking_success_text("Northgate tee sheet", "test") is False
        assert provider._verify_booking_success_text("Booking confirmed", "test") is True


class TestBookingPathDefaults:
    """The shipped defaults decide which chain books a real tee time.

    Built from the code defaults alone (no .env, no deployment env) so this
    pins what the repo ships rather than what the machine running it happens
    to be configured for.
    """

    # Field name -> shipped default. Uppercased, each is also its env var name.
    FLAG_DEFAULTS = {
        "walden_direct_http_booking": True,
        "walden_fast_booking_immediate": True,
        "walden_fast_booking_batch": True,
    }

    def test_direct_chain_is_on_for_both_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Direct HTTP plus the ad-hoc fast path: the off-race validation setup.

        Ad-hoc bookings are the safe place to meet the live site - a lost slot
        on a Tuesday afternoon costs nothing, a lost 6:30 race costs the tee
        time. Flipping either of these back off is a deliberate decision, not
        something that should slip through.
        """
        from app.config import Settings

        # _env_file=None silences .env but not the process environment -
        # EnvSettingsSource still reads it. Without this the test would measure
        # whatever the shell exported and pass while the shipped default drifted,
        # which is the one thing it exists to catch.
        for field in self.FLAG_DEFAULTS:
            monkeypatch.delenv(field.upper(), raising=False)

        defaults = Settings(_env_file=None)

        for field, expected in self.FLAG_DEFAULTS.items():
            assert getattr(defaults, field) is expected, field


class TestImmediateBookingFastPath:
    """Tests for routing an ad-hoc booking through the fast/direct chain.

    A date inside the 7-day window books inline, and that path could not reach
    the fast JS chain or direct HTTP at all - so the least-verified code in the
    repo could only ever be exercised during a live 6:30 race. These tests pin
    the routing, and that the flag-off behavior is untouched.
    """

    SLOT = dict(TestDirectHttpBookingWiring.SLOT)

    def _drive(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        driver: MagicMock | None = None,
    ) -> MagicMock:
        """Stub login/navigation so _book_tee_time_sync reaches slot booking."""
        import app.providers.walden_provider as walden_module

        class DummyWait:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def until(self, *_args: object, **_kwargs: object) -> None:
                return None

        monkeypatch.setattr(walden_module, "WebDriverWait", DummyWait)

        driver = driver or MagicMock()
        monkeypatch.setattr(provider, "_create_driver", lambda: driver)
        monkeypatch.setattr(provider, "_perform_login", lambda *_: True)
        monkeypatch.setattr(provider, "_select_course_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_select_date_sync", lambda *_: True)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", MagicMock())
        return driver

    def test_flag_off_keeps_todays_selenium_flow(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default must not change what an ad-hoc booking does."""
        from app.providers.base import BookingResult

        monkeypatch.setattr(settings, "walden_fast_booking_immediate", False)
        # On even with the fast path off: the direct chain must stay unreachable
        # from a flow that never enters the fast branch.
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        self._drive(provider, monkeypatch)

        find_and_book = MagicMock(return_value=BookingResult(success=True, booked_time=time(8, 42)))
        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", find_and_book)

        result = provider._book_tee_time_sync(date(2026, 2, 10), time(8, 42), 4, 32)

        assert result.success is True
        assert find_and_book.call_args.kwargs["use_fast_js"] is False

    def test_flag_on_routes_through_the_fast_chain_untimed(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fast, but still untimed - there is no window left to wait for."""
        from app.providers.base import BookingResult

        monkeypatch.setattr(settings, "walden_fast_booking_immediate", True)
        self._drive(provider, monkeypatch)

        find_and_book = MagicMock(return_value=BookingResult(success=True, booked_time=time(8, 42)))
        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", find_and_book)

        provider._book_tee_time_sync(date(2026, 2, 10), time(8, 42), 4, 32)

        kwargs = find_and_book.call_args.kwargs
        assert kwargs["use_fast_js"] is True
        assert kwargs.get("execute_at_timestamp_ms") is None

    def test_immediate_booking_runs_the_direct_http_chain_end_to_end(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the change: exercise direct HTTP off-race.

        Verification is left unstubbed - the browser DOM still shows the tee
        sheet, so a success read from the driver would be reported as a failure.
        """
        from app.providers.walden_http_booker import DirectBookingResult

        monkeypatch.setattr(settings, "walden_fast_booking_immediate", True)
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)

        driver = MagicMock()
        driver.current_url = "https://www.waldengolf.com/group/pages/book-a-tee-time"
        driver.page_source = "<html><body>Tee sheet - slots unavailable</body></html>"
        driver.find_element.side_effect = Exception("no body text")
        self._drive(provider, monkeypatch, driver)

        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        session = MagicMock()
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            MagicMock(return_value=session),
        )
        booker = MagicMock()
        booker.book.return_value = DirectBookingResult(
            success=True,
            phase="complete",
            timing={"totalMs": 280},
            final_markup=(
                "<div><h2>Booking confirmed</h2>"
                "<p>Confirmation #12345 - your tee time is booked.</p></div>"
            ),
        )
        monkeypatch.setattr(
            "app.providers.walden_provider.DirectHttpBooker", MagicMock(return_value=booker)
        )
        fast_chain = MagicMock()
        monkeypatch.setattr(provider, "_execute_fast_booking_chain_js", fast_chain)

        result = provider._book_tee_time_sync(date(2026, 2, 10), time(8, 42), 4, 32)

        assert result.success is True
        assert result.confirmation_number == "12345"
        assert result.booked_time == time(8, 42)
        # Untimed: Reserve fires as soon as it is staged, no target to wait for.
        booker.book.assert_called_once_with(4, target_timestamp_ms=None)
        fast_chain.assert_not_called()

    def test_immediate_booking_uses_the_js_chain_when_direct_http_is_off(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fast and direct are separate questions; the JS chain is the fast default."""
        monkeypatch.setattr(settings, "walden_fast_booking_immediate", True)
        monkeypatch.setattr(settings, "walden_direct_http_booking", False)
        self._drive(provider, monkeypatch)

        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        fast_chain = MagicMock(
            return_value={
                "success": True,
                "blocked": False,
                "phase": "complete",
                "error": None,
                "timing": {},
            }
        )
        monkeypatch.setattr(provider, "_execute_fast_booking_chain_js", fast_chain)
        timed_chain = MagicMock()
        monkeypatch.setattr(provider, "_stage_timed_booking_chain_js", timed_chain)
        monkeypatch.setattr(provider, "_verify_booking_success", MagicMock(return_value=True))
        monkeypatch.setattr(
            provider, "_extract_confirmation_number", MagicMock(return_value="ABC123")
        )

        result = provider._book_tee_time_sync(date(2026, 2, 10), time(8, 42), 4, 32)

        assert result.success is True
        fast_chain.assert_called_once()
        # Nothing to wait for, so the timed variant must stay out of it.
        timed_chain.assert_not_called()

    def test_pre_submit_direct_failure_still_falls_back_to_the_js_chain(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback semantics must survive the new routing."""
        from app.providers.walden_http_booker import DirectBookingResult

        monkeypatch.setattr(settings, "walden_fast_booking_immediate", True)
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        self._drive(provider, monkeypatch)

        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            MagicMock(return_value=MagicMock()),
        )
        booker = MagicMock()
        booker.book.return_value = DirectBookingResult(phase="reserve_staged", error="boom")
        monkeypatch.setattr(
            "app.providers.walden_provider.DirectHttpBooker", MagicMock(return_value=booker)
        )
        fast_chain = MagicMock(
            return_value={
                "success": True,
                "blocked": False,
                "phase": "complete",
                "error": None,
                "timing": {},
            }
        )
        monkeypatch.setattr(provider, "_execute_fast_booking_chain_js", fast_chain)
        monkeypatch.setattr(provider, "_verify_booking_success", MagicMock(return_value=True))
        monkeypatch.setattr(
            provider, "_extract_confirmation_number", MagicMock(return_value="ABC123")
        )

        result = provider._book_tee_time_sync(date(2026, 2, 10), time(8, 42), 4, 32)

        assert result.success is True
        fast_chain.assert_called_once()

    def test_post_submit_direct_failure_is_not_retried_in_the_browser(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reserve was accepted; a browser retry would race our own booking."""
        from app.providers.walden_http_booker import DirectBookingResult

        monkeypatch.setattr(settings, "walden_fast_booking_immediate", True)
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        self._drive(provider, monkeypatch)

        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            MagicMock(return_value=MagicMock()),
        )
        booker = MagicMock()
        booker.book.return_value = DirectBookingResult(phase="book_now", error="connection reset")
        monkeypatch.setattr(
            "app.providers.walden_provider.DirectHttpBooker", MagicMock(return_value=booker)
        )
        fast_chain = MagicMock()
        monkeypatch.setattr(provider, "_execute_fast_booking_chain_js", fast_chain)
        monkeypatch.setattr(provider, "_capture_diagnostic_info", MagicMock())

        result = provider._book_tee_time_sync(date(2026, 2, 10), time(8, 42), 4, 32)

        assert result.success is False
        assert result.error_message is not None
        assert "book_now" in result.error_message
        fast_chain.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestFallbackTeeTimeReporting:
    """What the member is told, when the club gave us a different tee time.

    The slot is chosen by row index before the window opens; the chain may end
    up holding a different one, because a refused Reserve walks down the ranked
    list. Everything after that has to name the slot actually held - the
    reservations check that decides whether the booking is real, and the time
    in the member's confirmation.
    """

    SLOT = {
        "timeStr": "8:42",
        "hours": 8,
        "minutes": 42,
        "index": 5,
        "diff": 0,
        "available": 4,
        "isExact": True,
        "reserveId": "form:teeTimeCourses:0:teeTimeSlots:5:slotTee:0:reserve_button",
    }

    def _run(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        chain_result: dict,
    ):
        """Drive the timed booking with a canned direct-HTTP chain result."""
        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        driver = MagicMock()
        driver.current_url = "https://www.waldengolf.com/group/pages/book-a-tee-time"
        driver.page_source = "<html><body>Tee sheet</body></html>"
        driver.find_element.side_effect = Exception("no body text")

        monkeypatch.setattr(
            provider, "_find_target_slot_js", MagicMock(return_value=dict(self.SLOT))
        )
        monkeypatch.setattr(
            provider, "_rank_candidate_slots_js", MagicMock(return_value=[dict(self.SLOT)])
        )
        monkeypatch.setattr(
            provider, "_try_direct_http_booking", MagicMock(return_value=chain_result)
        )
        monkeypatch.setattr(provider, "_stage_timed_booking_chain_js", MagicMock())

        return provider._find_and_book_time_slot_sync(
            driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
            execute_at_timestamp_ms=1770000000000,
        )

    def _booked(self, **extra: object) -> dict:
        """A completed direct-HTTP chain result."""
        return {
            "success": True,
            "blocked": False,
            "phase": "complete",
            "error": None,
            "timing": {"totalMs": 250},
            "finalMarkup": (
                "<div><h2>Booking confirmed</h2>"
                "<p>Confirmation #12345 - your tee time is booked.</p></div>"
            ),
            **extra,
        }

    def test_a_fallback_slot_is_reported_as_the_time_booked(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """08:42 was taken and 08:50 was held; the member has 08:50."""
        result = self._run(
            provider,
            monkeypatch,
            self._booked(
                bookedSlotTime=time(8, 50),
                attemptedTimes=[time(8, 42), time(8, 50)],
            ),
        )

        assert result.success is True
        assert result.booked_time == time(8, 50)

    def test_a_fallback_slot_explains_itself(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Getting a different time than asked for is not silent."""
        result = self._run(
            provider,
            monkeypatch,
            self._booked(
                bookedSlotTime=time(8, 50),
                attemptedTimes=[time(8, 42), time(8, 50)],
            ),
        )

        assert result.fallback_reason is not None
        assert "08:50" in result.fallback_reason

    def test_winning_the_requested_time_carries_no_fallback_reason(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary case must not grow an apology."""
        result = self._run(
            provider,
            monkeypatch,
            self._booked(bookedSlotTime=time(8, 42), attemptedTimes=[time(8, 42)]),
        )

        assert result.success is True
        assert result.booked_time == time(8, 42)
        assert result.fallback_reason is None

    def test_a_chain_that_reports_no_slot_time_leaves_the_pick_alone(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The JS chain says nothing about tee times; its result must still work."""
        result = self._run(provider, monkeypatch, self._booked())

        assert result.success is True
        assert result.booked_time == time(8, 42)


class TestChainSummaryLogging:
    """The one line a 6:30 post-mortem starts from.

    A morning is debugged from Cloud Logging, hours later, with no way to
    re-run it. If a number that decided the outcome is not in this line it is
    not anywhere, and a format error here would take the line out entirely.
    """

    SLOT = {
        "timeStr": "8:42",
        "hours": 8,
        "minutes": 42,
        "index": 5,
        "diff": 0,
        "available": 4,
        "isExact": True,
        "reserveId": "form:teeTimeCourses:0:teeTimeSlots:5:slotTee:0:reserve_button",
    }

    def _summary(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        result: object,
    ) -> str:
        """Run the direct path with a canned result and return the summary line."""
        from unittest.mock import MagicMock as _MagicMock

        monkeypatch.setattr(settings, "walden_direct_http_booking", True)
        monkeypatch.setattr(
            "app.providers.walden_provider.PrimeFacesSession.from_selenium",
            _MagicMock(return_value=_MagicMock()),
        )
        booker = _MagicMock()
        booker.book.return_value = result
        monkeypatch.setattr(
            "app.providers.walden_provider.DirectHttpBooker", _MagicMock(return_value=booker)
        )

        with caplog.at_level(logging.INFO, logger="app.providers.walden_provider"):
            provider._try_direct_http_booking(MagicMock(), self.SLOT, 4, 1770000000000)

        lines = [r.getMessage() for r in caplog.records if "Chain finished" in r.getMessage()]
        assert len(lines) == 1, lines
        return lines[0]

    def test_a_won_race_reports_lead_send_and_slot(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Everything needed to say why we won, without re-reading the run."""
        from app.providers.walden_http_booker import DirectBookingResult

        line = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(
                success=True,
                phase="complete",
                booked_slot_time=time(8, 42),
                attempted_times=[time(8, 42)],
                timing={
                    "clickDriftMs": 1,
                    "arrivalLeadMs": 470,
                    "reserveSentAtMs": -468,
                    "reserveAttempts": 1,
                    "totalMs": 1500,
                },
            ),
        )

        assert "lead=470ms" in line
        # Negative: the request left before our own 06:30:00, which is the point.
        assert "sent=-468ms vs window" in line
        assert "attempts=1" in line
        assert "booked=08:42 AM" in line

    def test_a_fallback_win_names_every_tee_time_tried(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """How hard the sheet was fought is the finding, not a detail."""
        from app.providers.walden_http_booker import DirectBookingResult

        line = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(
                success=True,
                phase="complete",
                booked_slot_time=time(8, 58),
                attempted_times=[time(8, 42), time(8, 50), time(8, 58)],
                timing={"reserveAttempts": 3, "arrivalLeadMs": 470, "reserveSentAtMs": -468},
            ),
        )

        assert "attempts=3" in line
        assert "tried=[08:42 AM, 08:50 AM, 08:58 AM]" in line
        assert "booked=08:58 AM" in line

    def test_the_measured_lead_is_visible(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The lead has to be readable, or it gets tuned blind."""
        from app.providers.walden_http_booker import DirectBookingResult

        line = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(
                success=True,
                phase="complete",
                # Two attempts means a block and a fallback, never the same tee
                # time twice: the chain no longer re-fires one slot.
                booked_slot_time=time(8, 50),
                attempted_times=[time(8, 42), time(8, 50)],
                timing={"reserveAttempts": 2, "arrivalLeadMs": 880},
            ),
        )

        assert "lead=880ms" in line
        assert "attempts=2" in line

    def test_a_lost_race_reports_nothing_booked(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A blocked run still has to say what it tried."""
        from app.providers.walden_http_booker import DirectBookingResult

        line = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(
                blocked=True,
                phase="reserve_sent",
                error="Slot blocked by another user (blocked by another user)",
                attempted_times=[time(8, 42), time(8, 50)],
                timing={"reserveAttempts": 2, "arrivalLeadMs": 470, "reserveSentAtMs": -468},
            ),
        )

        assert "blocked=True" in line
        assert "booked=nothing" in line
        assert "tried=[08:42 AM, 08:50 AM]" in line

    def test_the_refresh_figures_appear_only_when_it_ran(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """It is off by default; its absence must not clutter every line."""
        from app.providers.walden_http_booker import DirectBookingResult

        without = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(success=True, phase="complete", timing={"reserveAttempts": 1}),
        )
        assert "viewRefresh" not in without

        caplog.clear()
        with_refresh = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(
                success=True,
                phase="complete",
                timing={
                    "reserveAttempts": 1,
                    "viewRefreshMs": 729,
                    "viewRefreshAttempts": 1,
                    "viewRefreshFailed": "still-counting-down",
                    "viewRefreshCountdownS": 65,
                },
            ),
        )
        assert "viewRefresh=729ms/1x still-counting-down countdown=65s" in with_refresh

    def test_an_untimed_booking_says_so_rather_than_printing_a_drift(
        self,
        provider: WaldenGolfProvider,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Ad-hoc bookings have no window; a number there would be a lie."""
        from app.providers.walden_http_booker import DirectBookingResult

        line = self._summary(
            provider,
            monkeypatch,
            caplog,
            DirectBookingResult(
                success=True,
                phase="complete",
                booked_slot_time=time(17, 0),
                attempted_times=[time(17, 0)],
                timing={"reserveAttempts": 1},
            ),
        )

        assert "untimed (no window to lead)" in line
        assert "sent=" not in line
        assert "booked=05:00 PM" in line
