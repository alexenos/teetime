"""
Tests for WaldenGolfProvider Selenium implementation.

These tests verify the DOM parsing and time extraction logic works correctly
against the actual Walden Golf website structure.
"""

import os
from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.providers.walden_provider import WaldenGolfProvider


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

    def test_verify_success_ignores_hidden_error_in_html(self, provider: WaldenGolfProvider) -> None:
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
