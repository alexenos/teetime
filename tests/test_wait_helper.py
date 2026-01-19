"""
Tests for the wait strategy helper module.

These tests verify the WaitMode enum, WaitStrategy class, and the
configurable wait behavior for Selenium operations.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import TimeoutException

from app.config import WaitMode
from app.providers.wait_helper import (
    HYBRID_BUFFER_SECONDS,
    WaitStrategy,
    get_wait_strategy,
)


class TestWaitModeEnum:
    """Tests for the WaitMode enum."""

    def test_wait_mode_has_fixed_value(self) -> None:
        """Test that FIXED mode exists with correct value."""
        assert WaitMode.FIXED.value == "fixed"

    def test_wait_mode_has_event_driven_value(self) -> None:
        """Test that EVENT_DRIVEN mode exists with correct value."""
        assert WaitMode.EVENT_DRIVEN.value == "event_driven"

    def test_wait_mode_has_hybrid_value(self) -> None:
        """Test that HYBRID mode exists with correct value."""
        assert WaitMode.HYBRID.value == "hybrid"

    def test_wait_mode_is_string_enum(self) -> None:
        """Test that WaitMode values are strings."""
        assert isinstance(WaitMode.FIXED.value, str)
        assert isinstance(WaitMode.EVENT_DRIVEN.value, str)
        assert isinstance(WaitMode.HYBRID.value, str)

    def test_wait_mode_from_string(self) -> None:
        """Test that WaitMode can be created from string values."""
        assert WaitMode("fixed") == WaitMode.FIXED
        assert WaitMode("event_driven") == WaitMode.EVENT_DRIVEN
        assert WaitMode("hybrid") == WaitMode.HYBRID


class TestWaitStrategyInit:
    """Tests for WaitStrategy initialization."""

    def test_init_with_explicit_fixed_mode(self) -> None:
        """Test initialization with explicit FIXED mode."""
        strategy = WaitStrategy(mode=WaitMode.FIXED)
        assert strategy.mode == WaitMode.FIXED

    def test_init_with_explicit_event_driven_mode(self) -> None:
        """Test initialization with explicit EVENT_DRIVEN mode."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        assert strategy.mode == WaitMode.EVENT_DRIVEN

    def test_init_with_explicit_hybrid_mode(self) -> None:
        """Test initialization with explicit HYBRID mode."""
        strategy = WaitStrategy(mode=WaitMode.HYBRID)
        assert strategy.mode == WaitMode.HYBRID

    def test_init_uses_settings_when_no_mode_provided(self) -> None:
        """Test that init uses settings.wait_mode when no mode is provided."""
        with patch("app.providers.wait_helper.settings") as mock_settings:
            mock_settings.wait_mode = WaitMode.HYBRID
            strategy = WaitStrategy()
            assert strategy.mode == WaitMode.HYBRID


class TestWaitStrategyWaitForElement:
    """Tests for WaitStrategy.wait_for_element method."""

    @pytest.fixture
    def mock_driver(self) -> MagicMock:
        """Create a mock WebDriver."""
        return MagicMock()

    def test_fixed_mode_sleeps_for_duration(self, mock_driver: MagicMock) -> None:
        """Test that FIXED mode sleeps for the specified duration."""
        strategy = WaitStrategy(mode=WaitMode.FIXED)
        locator = ("css selector", ".test-element")

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            result = strategy.wait_for_element(mock_driver, locator, fixed_duration=2.0)
            mock_sleep.assert_called_once_with(2.0)
            assert result is None

    def test_event_driven_mode_uses_webdriverwait(self, mock_driver: MagicMock) -> None:
        """Test that EVENT_DRIVEN mode uses WebDriverWait."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        locator = ("css selector", ".test-element")
        mock_element = MagicMock()

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait.until.return_value = mock_element
            mock_wait_class.return_value = mock_wait

            result = strategy.wait_for_element(mock_driver, locator, fixed_duration=2.0)

            mock_wait_class.assert_called_once_with(mock_driver, 10.0)
            assert result == mock_element

    def test_hybrid_mode_adds_buffer_sleep(self, mock_driver: MagicMock) -> None:
        """Test that HYBRID mode adds buffer sleep after WebDriverWait."""
        strategy = WaitStrategy(mode=WaitMode.HYBRID)
        locator = ("css selector", ".test-element")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait.until.return_value = MagicMock()
            mock_wait_class.return_value = mock_wait

            with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
                strategy.wait_for_element(mock_driver, locator, fixed_duration=2.0)
                mock_sleep.assert_called_once_with(HYBRID_BUFFER_SECONDS)

    def test_event_driven_handles_timeout(self, mock_driver: MagicMock) -> None:
        """Test that EVENT_DRIVEN mode handles TimeoutException gracefully."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        locator = ("css selector", ".test-element")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait.until.side_effect = TimeoutException()
            mock_wait_class.return_value = mock_wait

            result = strategy.wait_for_element(mock_driver, locator, fixed_duration=2.0)
            assert result is None

    def test_wait_for_element_with_visible_condition(self, mock_driver: MagicMock) -> None:
        """Test wait_for_element with 'visible' condition."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        locator = ("css selector", ".test-element")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            with patch("app.providers.wait_helper.expected_conditions") as mock_ec:
                mock_wait = MagicMock()
                mock_wait_class.return_value = mock_wait

                strategy.wait_for_element(
                    mock_driver, locator, fixed_duration=2.0, condition="visible"
                )
                mock_ec.visibility_of_element_located.assert_called_once_with(locator)

    def test_wait_for_element_with_clickable_condition(self, mock_driver: MagicMock) -> None:
        """Test wait_for_element with 'clickable' condition."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        locator = ("css selector", ".test-element")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            with patch("app.providers.wait_helper.expected_conditions") as mock_ec:
                mock_wait = MagicMock()
                mock_wait_class.return_value = mock_wait

                strategy.wait_for_element(
                    mock_driver, locator, fixed_duration=2.0, condition="clickable"
                )
                mock_ec.element_to_be_clickable.assert_called_once_with(locator)

    def test_wait_for_element_with_custom_timeout(self, mock_driver: MagicMock) -> None:
        """Test wait_for_element with custom timeout."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        locator = ("css selector", ".test-element")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait_class.return_value = mock_wait

            strategy.wait_for_element(mock_driver, locator, fixed_duration=2.0, timeout=5.0)
            mock_wait_class.assert_called_once_with(mock_driver, 5.0)


class TestWaitStrategyWaitAfterAction:
    """Tests for WaitStrategy.wait_after_action method."""

    @pytest.fixture
    def mock_driver(self) -> MagicMock:
        """Create a mock WebDriver."""
        return MagicMock()

    def test_fixed_mode_sleeps_for_duration(self, mock_driver: MagicMock) -> None:
        """Test that FIXED mode sleeps for the specified duration."""
        strategy = WaitStrategy(mode=WaitMode.FIXED)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.wait_after_action(mock_driver, fixed_duration=1.5)
            mock_sleep.assert_called_once_with(1.5)

    def test_event_driven_with_wait_condition(self, mock_driver: MagicMock) -> None:
        """Test EVENT_DRIVEN mode with a wait condition."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)
        wait_condition = ("css selector", ".loaded")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait_class.return_value = mock_wait

            strategy.wait_after_action(
                mock_driver, fixed_duration=1.0, wait_condition=wait_condition
            )
            mock_wait_class.assert_called_once()

    def test_event_driven_without_wait_condition(self, mock_driver: MagicMock) -> None:
        """Test EVENT_DRIVEN mode without a wait condition (minimal wait)."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.wait_after_action(mock_driver, fixed_duration=1.0)
            mock_sleep.assert_not_called()

    def test_hybrid_mode_adds_buffer_after_condition(self, mock_driver: MagicMock) -> None:
        """Test that HYBRID mode adds buffer sleep after wait condition."""
        strategy = WaitStrategy(mode=WaitMode.HYBRID)
        wait_condition = ("css selector", ".loaded")

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait_class.return_value = mock_wait

            with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
                strategy.wait_after_action(
                    mock_driver, fixed_duration=1.0, wait_condition=wait_condition
                )
                mock_sleep.assert_called_once_with(HYBRID_BUFFER_SECONDS)

    def test_hybrid_mode_adds_buffer_without_condition(self, mock_driver: MagicMock) -> None:
        """Test that HYBRID mode adds buffer sleep even without wait condition."""
        strategy = WaitStrategy(mode=WaitMode.HYBRID)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.wait_after_action(mock_driver, fixed_duration=1.0)
            mock_sleep.assert_called_once_with(HYBRID_BUFFER_SECONDS)


class TestWaitStrategyWaitForStaleness:
    """Tests for WaitStrategy.wait_for_staleness method."""

    @pytest.fixture
    def mock_driver(self) -> MagicMock:
        """Create a mock WebDriver."""
        return MagicMock()

    @pytest.fixture
    def mock_element(self) -> MagicMock:
        """Create a mock element."""
        return MagicMock()

    def test_fixed_mode_sleeps_and_returns_false(
        self, mock_driver: MagicMock, mock_element: MagicMock
    ) -> None:
        """Test that FIXED mode sleeps and returns False."""
        strategy = WaitStrategy(mode=WaitMode.FIXED)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            result = strategy.wait_for_staleness(mock_driver, mock_element, fixed_duration=1.0)
            mock_sleep.assert_called_once_with(1.0)
            assert result is False

    def test_event_driven_returns_true_when_stale(
        self, mock_driver: MagicMock, mock_element: MagicMock
    ) -> None:
        """Test EVENT_DRIVEN mode returns True when element becomes stale."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait.until.return_value = True
            mock_wait_class.return_value = mock_wait

            result = strategy.wait_for_staleness(mock_driver, mock_element, fixed_duration=1.0)
            assert result is True

    def test_event_driven_returns_false_on_timeout(
        self, mock_driver: MagicMock, mock_element: MagicMock
    ) -> None:
        """Test EVENT_DRIVEN mode returns False on timeout."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait.until.side_effect = TimeoutException()
            mock_wait_class.return_value = mock_wait

            result = strategy.wait_for_staleness(mock_driver, mock_element, fixed_duration=1.0)
            assert result is False

    def test_hybrid_mode_adds_buffer_after_staleness(
        self, mock_driver: MagicMock, mock_element: MagicMock
    ) -> None:
        """Test that HYBRID mode adds buffer sleep after staleness check."""
        strategy = WaitStrategy(mode=WaitMode.HYBRID)

        with patch("app.providers.wait_helper.WebDriverWait") as mock_wait_class:
            mock_wait = MagicMock()
            mock_wait_class.return_value = mock_wait

            with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
                strategy.wait_for_staleness(mock_driver, mock_element, fixed_duration=1.0)
                mock_sleep.assert_called_once_with(HYBRID_BUFFER_SECONDS)


class TestWaitStrategySimpleWait:
    """Tests for WaitStrategy.simple_wait method."""

    def test_fixed_mode_sleeps_for_fixed_duration(self) -> None:
        """Test that FIXED mode sleeps for the fixed duration."""
        strategy = WaitStrategy(mode=WaitMode.FIXED)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)
            mock_sleep.assert_called_once_with(0.5)

    def test_event_driven_sleeps_for_event_driven_duration(self) -> None:
        """Test that EVENT_DRIVEN mode sleeps for the event_driven duration."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)
            mock_sleep.assert_called_once_with(0.1)

    def test_event_driven_skips_sleep_when_duration_zero(self) -> None:
        """Test that EVENT_DRIVEN mode skips sleep when duration is 0."""
        strategy = WaitStrategy(mode=WaitMode.EVENT_DRIVEN)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.0)
            mock_sleep.assert_not_called()

    def test_hybrid_mode_sleeps_for_buffer(self) -> None:
        """Test that HYBRID mode sleeps for the buffer duration."""
        strategy = WaitStrategy(mode=WaitMode.HYBRID)

        with patch("app.providers.wait_helper.time_module.sleep") as mock_sleep:
            strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)
            mock_sleep.assert_called_once_with(HYBRID_BUFFER_SECONDS)


class TestGetWaitStrategy:
    """Tests for the get_wait_strategy factory function."""

    def test_returns_wait_strategy_instance(self) -> None:
        """Test that get_wait_strategy returns a WaitStrategy instance."""
        strategy = get_wait_strategy(mode=WaitMode.FIXED)
        assert isinstance(strategy, WaitStrategy)

    def test_respects_mode_parameter(self) -> None:
        """Test that get_wait_strategy respects the mode parameter."""
        strategy = get_wait_strategy(mode=WaitMode.EVENT_DRIVEN)
        assert strategy.mode == WaitMode.EVENT_DRIVEN

    def test_uses_settings_when_no_mode(self) -> None:
        """Test that get_wait_strategy uses settings when no mode provided."""
        with patch("app.providers.wait_helper.settings") as mock_settings:
            mock_settings.wait_mode = WaitMode.HYBRID
            strategy = get_wait_strategy()
            assert strategy.mode == WaitMode.HYBRID


class TestHybridBufferConstant:
    """Tests for the HYBRID_BUFFER_SECONDS constant."""

    def test_hybrid_buffer_is_positive(self) -> None:
        """Test that HYBRID_BUFFER_SECONDS is a positive number."""
        assert HYBRID_BUFFER_SECONDS > 0

    def test_hybrid_buffer_is_reasonable(self) -> None:
        """Test that HYBRID_BUFFER_SECONDS is a reasonable value (< 1 second)."""
        assert HYBRID_BUFFER_SECONDS < 1.0

    def test_hybrid_buffer_value(self) -> None:
        """Test the specific value of HYBRID_BUFFER_SECONDS."""
        assert HYBRID_BUFFER_SECONDS == 0.3


class TestWaldenProviderWaitStrategyIntegration:
    """Tests for WaldenGolfProvider integration with WaitStrategy."""

    def test_provider_initializes_wait_strategy(self) -> None:
        """Test that WaldenGolfProvider initializes a WaitStrategy."""
        from app.providers.walden_provider import WaldenGolfProvider

        provider = WaldenGolfProvider()
        assert hasattr(provider, "wait_strategy")
        assert isinstance(provider.wait_strategy, WaitStrategy)

    def test_provider_uses_configured_wait_mode(self) -> None:
        """Test that provider uses the configured wait mode."""
        with patch("app.providers.wait_helper.settings") as mock_settings:
            mock_settings.wait_mode = WaitMode.HYBRID
            from app.providers.walden_provider import WaldenGolfProvider

            provider = WaldenGolfProvider()
            assert provider.wait_strategy.mode == WaitMode.HYBRID


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
