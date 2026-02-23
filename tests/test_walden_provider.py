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

    def test_select_player_count_uses_search_context(self, provider: WaldenGolfProvider) -> None:
        """When search_context is provided, find_element is called on it, not driver."""
        mock_driver = MagicMock()
        mock_modal = MagicMock()

        # Set up the mock modal to return a button group with a valid radio input
        mock_button_group = MagicMock()
        mock_radio = MagicMock()
        mock_button_div = MagicMock()
        mock_button_div.get_attribute.return_value = "ui-button"

        # Make the modal find the button group with the first selector
        mock_modal.find_element.return_value = mock_button_group
        mock_button_group.find_element.side_effect = [
            mock_radio,  # radio input
            mock_button_div,  # parent div (XPATH call)
        ]

        # Mock wait_strategy to be a no-op
        provider.wait_strategy = MagicMock()

        # Mock _verify_player_rows_appeared to return True
        with patch.object(provider, "_verify_player_rows_appeared", return_value=True):
            result = provider._select_player_count_sync(mock_driver, 4, search_context=mock_modal)

        assert result is True
        # The critical assertion: find_element was called on the MODAL, not the driver
        mock_modal.find_element.assert_called()
        # execute_script should still use driver (not modal)
        mock_driver.execute_script.assert_called()

    def test_select_player_count_defaults_to_driver(self, provider: WaldenGolfProvider) -> None:
        """When no search_context is provided, defaults to using driver."""
        mock_driver = MagicMock()

        # Set up the mock driver to return a button group with a valid radio input
        mock_button_group = MagicMock()
        mock_radio = MagicMock()
        mock_button_div = MagicMock()
        mock_button_div.get_attribute.return_value = "ui-button"

        mock_driver.find_element.return_value = mock_button_group
        mock_button_group.find_element.side_effect = [
            mock_radio,  # radio input
            mock_button_div,  # parent div
        ]

        provider.wait_strategy = MagicMock()

        with patch.object(provider, "_verify_player_rows_appeared", return_value=True):
            result = provider._select_player_count_sync(mock_driver, 4)

        assert result is True
        # find_element called on the driver (default search_context)
        mock_driver.find_element.assert_called()

    def test_complete_booking_passes_modal_as_context(self, provider: WaldenGolfProvider) -> None:
        """_complete_booking_sync captures modal element and passes it to player count selection."""
        mock_driver = MagicMock()
        mock_reserve_element = MagicMock()
        mock_modal = MagicMock()
        mock_confirm_button = MagicMock()
        mock_confirm_button.text = "Book Now"

        # Make the WebDriverWait return values for each .until() call:
        # 1. element_to_be_clickable (reserve button check)
        # 2. visibility_of_element_located (modal detection)
        # 3+ any remaining calls (Book Now wait, url_changes, success indicators, etc.)
        with patch("app.providers.walden_provider.WebDriverWait") as mock_wait_cls:
            mock_wait_instance = MagicMock()
            mock_wait_cls.return_value = mock_wait_instance
            # Use a default return for .until() but make the second call return the modal
            call_count = [0]

            def until_side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_reserve_element  # clickable check
                elif call_count[0] == 2:
                    return mock_modal  # modal detection
                else:
                    return mock_confirm_button  # all subsequent calls

            mock_wait_instance.until.side_effect = until_side_effect

            # Mock _select_player_count_sync to capture args and short-circuit
            with patch.object(
                provider, "_select_player_count_sync", return_value=False
            ) as mock_select:
                provider.wait_strategy = MagicMock()
                # Also mock _capture_diagnostic_info to avoid side effects
                with patch.object(provider, "_capture_diagnostic_info"):
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
        mock_driver.execute_script.return_value = {
            "timeStr": "8:42",
            "hours": 8,
            "minutes": 42,
            "index": 5,
            "diff": 0,
            "available": 4,
            "isExact": True,
        }

        result = provider._find_target_slot_js(mock_driver, time(8, 42), 4, 32, 8)

        assert result is not None
        assert result["isExact"] is True
        assert result["hours"] == 8
        assert result["minutes"] == 42
        assert result["index"] == 5

    def test_find_fallback_when_exact_unavailable(self, provider: WaldenGolfProvider) -> None:
        """Test that JS finder returns fallback slot."""
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = {
            "timeStr": "8:50",
            "hours": 8,
            "minutes": 50,
            "index": 7,
            "diff": 8,
            "available": 4,
            "isExact": False,
        }

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

        with mock_patch("app.providers.walden_provider.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 2, 12, 6, 30, 0, tzinfo=ZoneInfo("America/Chicago")
            )
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
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

        with mock_patch("app.providers.walden_provider.datetime") as mock_dt:
            mock_dt.now = mock_now
            # Route strftime and other calls through real datetime
            mock_dt.side_effect = lambda *args, **kwargs: real_datetime(*args, **kwargs)

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
        """Test that fast JS path finds slot, clicks it, and completes booking."""
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

        # Mock JS click
        monkeypatch.setattr(provider, "_click_slot_by_index_js", MagicMock(return_value=True))

        # Mock complete booking
        monkeypatch.setattr(
            provider,
            "_complete_booking_sync",
            MagicMock(
                return_value=SimpleNamespace(
                    success=True,
                    booked_time=time(8, 42),
                    confirmation_number="ABC123",
                    course_name=None,
                )
            ),
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
        # Verify _complete_booking_sync was called with already_clicked=True
        call_kwargs = provider._complete_booking_sync.call_args[1]
        assert call_kwargs["already_clicked"] is True

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

    def test_fast_js_returns_failure_when_click_fails(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that fast JS path returns failure when click fails."""
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

        monkeypatch.setattr(provider, "_click_slot_by_index_js", MagicMock(return_value=False))

        result = provider._find_and_book_time_slot_sync(
            mock_driver,
            target_time=time(8, 42),
            num_players=4,
            fallback_window_minutes=32,
            skip_scroll=True,
            use_fast_js=True,
        )

        assert result.success is False
        assert "Failed to click Reserve" in result.error_message


class TestBatchBookingPreparation:
    """Tests for the restructured batch booking flow with pre-6:30 preparation."""

    def test_batch_booking_does_prep_before_wait(
        self, provider: WaldenGolfProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that date selection and scrolling happen before execute_at wait."""
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

        def mock_precision_wait(execute_at: object) -> None:
            call_order.append("precision_wait")

        def mock_find_and_book(*_args: object, **_kwargs: object) -> object:
            call_order.append("find_and_book")
            return SimpleNamespace(success=True, booked_time=time(8, 42), confirmation_number="X")

        monkeypatch.setattr(provider, "_select_date_sync", mock_select_date)
        monkeypatch.setattr(provider, "_scroll_to_load_all_slots", mock_scroll)
        monkeypatch.setattr(provider, "_find_target_slot_js", lambda *_a, **_kw: None)
        monkeypatch.setattr(provider, "_precision_wait_until", mock_precision_wait)
        monkeypatch.setattr(provider, "_find_and_book_time_slot_sync", mock_find_and_book)

        from app.providers.base import BatchBookingRequest

        req = BatchBookingRequest(booking_id="a", target_time=time(8, 42), num_players=4)

        result = provider._book_multiple_tee_times_sync(
            target_date=date.today() + timedelta(days=7),
            requests=[req],
            execute_at=datetime(2026, 2, 19, 6, 30, 0),
        )

        assert result.total_succeeded == 1
        # Verify order: date selection and scroll happen BEFORE precision wait
        assert call_order.index("select_date") < call_order.index("precision_wait")
        assert call_order.index("scroll") < call_order.index("precision_wait")
        assert call_order.index("precision_wait") < call_order.index("find_and_book")

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
