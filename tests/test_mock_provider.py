"""
Tests for MockWaldenProvider in app/providers/walden_provider.py.

These tests verify the mock provider used for testing and development
when real credentials are not available.
"""

from datetime import date, time, timedelta

import pytest

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


class TestMockWaldenProviderBookTeeTime:
    """Tests for the book_tee_time method."""

    @pytest.mark.asyncio
    async def test_book_tee_time_success(self, mock_provider: MockWaldenProvider) -> None:
        """Test successful booking."""
        target_date = date.today() + timedelta(days=7)
        target_time = time(8, 0)

        result = await mock_provider.book_tee_time(
            target_date=target_date,
            target_time=target_time,
            num_players=4,
            fallback_window_minutes=30,
        )

        assert result.success is True
        assert result.booked_time == target_time
        assert result.confirmation_number is not None
        assert result.confirmation_number.startswith("MOCK-")
        assert result.error_message is None

    @pytest.mark.asyncio
    async def test_book_tee_time_different_times(self, mock_provider: MockWaldenProvider) -> None:
        """Test booking at different times."""
        target_date = date.today() + timedelta(days=7)

        for hour in [7, 8, 9, 10, 14, 17]:
            target_time = time(hour, 0)
            result = await mock_provider.book_tee_time(
                target_date=target_date,
                target_time=target_time,
                num_players=4,
            )
            assert result.success is True
            assert result.booked_time == target_time

    @pytest.mark.asyncio
    async def test_book_tee_time_different_player_counts(
        self, mock_provider: MockWaldenProvider
    ) -> None:
        """Test booking with different player counts."""
        target_date = date.today() + timedelta(days=7)
        target_time = time(8, 0)

        for num_players in [1, 2, 3, 4]:
            result = await mock_provider.book_tee_time(
                target_date=target_date,
                target_time=target_time,
                num_players=num_players,
            )
            assert result.success is True


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
        """Test that confirmation numbers have the expected format."""
        target_date = date.today() + timedelta(days=7)
        target_time = time(8, 0)

        result = await mock_provider.book_tee_time(
            target_date=target_date,
            target_time=target_time,
            num_players=4,
        )

        assert result.confirmation_number is not None
        assert result.confirmation_number.startswith("MOCK-")
        assert len(result.confirmation_number) > 5

    @pytest.mark.asyncio
    async def test_confirmation_numbers_are_distinct(
        self, mock_provider: MockWaldenProvider
    ) -> None:
        """Test that each booking gets a distinct confirmation number."""
        import asyncio

        target_date = date.today() + timedelta(days=7)
        target_time = time(8, 0)

        confirmation_numbers: list[str] = []
        for _ in range(3):
            result = await mock_provider.book_tee_time(
                target_date=target_date,
                target_time=target_time,
                num_players=4,
            )
            assert result.confirmation_number is not None
            assert result.confirmation_number.startswith("MOCK-")
            confirmation_numbers.append(result.confirmation_number)
            # Sleep 1 second to ensure timestamp-based IDs are different
            # (MockWaldenProvider uses second-precision timestamps)
            await asyncio.sleep(1.0)

        # Verify all confirmation numbers are distinct
        assert len(set(confirmation_numbers)) == 3, (
            f"Expected 3 distinct confirmation numbers, got: {confirmation_numbers}"
        )
