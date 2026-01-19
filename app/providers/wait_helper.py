"""
Wait strategy helper for Selenium operations.

This module provides configurable wait strategies for Selenium WebDriver operations.
The wait mode can be configured via the WAIT_MODE environment variable.

Three modes are supported:
- FIXED: Use fixed sleep durations (most reliable, slowest)
- EVENT_DRIVEN: Use WebDriverWait only (fastest, less reliable)
- HYBRID: Use WebDriverWait + small buffer sleep (balanced)
"""

import logging
import time as time_module
from typing import Any

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

from app.config import WaitMode, settings

logger = logging.getLogger(__name__)

HYBRID_BUFFER_SECONDS = 0.3


class WaitStrategy:
    """
    Provides wait methods that behave differently based on the configured wait mode.

    Usage:
        wait_strategy = WaitStrategy()
        wait_strategy.wait_for_element(driver, (By.CSS_SELECTOR, ".my-element"), fixed_duration=2.0)
        wait_strategy.wait_after_action(driver, fixed_duration=1.0)
    """

    def __init__(self, mode: WaitMode | None = None) -> None:
        """
        Initialize the wait strategy.

        Args:
            mode: The wait mode to use. If None, uses the configured setting.
        """
        self.mode = mode or settings.wait_mode
        logger.info(f"WaitStrategy initialized with mode: {self.mode.value}")

    def wait_for_element(
        self,
        driver: WebDriver,
        locator: tuple[str, str],
        fixed_duration: float,
        timeout: float = 10.0,
        condition: str = "presence",
    ) -> Any | None:
        """
        Wait for an element based on the configured wait mode.

        Args:
            driver: The WebDriver instance
            locator: Tuple of (By.*, selector) for the element
            fixed_duration: Duration to sleep in FIXED mode
            timeout: Maximum wait time for WebDriverWait in EVENT_DRIVEN/HYBRID modes
            condition: The expected condition type:
                - "presence": Wait for element to be present in DOM
                - "visible": Wait for element to be visible
                - "clickable": Wait for element to be clickable

        Returns:
            The element if found (in EVENT_DRIVEN/HYBRID modes), None in FIXED mode
        """
        if self.mode == WaitMode.FIXED:
            logger.debug(f"FIXED mode: sleeping {fixed_duration}s for element {locator}")
            time_module.sleep(fixed_duration)
            return None

        wait = WebDriverWait(driver, timeout)
        element = None

        try:
            if condition == "presence":
                element = wait.until(expected_conditions.presence_of_element_located(locator))
            elif condition == "visible":
                element = wait.until(expected_conditions.visibility_of_element_located(locator))
            elif condition == "clickable":
                element = wait.until(expected_conditions.element_to_be_clickable(locator))
            else:
                element = wait.until(expected_conditions.presence_of_element_located(locator))

            logger.debug(f"{self.mode.value} mode: element {locator} found after WebDriverWait")

        except TimeoutException:
            logger.warning(f"{self.mode.value} mode: timeout waiting for element {locator}")

        if self.mode == WaitMode.HYBRID:
            logger.debug(f"HYBRID mode: adding {HYBRID_BUFFER_SECONDS}s buffer")
            time_module.sleep(HYBRID_BUFFER_SECONDS)

        return element

    def wait_after_action(
        self,
        driver: WebDriver,
        fixed_duration: float,
        wait_condition: tuple[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        """
        Wait after performing an action (click, form submission, etc.).

        This is used when we need to wait for the page/DOM to update after an action.

        Args:
            driver: The WebDriver instance
            fixed_duration: Duration to sleep in FIXED mode
            wait_condition: Optional locator to wait for in EVENT_DRIVEN/HYBRID modes.
                           If None, uses a minimal wait in EVENT_DRIVEN mode.
            timeout: Maximum wait time for WebDriverWait
        """
        if self.mode == WaitMode.FIXED:
            logger.debug(f"FIXED mode: sleeping {fixed_duration}s after action")
            time_module.sleep(fixed_duration)
            return

        if wait_condition:
            wait = WebDriverWait(driver, timeout)
            try:
                wait.until(expected_conditions.presence_of_element_located(wait_condition))
                logger.debug(f"{self.mode.value} mode: condition {wait_condition} met after action")
            except TimeoutException:
                logger.warning(
                    f"{self.mode.value} mode: timeout waiting for {wait_condition} after action"
                )
        else:
            logger.debug(f"{self.mode.value} mode: no wait condition, minimal wait")

        if self.mode == WaitMode.HYBRID:
            logger.debug(f"HYBRID mode: adding {HYBRID_BUFFER_SECONDS}s buffer after action")
            time_module.sleep(HYBRID_BUFFER_SECONDS)

    def wait_for_staleness(
        self,
        driver: WebDriver,
        element: Any,
        fixed_duration: float,
        timeout: float = 10.0,
    ) -> bool:
        """
        Wait for an element to become stale (detached from DOM).

        This is useful for waiting for DOM updates after actions that cause re-renders.

        Args:
            driver: The WebDriver instance
            element: The element to wait for staleness
            fixed_duration: Duration to sleep in FIXED mode
            timeout: Maximum wait time for WebDriverWait

        Returns:
            True if element became stale, False otherwise (or in FIXED mode)
        """
        if self.mode == WaitMode.FIXED:
            logger.debug(f"FIXED mode: sleeping {fixed_duration}s for staleness")
            time_module.sleep(fixed_duration)
            return False

        wait = WebDriverWait(driver, timeout)
        became_stale = False

        try:
            wait.until(expected_conditions.staleness_of(element))
            became_stale = True
            logger.debug(f"{self.mode.value} mode: element became stale")
        except TimeoutException:
            logger.warning(f"{self.mode.value} mode: timeout waiting for staleness")

        if self.mode == WaitMode.HYBRID:
            logger.debug(f"HYBRID mode: adding {HYBRID_BUFFER_SECONDS}s buffer after staleness")
            time_module.sleep(HYBRID_BUFFER_SECONDS)

        return became_stale

    def simple_wait(self, fixed_duration: float, event_driven_duration: float = 0.0) -> None:
        """
        Simple wait without any element conditions.

        This is for cases where we just need a pause (e.g., scroll animations).

        Args:
            fixed_duration: Duration to sleep in FIXED mode
            event_driven_duration: Duration to sleep in EVENT_DRIVEN mode (default 0)
        """
        if self.mode == WaitMode.FIXED:
            logger.debug(f"FIXED mode: simple sleep {fixed_duration}s")
            time_module.sleep(fixed_duration)
        elif self.mode == WaitMode.EVENT_DRIVEN:
            if event_driven_duration > 0:
                logger.debug(f"EVENT_DRIVEN mode: simple sleep {event_driven_duration}s")
                time_module.sleep(event_driven_duration)
            else:
                logger.debug("EVENT_DRIVEN mode: skipping simple wait")
        else:
            logger.debug(f"HYBRID mode: simple sleep {HYBRID_BUFFER_SECONDS}s")
            time_module.sleep(HYBRID_BUFFER_SECONDS)


def get_wait_strategy(mode: WaitMode | None = None) -> WaitStrategy:
    """
    Factory function to get a WaitStrategy instance.

    Args:
        mode: Optional wait mode override. If None, uses configured setting.

    Returns:
        A WaitStrategy instance configured with the specified mode.
    """
    return WaitStrategy(mode)
