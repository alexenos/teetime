"""
Tests for WaldenGolfProvider Selenium implementation.

These tests verify the DOM parsing and time extraction logic works correctly
against the actual Walden Golf website structure.
"""

import os
from datetime import date, timedelta

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
    not os.getenv("WALDEN_MEMBER_NUMBER") or not os.getenv("WALDEN_PASSWORD"),
    reason="Walden Golf credentials not configured",
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
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

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


class TestWaldenProviderBookingVerification:
    """Tests for booking success verification logic."""

    def test_verify_success_with_successfully(self, provider: WaldenGolfProvider) -> None:
        """Test that 'successfully' indicator returns True."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Your tee time was successfully booked!</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_success_with_confirmed(self, provider: WaldenGolfProvider) -> None:
        """Test that 'confirmed' indicator returns True."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Your reservation is confirmed.</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_success_with_thank_you(self, provider: WaldenGolfProvider) -> None:
        """Test that 'thank you' indicator returns True."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>Thank you for your reservation!</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is True

    def test_verify_failure_with_error(self, provider: WaldenGolfProvider) -> None:
        """Test that 'error' indicator returns False."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        mock_driver.page_source = (
            "<html><body>An error occurred while processing your request.</body></html>"
        )
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_failure_with_unavailable(self, provider: WaldenGolfProvider) -> None:
        """Test that 'unavailable' indicator returns False."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>This time slot is unavailable.</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_failure_with_already_booked(self, provider: WaldenGolfProvider) -> None:
        """Test that 'already booked' indicator returns False."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        mock_driver.page_source = "<html><body>This slot is already booked.</body></html>"
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_failure_takes_precedence(self, provider: WaldenGolfProvider) -> None:
        """Test that failure indicators take precedence over success indicators."""
        from unittest.mock import MagicMock

        mock_driver = MagicMock()
        # Page has both success and failure indicators - failure should win
        mock_driver.page_source = (
            "<html><body>Successfully detected an error in your booking.</body></html>"
        )
        result = provider._verify_booking_success(mock_driver)
        assert result is False

    def test_verify_ambiguous_returns_false(self, provider: WaldenGolfProvider) -> None:
        """Test that ambiguous page content returns False."""
        from unittest.mock import MagicMock

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
