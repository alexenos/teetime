"""
Tests for MockWaldenProvider in app/providers/walden_provider.py.

These tests verify the mock provider used for testing and development
when real credentials are not available.
"""

from datetime import date, time, timedelta

import pytest

from app.providers.base import BatchBookingRequest
from app.providers.walden_provider import MockWaldenProvider


@pytest.fixture
def mock_provider() -> MockWaldenProvider:
    """Create a MockWaldenProvider instance."""
    return MockWaldenProvider()


class TestMockWaldenProviderLogin:
    """Tests for the login method."""

    @pytest.mark.asyncio
    async def test_login_always_succeeds(self, mock_provider: MockWaldenProvider) -> None:
        """Test that login always returns True."""
        result = await mock_provider.login()
        assert result is True


class TestMockWaldenProviderBookMultipleTeeTimes:
    """Tests for book_multiple_tee_times, the only booking method (#127)."""

    @staticmethod
    def _requests(*times: time) -> list[BatchBookingRequest]:
        return [
            BatchBookingRequest(booking_id=f"booking-{i}", target_time=t, num_players=4)
            for i, t in enumerate(times)
        ]

    @pytest.mark.asyncio
    async def test_every_request_succeeds_at_its_time(
        self, mock_provider: MockWaldenProvider
    ) -> None:
        target_date = date.today() + timedelta(days=7)
        requests = self._requests(time(7, 0), time(8, 30), time(14, 0))

        batch = await mock_provider.book_multiple_tee_times(target_date, requests)

        assert batch.total_succeeded == 3
        assert [item.booking_id for item in batch.results] == [r.booking_id for r in requests]
        assert all(item.result.success for item in batch.results)
        assert [item.result.booked_time for item in batch.results] == [
            time(7, 0),
            time(8, 30),
            time(14, 0),
        ]


class TestMockWaldenProviderGetAvailableTimes:
    """Tests for the get_available_times method."""

    @pytest.mark.asyncio
    async def test_get_available_times_returns_list(
        self, mock_provider: MockWaldenProvider
    ) -> None:
        """Test that get_available_times returns a list of times."""
        target_date = date.today() + timedelta(days=7)

        result = await mock_provider.get_available_times(target_date)

        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_get_available_times_valid_times(self, mock_provider: MockWaldenProvider) -> None:
        """Test that returned times are valid time objects."""
        target_date = date.today() + timedelta(days=7)

        result = await mock_provider.get_available_times(target_date)

        for t in result:
            assert isinstance(t, time)
            assert 0 <= t.hour <= 23
            assert 0 <= t.minute <= 59

    @pytest.mark.asyncio
    async def test_get_available_times_reasonable_range(
        self, mock_provider: MockWaldenProvider
    ) -> None:
        """Test that returned times are in a reasonable golf range."""
        target_date = date.today() + timedelta(days=7)

        result = await mock_provider.get_available_times(target_date)

        for t in result:
            assert t.hour >= 6
            assert t.hour <= 18


class TestMockWaldenProviderCancelBooking:
    """Tests for the cancel_booking method."""

    @pytest.mark.asyncio
    async def test_cancel_booking_always_succeeds(self, mock_provider: MockWaldenProvider) -> None:
        """Test that cancel_booking always returns True."""
        result = await mock_provider.cancel_booking("MOCK-123456")
        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_booking_any_confirmation(self, mock_provider: MockWaldenProvider) -> None:
        """Test cancelling with any confirmation number."""
        for conf_num in ["MOCK-123", "CONF-456", "ABC123"]:
            result = await mock_provider.cancel_booking(conf_num)
            assert result is True


class TestMockWaldenProviderClose:
    """Tests for the close method."""

    @pytest.mark.asyncio
    async def test_close_no_error(self, mock_provider: MockWaldenProvider) -> None:
        """Test that close completes without error."""
        await mock_provider.close()


class TestMockWaldenProviderConfirmationNumbers:
    """Tests for confirmation number generation."""

    @pytest.mark.asyncio
    async def test_confirmation_number_format(self, mock_provider: MockWaldenProvider) -> None:
        """Confirmation numbers start with MOCK- and carry the booking id prefix."""
        target_date = date.today() + timedelta(days=7)
        requests = [
            BatchBookingRequest(booking_id="abcdef123456", target_time=time(8, 0), num_players=4)
        ]

        batch = await mock_provider.book_multiple_tee_times(target_date, requests)

        confirmation = batch.results[0].result.confirmation_number
        assert confirmation is not None
        assert confirmation.startswith("MOCK-")
        assert confirmation.endswith("-abcdef12")

    @pytest.mark.asyncio
    async def test_confirmation_numbers_are_distinct(
        self, mock_provider: MockWaldenProvider
    ) -> None:
        """Each booking in a batch gets a distinct confirmation number."""
        target_date = date.today() + timedelta(days=7)
        requests = [
            BatchBookingRequest(booking_id=f"booking{i}-id", target_time=time(8, 0), num_players=4)
            for i in range(3)
        ]

        batch = await mock_provider.book_multiple_tee_times(target_date, requests)

        numbers = [item.result.confirmation_number for item in batch.results]
        assert len(set(numbers)) == 3, numbers
