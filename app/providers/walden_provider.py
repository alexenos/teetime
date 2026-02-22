import asyncio
import functools
import logging
import os
import re
import time as time_module
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import google.auth
import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from app.config import settings
from app.providers.base import (
    BatchBookingItemResult,
    BatchBookingRequest,
    BatchBookingResult,
    BookingResult,
    ReservationProvider,
)
from app.providers.wait_helper import WaitStrategy

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_EXCEPTIONS = (
    StaleElementReferenceException,
    ElementClickInterceptedException,
    TimeoutException,
)


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    exceptions: tuple[type[Exception], ...] = TRANSIENT_EXCEPTIONS,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for retrying operations that may fail due to transient Selenium issues.

    Uses exponential backoff between attempts. Only retries on specified exception types.

    Args:
        max_attempts: Maximum number of attempts (default 3)
        backoff_base: Base delay in seconds, doubled each attempt (default 0.5)
        exceptions: Tuple of exception types to retry on
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = backoff_base * (2**attempt)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time_module.sleep(delay)
                    else:
                        logger.error(f"All {max_attempts} attempts failed for {func.__name__}: {e}")
            raise last_exception  # type: ignore[misc]

        return wrapper

    return decorator


class WaldenGolfProvider(ReservationProvider):
    """
    Selenium-based provider for booking tee times at Walden Golf / Northgate Country Club.

    The booking system uses Liferay Portal with Northstar Technologies' club management
    software. This provider automates the browser-based booking flow:
    1. Login with member credentials
    2. Navigate to tee time booking page
    3. Select course (Northgate 18)
    4. Select date and find available time slots
    5. Click Reserve on the desired time slot
    6. Confirm the booking

    Time slots are in 8-minute intervals for Northgate (e.g., 07:30, 07:38, 07:46).

    Implementation Note:
        All public async methods use asyncio.to_thread() to run blocking Selenium
        operations in a background thread. Each operation manages its own WebDriver
        lifecycle (create -> use -> quit) to avoid thread-affinity issues.
    """

    BASE_URL = "https://www.waldengolf.com"
    LOGIN_URL = f"{BASE_URL}/web/pages/login"
    DASHBOARD_URL = f"{BASE_URL}/group/pages/home"
    TEE_TIME_URL = f"{BASE_URL}/group/pages/book-a-tee-time"

    NORTHGATE_COURSE_NAME = "Northgate"
    TEE_TIME_INTERVAL_MINUTES = 8
    MAX_PLAYERS = 4  # Maximum players per tee time slot

    # Course index constants for element ID parsing
    # The Walden Golf website uses teeTimeCourses:0 for Northgate and teeTimeCourses:1 for Walden
    NORTHGATE_COURSE_INDEX = "0"
    WALDEN_COURSE_INDEX = "1"

    def __init__(self) -> None:
        """
        Initialize the WaldenGolfProvider.

        Validates that required credentials are configured. Logs a warning if
        credentials are missing - operations will fail at login time.
        """
        self.wait_strategy = WaitStrategy()
        if not settings.walden_member_number or not settings.walden_password:
            logger.warning(
                "Walden Golf credentials not configured. "
                "Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD environment variables."
            )

    async def __aenter__(self) -> "WaldenGolfProvider":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    def _create_driver(self) -> webdriver.Chrome:
        """Create a headless Chrome WebDriver instance."""
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # Check for ChromeDriver path from environment variable first,
        # then fall back to ChromeDriverManager for automatic version management
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(chromedriver_path)
        else:
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
            },
        )

        return driver

    async def login(self) -> bool:
        """
        Log in to the Walden Golf member portal.

        This method creates a temporary driver, logs in, and closes it.
        It is primarily useful for testing credentials.

        Returns:
            True if login was successful, False otherwise.
        """
        return await asyncio.to_thread(self._login_sync)

    def _login_sync(self) -> bool:
        """Synchronous login implementation with full driver lifecycle."""
        driver = self._create_driver()
        try:
            return self._perform_login(driver)
        finally:
            driver.quit()

    def _perform_login(self, driver: webdriver.Chrome) -> bool:
        """
        Perform the login flow on an existing driver.

        Args:
            driver: The WebDriver instance to use

        Returns:
            True if login was successful, False otherwise.
        """
        try:
            logger.info("Navigating to login page...")
            driver.get(self.LOGIN_URL)

            wait = WebDriverWait(driver, 15)
            member_input = wait.until(
                expected_conditions.presence_of_element_located(
                    (By.NAME, "_com_liferay_login_web_portlet_LoginPortlet_login")
                )
            )

            password_input = driver.find_element(
                By.NAME, "_com_liferay_login_web_portlet_LoginPortlet_password"
            )

            logger.info("Entering credentials...")
            member_input.clear()
            member_input.send_keys(settings.walden_member_number)

            password_input.clear()
            password_input.send_keys(settings.walden_password)

            submit_button = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
            current_url = driver.current_url
            submit_button.click()

            try:
                wait.until(expected_conditions.url_changes(current_url))
            except TimeoutException:
                pass

            if "login" not in driver.current_url.lower() or "home" in driver.current_url.lower():
                logger.info(f"Login successful. Current URL: {driver.current_url}")
                return True

            logger.error(f"Login failed. Still on URL: {driver.current_url}")
            return False

        except TimeoutException as e:
            logger.error(f"Login timeout: {e}")
            return False
        except WebDriverException as e:
            logger.error(f"Login WebDriver error: {e}")
            return False

    async def book_tee_time(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int = 32,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        """
        Book a tee time at Northgate Country Club.

        This method runs the entire booking workflow in a background thread:
        1. Creates a new WebDriver instance
        2. Logs in to the member portal
        3. Navigates to the tee time booking page
        4. Selects the Northgate course and target date
        5. Finds the requested time slot (or nearest available within fallback window)
        6. Clicks Reserve, selects player count, and confirms the booking
        7. Closes the WebDriver

        The async interface is genuinely non-blocking - all Selenium operations
        run in a dedicated thread via asyncio.to_thread().

        Args:
            target_date: The date to book (should be 7 days in advance for new bookings)
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: If exact time unavailable, try times within this window

        Returns:
            BookingResult with success status, booked time, and confirmation details
        """
        return await asyncio.to_thread(
            self._book_tee_time_sync,
            target_date,
            target_time,
            num_players,
            fallback_window_minutes,
            tee_time_interval_minutes,
        )

    def _book_tee_time_sync(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        """
        Synchronous booking implementation with full driver lifecycle.

        Creates driver, performs booking, and ensures cleanup in finally block.
        """
        # Calculate time range for logging
        target_minutes = target_time.hour * 60 + target_time.minute
        earliest_minutes = max(0, target_minutes - fallback_window_minutes)
        latest_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)
        earliest_time = time(earliest_minutes // 60, earliest_minutes % 60)
        latest_time = time(latest_minutes // 60, latest_minutes % 60)

        logger.info(
            f"BOOKING_DEBUG: === STARTING BOOKING ATTEMPT === "
            f"date={target_date} ({target_date.strftime('%A')}), "
            f"requested_time={target_time.strftime('%H:%M')}, "
            f"time_range={earliest_time.strftime('%H:%M')}-{latest_time.strftime('%H:%M')}, "
            f"players={num_players}, fallback_window={fallback_window_minutes}min"
        )
        driver = self._create_driver()
        try:
            logger.debug("BOOKING_DEBUG: Step 1/5 - Logging in to Walden Golf")
            if not self._perform_login(driver):
                logger.error("BOOKING_DEBUG: Login failed")
                return BookingResult(
                    success=False,
                    error_message="Failed to log in to Walden Golf",
                )
            logger.debug("BOOKING_DEBUG: Login successful")

            logger.debug("BOOKING_DEBUG: Step 2/5 - Navigating to tee time booking page")
            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))
            logger.debug(f"BOOKING_DEBUG: Tee time page loaded. URL: {driver.current_url}")

            logger.debug("BOOKING_DEBUG: Step 3/5 - Selecting course and date")
            if not self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME):
                logger.error("BOOKING_DEBUG: Course selection/verification failed")
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Failed to select or verify {self.NORTHGATE_COURSE_NAME} course. "
                        f"The booking may have been attempted on the wrong course. "
                        f"Please verify the course selection manually."
                    ),
                )
            if not self._select_date_sync(driver, target_date):
                logger.error("BOOKING_DEBUG: Date selection failed")
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Failed to select date {target_date.strftime('%m/%d/%Y')}. "
                        f"Cannot proceed with booking - would search wrong date."
                    ),
                )

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form",
                    )
                )
            )
            logger.debug("BOOKING_DEBUG: Course and date selection complete")

            logger.debug("BOOKING_DEBUG: Step 4/5 - Finding and booking time slot")
            result = self._find_and_book_time_slot_sync(
                driver,
                target_time,
                num_players,
                fallback_window_minutes,
                tee_time_interval_minutes=tee_time_interval_minutes,
            )

            logger.info(
                f"BOOKING_DEBUG: Step 5/5 - Booking result: success={result.success}, "
                f"booked_time={result.booked_time}, confirmation={result.confirmation_number}, "
                f"error={result.error_message}"
            )
            return result

        except TimeoutException as e:
            logger.error(f"BOOKING_DEBUG: Booking timeout exception: {e}")
            self._capture_diagnostic_info(driver, "booking_timeout")
            return BookingResult(
                success=False,
                error_message=f"Booking timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"BOOKING_DEBUG: Booking WebDriver exception: {e}")
            self._capture_diagnostic_info(driver, "booking_webdriver_error")
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )
        finally:
            logger.debug("BOOKING_DEBUG: === BOOKING ATTEMPT COMPLETE - Closing driver ===")
            driver.quit()

    async def book_multiple_tee_times(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None = None,
    ) -> BatchBookingResult:
        """
        Book multiple tee times in a single session for efficiency.

        This method is optimized for booking multiple tee times on the same date:
        1. Creates a single WebDriver session
        2. Logs in once
        3. If execute_at is provided, waits until that time before booking
        4. Books all requested times in sequence
        5. Returns results for all bookings

        Args:
            target_date: The date to book (all requests must be for this date)
            requests: List of booking requests to execute
            execute_at: Optional datetime to wait until before starting bookings.
                       If provided, the method will log in early and wait until
                       this time before refreshing the page and booking.

        Returns:
            BatchBookingResult with results for each booking request
        """
        return await asyncio.to_thread(
            self._book_multiple_tee_times_sync,
            target_date,
            requests,
            execute_at,
        )

    def _book_multiple_tee_times_sync(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None,
    ) -> BatchBookingResult:
        """
        Synchronous batch booking implementation with single driver lifecycle.

        Creates driver once, logs in once, then books all requested times in sequence.
        If execute_at is provided, waits until that time before refreshing and booking.

        Requests are sorted by target_time to process earlier times first, which helps
        avoid conflicts where a fallback slot for an earlier booking takes a slot needed
        by a later booking.
        """
        if not requests:
            return BatchBookingResult()

        # Sort requests by target_time to process earlier times first
        # This helps avoid conflicts where fallback slots overlap with later bookings
        sorted_requests = sorted(requests, key=lambda r: r.target_time)

        # Build list of all requested times and their fallback windows for conflict detection
        # Each entry is (target_time, fallback_window_minutes, booking_id)
        pending_booking_times: list[tuple[time, int, str]] = [
            (req.target_time, req.fallback_window_minutes, req.booking_id)
            for req in sorted_requests
        ]

        logger.info(
            f"BATCH_BOOKING: === STARTING BATCH BOOKING === "
            f"date={target_date} ({target_date.strftime('%A')}), "
            f"num_requests={len(sorted_requests)}, "
            f"execute_at={execute_at.strftime('%H:%M:%S') if execute_at else 'immediate'}, "
            f"sorted_times={[r.target_time.strftime('%H:%M') for r in sorted_requests]}"
        )

        results: list[BatchBookingItemResult] = []
        total_succeeded = 0
        total_failed = 0

        driver = self._create_driver()
        try:
            logger.info("BATCH_BOOKING: Step 1 - Logging in to Walden Golf")
            if not self._perform_login(driver):
                logger.error("BATCH_BOOKING: Login failed")
                for req in sorted_requests:
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message="Failed to log in to Walden Golf",
                            ),
                        )
                    )
                    total_failed += 1
                return BatchBookingResult(
                    results=results,
                    total_succeeded=total_succeeded,
                    total_failed=total_failed,
                )
            logger.info("BATCH_BOOKING: Login successful")

            logger.info("BATCH_BOOKING: Step 2 - Navigating to tee time booking page")
            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))
            logger.info(f"BATCH_BOOKING: Tee time page loaded. URL: {driver.current_url}")

            logger.info("BATCH_BOOKING: Step 3 - Selecting course")
            if not self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME):
                logger.error("BATCH_BOOKING: Course selection/verification failed")
                for req in sorted_requests:
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=(
                                    f"Failed to select or verify {self.NORTHGATE_COURSE_NAME} course."
                                ),
                            ),
                        )
                    )
                    total_failed += 1
                return BatchBookingResult(
                    results=results,
                    total_succeeded=total_succeeded,
                    total_failed=total_failed,
                )

            # Step 4 - Select date BEFORE waiting for booking window
            # Slot availability is already visible on the page before 6:30 AM.
            # We do all preparation (date selection, scrolling, slot pre-location)
            # before the wait so that at 6:30 AM the ONLY work is clicking Reserve.
            logger.info("BATCH_BOOKING: Step 4 - Selecting date")
            if not self._select_date_sync(driver, target_date):
                logger.error("BATCH_BOOKING: Date selection failed")
                for req in sorted_requests:
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=(
                                    f"Failed to select date {target_date.strftime('%m/%d/%Y')}. "
                                    f"Cannot proceed with booking - would search wrong date."
                                ),
                            ),
                        )
                    )
                    total_failed += 1
                return BatchBookingResult(
                    results=results,
                    total_succeeded=total_succeeded,
                    total_failed=total_failed,
                )

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form",
                    )
                )
            )
            logger.info("BATCH_BOOKING: Date selection complete")

            # Step 5 - Pre-scroll tee sheet to load all needed slot items
            max_needed_minutes = None
            for req in sorted_requests:
                req_minutes = req.target_time.hour * 60 + req.target_time.minute
                req_end_minutes = min(24 * 60 - 1, req_minutes + req.fallback_window_minutes)
                if max_needed_minutes is None or req_end_minutes > max_needed_minutes:
                    max_needed_minutes = req_end_minutes

            if max_needed_minutes is not None:
                logger.info(
                    "BATCH_BOOKING: Step 5 - Pre-scrolling tee sheet to latest needed time "
                    f"{time(max_needed_minutes // 60, max_needed_minutes % 60).strftime('%I:%M %p')}"
                )
                self._scroll_to_load_all_slots(
                    driver,
                    target_time=sorted_requests[-1].target_time,
                    fallback_window_minutes=sorted_requests[-1].fallback_window_minutes,
                    max_time_minutes_override=max_needed_minutes,
                )

            # Step 6 - Pre-locate target slots using JavaScript
            # When execute_at is set, we scan the DOM NOW (before the booking
            # window opens) so that at 6:30 AM we only need to click, not search.
            # Each slot is cached by booking_id; at click time the prelocated slot
            # is reused unless a dynamic times_to_exclude conflict invalidates it,
            # in which case a fresh DOM scan occurs as a fallback.
            use_fast_booking = execute_at is not None
            prelocated_slots: dict[str, dict[str, Any]] = {}
            if use_fast_booking:
                logger.info(
                    "BATCH_BOOKING: Step 6 - Pre-locating target slots via JavaScript "
                    f"for {len(sorted_requests)} request(s)"
                )
                for req in sorted_requests:
                    # Build a preliminary times_to_exclude using only the known
                    # target times of other requests (booked_times is empty here).
                    prelim_exclude: set[time] = set()
                    req_minutes = req.target_time.hour * 60 + req.target_time.minute
                    for later_time, _later_window, later_id in pending_booking_times:
                        if later_id == req.booking_id:
                            continue
                        later_minutes = later_time.hour * 60 + later_time.minute
                        if later_minutes > req_minutes:
                            prelim_exclude.add(later_time)

                    slot = self._find_target_slot_js(
                        driver,
                        req.target_time,
                        req.num_players,
                        req.fallback_window_minutes,
                        req.tee_time_interval_minutes,
                        prelim_exclude,
                    )
                    if slot is not None:
                        prelocated_slots[req.booking_id] = slot
                        slot_time = time(slot["hours"], slot["minutes"])
                        logger.info(
                            f"BATCH_BOOKING: Pre-located slot for booking_id={req.booking_id}: "
                            f"{slot_time.strftime('%I:%M %p')} (index={slot['index']}, "
                            f"exact={slot['isExact']})"
                        )
                    else:
                        logger.warning(
                            f"BATCH_BOOKING: No slot found during pre-location for "
                            f"booking_id={req.booking_id} "
                            f"(target={req.target_time.strftime('%I:%M %p')}); "
                            f"will re-scan at click time"
                        )

            # Step 7 - Wait until execute_at with millisecond precision
            if execute_at:
                # No page refresh at 6:30 AM. Slot availability is already on the page.
                # The server simply starts accepting bookings at 6:30 AM.
                logger.info("BATCH_BOOKING: Step 7 - Precision wait until booking window opens")
                self._precision_wait_until(execute_at)

            # Track times that have been successfully booked to avoid conflicts
            # When a booking succeeds, we add its booked_time to this set
            booked_times: set[time] = set()

            logger.info(
                f"BATCH_BOOKING: Step 8 - Booking {len(sorted_requests)} tee times"
                f"{f' (fast JS mode, {len(prelocated_slots)} pre-located)' if use_fast_booking else ''}"
            )
            for i, req in enumerate(sorted_requests, 1):
                # Calculate times to exclude: times already booked + times needed by later bookings
                # This prevents a fallback slot from taking a time needed by a later booking
                times_to_exclude = booked_times.copy()

                # Add times that are within the fallback window of later bookings
                for later_time, later_window, later_id in pending_booking_times:
                    if later_id == req.booking_id:
                        continue  # Skip current booking
                    # Check if this later booking's target time could conflict
                    later_minutes = later_time.hour * 60 + later_time.minute
                    current_minutes = req.target_time.hour * 60 + req.target_time.minute
                    # Only protect times for bookings that haven't been processed yet
                    if later_minutes > current_minutes:
                        times_to_exclude.add(later_time)

                logger.info(
                    f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} - "
                    f"time={req.target_time.strftime('%H:%M')}, "
                    f"players={req.num_players}, booking_id={req.booking_id}, "
                    f"excluding_times={[t.strftime('%H:%M') for t in sorted(times_to_exclude)]}"
                )

                try:
                    result = self._find_and_book_time_slot_sync(
                        driver,
                        req.target_time,
                        req.num_players,
                        req.fallback_window_minutes,
                        times_to_exclude=times_to_exclude,
                        tee_time_interval_minutes=req.tee_time_interval_minutes,
                        skip_scroll=True,
                        use_fast_js=use_fast_booking,
                        prelocated_slot=prelocated_slots.get(req.booking_id),
                    )

                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=result,
                        )
                    )

                    if result.success:
                        total_succeeded += 1
                        # Track the booked time to avoid conflicts with later bookings
                        if result.booked_time:
                            booked_times.add(result.booked_time)
                        logger.info(
                            f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} SUCCESS - "
                            f"booked_time={result.booked_time}, "
                            f"confirmation={result.confirmation_number}"
                        )
                    else:
                        total_failed += 1
                        logger.warning(
                            f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} FAILED - "
                            f"error={result.error_message}"
                        )

                    if i < len(sorted_requests):
                        logger.info(
                            "BATCH_BOOKING: Navigating back to tee time page for next booking"
                        )
                        driver.get(self.TEE_TIME_URL)
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (By.CSS_SELECTOR, "form")
                            )
                        )
                        if not self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME):
                            logger.warning("BATCH_BOOKING: Course re-selection failed")
                        if not self._select_date_sync(driver, target_date):
                            logger.error("BATCH_BOOKING: Date re-selection failed for next booking")
                            # Continue with remaining bookings but they will likely fail
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (
                                    By.CSS_SELECTOR,
                                    ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form",
                                )
                            )
                        )

                        remaining_needed_minutes = None
                        for remaining_req in sorted_requests[i:]:
                            remaining_minutes = (
                                remaining_req.target_time.hour * 60
                                + remaining_req.target_time.minute
                            )
                            remaining_end_minutes = min(
                                24 * 60 - 1,
                                remaining_minutes + remaining_req.fallback_window_minutes,
                            )
                            if (
                                remaining_needed_minutes is None
                                or remaining_end_minutes > remaining_needed_minutes
                            ):
                                remaining_needed_minutes = remaining_end_minutes

                        if remaining_needed_minutes is not None:
                            logger.info(
                                "BATCH_BOOKING: Pre-scrolling tee sheet for remaining bookings to "
                                f"{time(remaining_needed_minutes // 60, remaining_needed_minutes % 60).strftime('%I:%M %p')}"
                            )
                            self._scroll_to_load_all_slots(
                                driver,
                                target_time=sorted_requests[-1].target_time,
                                fallback_window_minutes=sorted_requests[-1].fallback_window_minutes,
                                max_time_minutes_override=remaining_needed_minutes,
                            )

                except Exception as e:
                    logger.error(f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} ERROR - {e}")
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=f"Booking error: {str(e)}",
                            ),
                        )
                    )
                    total_failed += 1

            logger.info(
                f"BATCH_BOOKING: === BATCH COMPLETE === "
                f"succeeded={total_succeeded}, failed={total_failed}"
            )

            return BatchBookingResult(
                results=results,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
            )

        except TimeoutException as e:
            logger.error(f"BATCH_BOOKING: Timeout exception: {e}")
            self._capture_diagnostic_info(driver, "batch_booking_timeout")
            for req in sorted_requests:
                if not any(r.booking_id == req.booking_id for r in results):
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=f"Batch booking timeout: {str(e)}",
                            ),
                        )
                    )
                    total_failed += 1
            return BatchBookingResult(
                results=results,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
            )
        except WebDriverException as e:
            logger.error(f"BATCH_BOOKING: WebDriver exception: {e}")
            self._capture_diagnostic_info(driver, "batch_booking_webdriver_error")
            for req in sorted_requests:
                if not any(r.booking_id == req.booking_id for r in results):
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=f"Batch booking error: {str(e)}",
                            ),
                        )
                    )
                    total_failed += 1
            return BatchBookingResult(
                results=results,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
            )
        finally:
            logger.info("BATCH_BOOKING: === BATCH BOOKING COMPLETE - Closing driver ===")
            driver.quit()

    def _select_course_sync(self, driver: webdriver.Chrome, course_name: str) -> bool:
        """
        Select the course from the multi-select checkbox dropdown.

        The Walden Golf tee time page uses a multi-select dropdown with checkboxes
        for course selection. By default, both Northgate and Walden on Lake Conroe
        are selected, showing tee times for both courses in separate columns.

        To prevent accidental bookings at the wrong course, this method:
        1. Opens the course selection dropdown
        2. Ensures the target course (Northgate) is checked
        3. Unchecks other courses (Walden on Lake Conroe) to show only Northgate times
        4. Closes the dropdown and verifies the selection

        Args:
            driver: The WebDriver instance
            course_name: The name of the course to select (e.g., "Northgate")

        Returns:
            True if the correct course is selected/verified, False otherwise
        """
        walden_course_name = "Walden on Lake Conroe"

        try:
            if self._select_course_via_checkbox_dropdown(driver, course_name, walden_course_name):
                logger.info(f"Successfully configured course selection for {course_name} only")
            else:
                if self._select_course_via_standard_dropdown(driver, course_name):
                    logger.info(f"Selected course via standard dropdown: {course_name}")
                else:
                    logger.warning(
                        f"No course dropdown found - attempting to verify "
                        f"current course is {course_name}"
                    )
        except Exception as e:
            logger.warning(f"Error during course selection: {e}")

        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

        if self._verify_course_selection(driver, course_name):
            logger.info(f"Verified: Currently on {course_name} course page")
            return True
        else:
            logger.error(
                f"BOOKING_DEBUG: Failed to verify {course_name} course selection. "
                f"May be on wrong course page."
            )
            return False

    def _select_course_via_checkbox_dropdown(
        self, driver: webdriver.Chrome, target_course: str, course_to_deselect: str
    ) -> bool:
        """
        Handle multi-select checkbox dropdown for course selection.

        The Walden Golf site uses a custom dropdown with checkboxes where multiple
        courses can be selected simultaneously. This method ensures only the target
        course is selected by checking it and unchecking others.

        Args:
            driver: The WebDriver instance
            target_course: Course to ensure is checked (e.g., "Northgate")
            course_to_deselect: Course to uncheck (e.g., "Walden on Lake Conroe")

        Returns:
            True if checkbox dropdown was found and configured, False otherwise
        """
        try:
            dropdown_trigger_selectors = [
                "[class*='select'][class*='course']",
                "div[class*='multiselect']",
                "button[class*='dropdown']",
                ".course-dropdown",
                "[aria-label*='course' i]",
                "[placeholder*='course' i]",
            ]

            dropdown_trigger = None
            for selector in dropdown_trigger_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        if elem.is_displayed():
                            dropdown_trigger = elem
                            break
                    if dropdown_trigger:
                        break
                except NoSuchElementException:
                    continue

            if not dropdown_trigger:
                try:
                    dropdown_trigger = driver.find_element(
                        By.XPATH,
                        "//*[contains(text(), 'Select Course') or contains(text(), 'Course')]"
                        "[contains(@class, 'select') or contains(@class, 'dropdown') or "
                        "self::button or self::div[contains(@class, 'trigger')]]",
                    )
                except NoSuchElementException:
                    pass

            if not dropdown_trigger:
                logger.debug("No checkbox dropdown trigger found for course selection")
                return False

            dropdown_trigger.click()
            logger.info("Opened course selection dropdown")
            self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

            checkbox_items = driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='checkbox'], "
                "li[class*='option'], "
                "div[class*='option'], "
                "label[class*='checkbox']",
            )

            if not checkbox_items:
                checkbox_items = driver.find_elements(
                    By.XPATH,
                    "//li[.//input[@type='checkbox']] | "
                    "//div[contains(@class, 'option')] | "
                    "//label[contains(@class, 'check')]",
                )

            target_found = False
            deselect_found = False

            for item in checkbox_items:
                item_text = item.text.lower() if item.text else ""
                if not item_text:
                    try:
                        item_text = (item.get_attribute("textContent") or "").lower()
                    except Exception:
                        continue

                if target_course.lower() in item_text:
                    target_found = True
                    checkbox = self._find_checkbox_in_element(driver, item, target_course)
                    if checkbox and not checkbox.is_selected():
                        self._click_checkbox_or_label(driver, item, checkbox)
                        logger.info(f"Checked '{target_course}' in course dropdown")
                    elif checkbox and checkbox.is_selected():
                        logger.info(f"'{target_course}' already checked")

                elif course_to_deselect.lower() in item_text:
                    deselect_found = True
                    checkbox = self._find_checkbox_in_element(driver, item, course_to_deselect)
                    if checkbox and checkbox.is_selected():
                        self._click_checkbox_or_label(driver, item, checkbox)
                        logger.info(f"Unchecked '{course_to_deselect}' in course dropdown")
                    elif checkbox and not checkbox.is_selected():
                        logger.info(f"'{course_to_deselect}' already unchecked")

            try:
                close_button = driver.find_element(
                    By.CSS_SELECTOR, "[class*='close'], .x, button[aria-label='close']"
                )
                close_button.click()
            except NoSuchElementException:
                try:
                    dropdown_trigger.click()
                except Exception:
                    driver.find_element(By.TAG_NAME, "body").click()

            self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

            if target_found:
                logger.info(
                    f"Course dropdown configured: {target_course}=checked, "
                    f"{course_to_deselect}={'unchecked' if deselect_found else 'not found'}"
                )
                return True

            logger.warning(f"Target course '{target_course}' not found in dropdown options")
            return False

        except Exception as e:
            logger.debug(f"Checkbox dropdown selection failed: {e}")
            return False

    def _find_checkbox_in_element(
        self, driver: webdriver.Chrome, container: Any, course_name: str
    ) -> Any | None:
        """Find the checkbox input within a container element."""
        try:
            return container.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
        except NoSuchElementException:
            pass

        try:
            return container.find_element(By.TAG_NAME, "input")
        except NoSuchElementException:
            pass

        try:
            return driver.find_element(
                By.XPATH,
                f"//input[@type='checkbox'][following-sibling::*[contains(text(), '{course_name}')] "
                f"or preceding-sibling::*[contains(text(), '{course_name}')]]",
            )
        except NoSuchElementException:
            pass

        return None

    def _get_visible_page_text(self, driver: webdriver.Chrome) -> str:
        """Get visible text from the page (prefer <body>.text over raw HTML source)."""
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body_text = getattr(body, "text", "")
            if isinstance(body_text, str) and body_text.strip():
                return body_text
        except Exception:
            pass

        page_source = getattr(driver, "page_source", "")
        return page_source if isinstance(page_source, str) else ""

    def _extract_booking_error_message(self, driver: webdriver.Chrome) -> str | None:
        """Extract user-visible booking error text from common alert/message containers."""
        selectors = [
            ".ui-messages-error",
            ".ui-message-error",
            ".ui-growl-message-error",
            ".error",
            ".errors",
            "[class*='error']",
            ".alert",
            ".alert-danger",
            "[role='alert']",
            "[aria-live='assertive']",
            "[aria-live='polite']",
        ]

        try:
            messages: list[str] = []
            for sel in selectors:
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel)[:10]:
                        try:
                            if (el.get_attribute("aria-hidden") or "").lower() == "true":
                                continue
                        except Exception:
                            pass

                        try:
                            if not el.is_displayed():
                                continue
                        except Exception:
                            pass

                        text = (getattr(el, "text", "") or "").strip()
                        if text:
                            messages.append(text)
                except Exception:
                    continue

            if messages:
                unique: list[str] = []
                for msg in messages:
                    if msg not in unique:
                        unique.append(msg)
                joined = " | ".join(unique)
                return joined[:500]
        except Exception:
            pass

        # Fallback: provide a short snippet of visible text if it contains likely failure words.
        visible_text = self._get_visible_page_text(driver)
        visible_lower = visible_text.lower()
        if any(word in visible_lower for word in ("error", "unable", "failed", "unavailable")):
            snippet = " ".join(visible_text.split())
            return snippet[:500]

        return None

    def _click_checkbox_or_label(
        self, driver: webdriver.Chrome, container: Any, checkbox: Any
    ) -> None:
        """Click the checkbox or its label to toggle selection."""
        try:
            checkbox.click()
            return
        except Exception:
            pass

        try:
            label = container.find_element(By.TAG_NAME, "label")
            label.click()
            return
        except Exception:
            pass

        try:
            container.click()
            return
        except Exception:
            pass

        try:
            driver.execute_script("arguments[0].click();", checkbox)
        except Exception as e:
            logger.warning(f"Failed to click checkbox: {e}")

    def _select_course_via_standard_dropdown(
        self, driver: webdriver.Chrome, course_name: str
    ) -> bool:
        """
        Fallback: Select course using standard HTML select dropdown.

        Args:
            driver: The WebDriver instance
            course_name: The name of the course to select

        Returns:
            True if course was selected, False otherwise
        """
        course_dropdown_selectors = [
            "select[id*='course']",
            "select[name*='course']",
            "select[id*='Course']",
            "select[name*='Course']",
            "select.course-select",
            "#courseSelect",
        ]

        for selector in course_dropdown_selectors:
            try:
                course_select = driver.find_element(By.CSS_SELECTOR, selector)
                select = Select(course_select)

                for option in select.options:
                    if course_name.lower() in option.text.lower():
                        select.select_by_visible_text(option.text)
                        logger.info(f"Selected course: {option.text} using selector: {selector}")
                        wait = WebDriverWait(driver, 10)
                        try:
                            wait.until(expected_conditions.staleness_of(course_select))
                        except TimeoutException:
                            pass
                        return True

            except NoSuchElementException:
                continue

        return False

    def _verify_course_selection(self, driver: webdriver.Chrome, course_name: str) -> bool:
        """
        Verify that the correct course is currently selected/displayed.

        Checks multiple indicators on the page to confirm we're viewing
        the correct course's tee times.

        Args:
            driver: The WebDriver instance
            course_name: The expected course name (e.g., "Northgate")

        Returns:
            True if the correct course is verified, False otherwise
        """
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            course_name_lower = course_name.lower()

            if course_name_lower in page_text:
                logger.debug(f"Found '{course_name}' in page text")
                return True

            course_indicators = [
                f"h1:contains('{course_name}')",
                f"h2:contains('{course_name}')",
                f".course-name:contains('{course_name}')",
                f"[class*='course']:contains('{course_name}')",
            ]

            for indicator in course_indicators:
                try:
                    elements = driver.find_elements(
                        By.XPATH,
                        f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                        f"'abcdefghijklmnopqrstuvwxyz'), '{course_name_lower}')]",
                    )
                    if elements:
                        logger.debug(f"Found course indicator element for '{course_name}'")
                        return True
                except NoSuchElementException:
                    continue

            try:
                selected_options = driver.find_elements(
                    By.CSS_SELECTOR, "select option:checked, select option[selected]"
                )
                for option in selected_options:
                    if course_name_lower in option.text.lower():
                        logger.debug(f"Found '{course_name}' in selected dropdown option")
                        return True
            except NoSuchElementException:
                pass

            logger.warning(
                f"Could not verify course '{course_name}' on page. "
                f"Page may be on a different course."
            )
            return False

        except Exception as e:
            logger.error(f"Error verifying course selection: {e}")
            return False

    def _select_date_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select the target date using various date selection mechanisms.

        The Northstar Technologies tee sheet may use different date selection methods:
        1. Date input field (various selectors)
        2. Date picker widget
        3. Day-of-week tabs
        4. Calendar navigation

        This method tries multiple approaches in order of likelihood.

        Returns:
            True if date was successfully selected, False otherwise.
        """
        day_name = target_date.strftime("%A")
        date_str = target_date.strftime("%m/%d/%Y")
        date_str_alt = target_date.strftime("%Y-%m-%d")
        logger.info(f"BOOKING_DEBUG: Selecting date {target_date} ({day_name})")

        date_input_selectors = [
            "input[type='text'][id*='date']",
            "input[type='date']",
            "input[id*='date']",
            "input[name*='date']",
            "input[class*='date']",
            "input[placeholder*='date' i]",
            "input[placeholder*='mm/dd' i]",
            ".datepicker input",
            "[data-date] input",
        ]

        for selector in date_input_selectors:
            try:
                date_input = driver.find_element(By.CSS_SELECTOR, selector)
                input_type = date_input.get_attribute("type")

                date_input.clear()
                if input_type == "date":
                    date_input.send_keys(date_str_alt)
                else:
                    date_input.send_keys(date_str)
                logger.info(f"BOOKING_DEBUG: Entered date {date_str} using selector: {selector}")

                wait = WebDriverWait(driver, 5)
                try:
                    search_button = wait.until(
                        expected_conditions.element_to_be_clickable(
                            (
                                By.CSS_SELECTOR,
                                "button[type='submit'], input[type='submit'], button.search, .btn-search",
                            )
                        )
                    )
                    search_button.click()
                    logger.info("BOOKING_DEBUG: Clicked search/submit button after date entry")
                except TimeoutException:
                    pass

                return True

            except NoSuchElementException:
                continue

        # Skip day tab lookup - go directly to calendar picker for faster date selection
        logger.info("BOOKING_DEBUG: No date input found, using calendar picker...")
        if self._select_date_via_calendar_sync(driver, target_date):
            logger.info("BOOKING_DEBUG: Date selection via calendar successful")
            return True
        else:
            logger.error(
                f"BOOKING_DEBUG: Calendar date selection failed for {target_date}. "
                f"Cannot proceed with booking on wrong date."
            )
            return False

    def _select_date_via_calendar_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select date using a calendar picker widget if available.

        Handles month navigation when the target date is in a different month than
        the currently displayed month. Uses the month/year dropdowns or navigation
        arrows to reach the correct month before selecting the day.

        Returns:
            True if date was selected successfully, False otherwise.
        """
        try:
            calendar_triggers = driver.find_elements(
                By.CSS_SELECTOR,
                ".calendar-trigger, .datepicker-trigger, [class*='calendar'], "
                "button[aria-label*='calendar' i], .ui-datepicker-trigger, "
                "span.icon-calendar, i.fa-calendar",
            )

            if calendar_triggers:
                calendar_triggers[0].click()
                logger.info("BOOKING_DEBUG: Clicked calendar trigger")

                wait = WebDriverWait(driver, 5)
                try:
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                ".ui-datepicker, .datepicker, [class*='calendar-popup'], "
                                ".ui-datepicker-calendar, select[class*='month'], select[class*='year']",
                            )
                        )
                    )
                    logger.info("BOOKING_DEBUG: Calendar popup appeared")

                    # Navigate to the correct month/year if needed
                    if not self._navigate_calendar_to_month(driver, target_date):
                        logger.warning(
                            f"BOOKING_DEBUG: Failed to navigate calendar to {target_date.strftime('%B %Y')}"
                        )
                        return False

                    # Now select the day
                    day_str = str(target_date.day)
                    day_elements = driver.find_elements(
                        By.XPATH,
                        f"//td[@data-date='{target_date.day}'] | "
                        f"//a[text()='{day_str}'] | "
                        f"//td[contains(@class, 'day') and text()='{day_str}'] | "
                        f"//td[normalize-space(text())='{day_str}']",
                    )

                    logger.info(
                        f"BOOKING_DEBUG: Found {len(day_elements)} day elements for day {day_str}"
                    )

                    for day_el in day_elements:
                        if day_el.is_displayed() and day_el.is_enabled():
                            # Avoid clicking on days from adjacent months (often grayed out)
                            day_class = day_el.get_attribute("class") or ""
                            if "ui-datepicker-other-month" in day_class or "disabled" in day_class:
                                logger.debug(
                                    f"BOOKING_DEBUG: Skipping day element with class: {day_class}"
                                )
                                continue

                            day_el.click()
                            logger.info(
                                f"BOOKING_DEBUG: Selected day {day_str} from calendar for date {target_date}"
                            )
                            # Wait for page to reload after date selection
                            self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)
                            # Wait for tee time slots to appear
                            try:
                                WebDriverWait(driver, 10).until(
                                    expected_conditions.presence_of_element_located(
                                        (
                                            By.CSS_SELECTOR,
                                            ".custom-free-slot-span, .teetime-row, [class*='tee-time'], "
                                            "li.ui-datascroller-item",
                                        )
                                    )
                                )
                            except TimeoutException:
                                logger.debug(
                                    "BOOKING_DEBUG: Tee time slots not found after calendar selection"
                                )
                            return True

                    logger.warning(
                        f"BOOKING_DEBUG: No clickable day element found for day {day_str}"
                    )

                except TimeoutException:
                    logger.warning("BOOKING_DEBUG: Calendar popup did not appear")

        except Exception as e:
            logger.warning(f"BOOKING_DEBUG: Calendar selection failed: {e}")

        return False

    def _navigate_calendar_to_month(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Navigate the calendar to the correct month and year.

        Tries multiple strategies:
        1. Use month/year dropdown selects if available
        2. Use next/prev navigation arrows

        Args:
            driver: The WebDriver instance
            target_date: The target date to navigate to

        Returns:
            True if navigation succeeded or no navigation needed, False otherwise
        """
        target_month = target_date.month
        target_year = target_date.year
        target_month_name = target_date.strftime("%B")  # e.g., "February"
        target_month_abbr = target_date.strftime("%b")  # e.g., "Feb"

        logger.info(f"BOOKING_DEBUG: Navigating calendar to {target_month_name} {target_year}")

        # Strategy 1: Try month/year dropdown selects
        try:
            # Look for month dropdown - try various selectors
            month_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-month, select[class*='month'], "
                "select[data-handler='selectMonth'], select[name*='month']",
            )
            year_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-year, select[class*='year'], "
                "select[data-handler='selectYear'], select[name*='year']",
            )

            if month_selects and year_selects:
                logger.info("BOOKING_DEBUG: Found month/year dropdowns, using select strategy")

                # Select year first
                year_select = Select(year_selects[0])
                try:
                    year_select.select_by_value(str(target_year))
                    logger.info(f"BOOKING_DEBUG: Selected year {target_year} from dropdown")
                except Exception:
                    try:
                        year_select.select_by_visible_text(str(target_year))
                        logger.info(f"BOOKING_DEBUG: Selected year {target_year} by text")
                    except Exception as e:
                        logger.warning(f"BOOKING_DEBUG: Could not select year: {e}")

                self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)

                # Select month (0-indexed in some implementations, 1-indexed in others)
                month_select = Select(month_selects[0])
                try:
                    # Try 0-indexed first (JavaScript Date style)
                    month_select.select_by_value(str(target_month - 1))
                    logger.info(
                        f"BOOKING_DEBUG: Selected month {target_month_name} (value={target_month - 1})"
                    )
                except Exception:
                    try:
                        # Try 1-indexed
                        month_select.select_by_value(str(target_month))
                        logger.info(
                            f"BOOKING_DEBUG: Selected month {target_month_name} (value={target_month})"
                        )
                    except Exception:
                        try:
                            # Try by visible text
                            month_select.select_by_visible_text(target_month_name)
                            logger.info(
                                f"BOOKING_DEBUG: Selected month {target_month_name} by text"
                            )
                        except Exception:
                            try:
                                month_select.select_by_visible_text(target_month_abbr)
                                logger.info(
                                    f"BOOKING_DEBUG: Selected month {target_month_abbr} by abbr"
                                )
                            except Exception as e:
                                logger.warning(f"BOOKING_DEBUG: Could not select month: {e}")

                self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.2)
                return True

        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Dropdown strategy failed: {e}")

        # Strategy 2: Use navigation arrows to move month by month
        try:
            # Determine current month/year displayed
            current_month, current_year = self._get_calendar_current_month(driver)

            if current_month is None or current_year is None:
                logger.warning("BOOKING_DEBUG: Could not determine current calendar month")
                # Assume we need to navigate - try clicking next
                current_month = datetime.now().month
                current_year = datetime.now().year

            logger.info(
                f"BOOKING_DEBUG: Calendar currently showing {current_month}/{current_year}, "
                f"need {target_month}/{target_year}"
            )

            # Calculate months to navigate
            months_diff = (target_year - current_year) * 12 + (target_month - current_month)

            if months_diff == 0:
                logger.info("BOOKING_DEBUG: Already on correct month")
                return True

            # Find navigation buttons
            if months_diff > 0:
                # Need to go forward
                nav_selectors = [
                    "a.ui-datepicker-next",
                    "button.ui-datepicker-next",
                    "[data-handler='next']",
                    ".ui-datepicker-next",
                    "a[title='Next']",
                    "button[title='Next']",
                    "span.ui-icon-circle-triangle-e",
                    "[class*='next']",
                    "a[class*='next']",
                    "button[class*='next']",
                ]
                direction = "next"
            else:
                # Need to go backward
                nav_selectors = [
                    "a.ui-datepicker-prev",
                    "button.ui-datepicker-prev",
                    "[data-handler='prev']",
                    ".ui-datepicker-prev",
                    "a[title='Prev']",
                    "button[title='Prev']",
                    "span.ui-icon-circle-triangle-w",
                    "[class*='prev']",
                    "a[class*='prev']",
                    "button[class*='prev']",
                ]
                direction = "prev"
                months_diff = abs(months_diff)

            nav_button = None
            for selector in nav_selectors:
                try:
                    buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        if btn.is_displayed() and btn.is_enabled():
                            nav_button = btn
                            logger.info(
                                f"BOOKING_DEBUG: Found {direction} nav button with selector: {selector}"
                            )
                            break
                    if nav_button:
                        break
                except Exception:
                    continue

            if not nav_button:
                logger.warning(f"BOOKING_DEBUG: Could not find {direction} navigation button")
                return False

            # Click navigation button for each month we need to move
            for i in range(months_diff):
                try:
                    # Re-find the button each time as DOM may update
                    for selector in nav_selectors:
                        try:
                            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                            for btn in buttons:
                                if btn.is_displayed() and btn.is_enabled():
                                    nav_button = btn
                                    break
                            if nav_button:
                                break
                        except Exception:
                            continue

                    nav_button.click()
                    logger.debug(
                        f"BOOKING_DEBUG: Clicked {direction} button ({i + 1}/{months_diff})"
                    )
                    self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)
                except Exception as e:
                    logger.warning(f"BOOKING_DEBUG: Error clicking nav button: {e}")
                    return False

            logger.info(
                f"BOOKING_DEBUG: Navigated {months_diff} months {direction} to reach "
                f"{target_month_name} {target_year}"
            )
            return True

        except Exception as e:
            logger.warning(f"BOOKING_DEBUG: Navigation arrow strategy failed: {e}")

        return False

    def _get_calendar_current_month(
        self, driver: webdriver.Chrome
    ) -> tuple[int | None, int | None]:
        """
        Determine the currently displayed month and year in the calendar.

        Returns:
            Tuple of (month, year) as integers, or (None, None) if cannot determine
        """
        try:
            # Try to read from month/year dropdowns
            month_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-month, select[class*='month']",
            )
            year_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-year, select[class*='year']",
            )

            if month_selects and year_selects:
                month_select = Select(month_selects[0])
                year_select = Select(year_selects[0])

                # Get selected values
                selected_month = month_select.first_selected_option
                selected_year = year_select.first_selected_option

                month_val = selected_month.get_attribute("value")
                year_val = selected_year.get_attribute("value")

                if month_val is not None and year_val is not None:
                    # Month might be 0-indexed
                    month_int = int(month_val)
                    if month_int < 12:  # Likely 0-indexed
                        month_int += 1
                    return month_int, int(year_val)

            # Try to read from header text (e.g., "January 2026" or "Jan 2026")
            header_selectors = [
                ".ui-datepicker-title",
                ".datepicker-title",
                "[class*='calendar-header']",
                "[class*='datepicker-header']",
            ]

            for selector in header_selectors:
                try:
                    headers = driver.find_elements(By.CSS_SELECTOR, selector)
                    for header in headers:
                        text = header.text.strip()
                        if text:
                            # Try to parse "January 2026" or "Jan 2026"
                            for fmt in ["%B %Y", "%b %Y"]:
                                try:
                                    parsed = datetime.strptime(text, fmt)
                                    return parsed.month, parsed.year
                                except ValueError:
                                    continue
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Error getting current calendar month: {e}")

        return None, None

    def _select_date_via_tabs_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select date using the day-of-week tabs if date picker not available.

        Returns:
            True if date was selected successfully, False otherwise.
        """
        day_name = target_date.strftime("%A")
        date_str = target_date.strftime("%m/%d")
        logger.debug(f"BOOKING_DEBUG: Looking for day tab for {day_name} ({date_str})")

        try:
            day_tabs = driver.find_elements(
                By.CSS_SELECTOR,
                ".day-tab, [class*='day-tab'], a[href*='day'], "
                "[data-day], .teetime-day-tab, .nav-tabs a",
            )
            logger.debug(f"BOOKING_DEBUG: Found {len(day_tabs)} potential day tabs")

            for i, tab in enumerate(day_tabs):
                tab_text = tab.text.lower()
                logger.debug(f"BOOKING_DEBUG: Tab {i}: text='{tab_text}'")
                if day_name.lower() in tab_text or date_str in tab.text:
                    wait = WebDriverWait(driver, 10)
                    try:
                        wait.until(expected_conditions.element_to_be_clickable(tab))
                        tab.click()
                        logger.debug(f"BOOKING_DEBUG: Clicked day tab: {day_name}")
                        wait.until(expected_conditions.staleness_of(tab))
                    except TimeoutException:
                        tab.click()
                        logger.info(
                            f"BOOKING_DEBUG: Clicked day tab (no staleness wait): {day_name}"
                        )
                    return True

            logger.info(
                f"BOOKING_DEBUG: Could not find day tab for {day_name}. Available tabs: {[t.text for t in day_tabs[:5]]}"
            )
            return False

        except NoSuchElementException:
            logger.info("BOOKING_DEBUG: No day tabs found on page")
            return False

    def _select_player_count_sync(self, driver: webdriver.Chrome, num_players: int) -> bool:
        """
        Select the number of players in the booking dialog.

        The player count selector on Walden Golf is a button group (ui-selectonebutton)
        with buttons for 1, 2, 3, 4 players. This method clicks the appropriate button.

        Args:
            driver: The WebDriver instance
            num_players: Number of players (1-4)

        Returns:
            True if player count was successfully selected, False otherwise
        """
        try:
            logger.debug(
                f"BOOKING_DEBUG: Starting player count selection for {num_players} players"
            )
            # Wait for the player count button group to appear
            self.wait_strategy.wait_for_element(
                driver,
                (By.CSS_SELECTOR, ".reservation-players, .ui-selectonebutton"),
                fixed_duration=1.0,
                timeout=5.0,
            )

            # The Walden Golf site uses a button group with class "reservation-players"
            # Each button contains a radio input with value 1, 2, 3, or 4
            # The button div has class "ui-button" and we need to click the one with the correct value

            # First try to find the button group
            button_group_selectors = [
                ".reservation-players",
                ".ui-selectonebutton",
                "[class*='players-sel']",
            ]

            button_group = None
            for selector in button_group_selectors:
                try:
                    button_group = driver.find_element(By.CSS_SELECTOR, selector)
                    logger.info(
                        f"BOOKING_DEBUG: Found player button group with selector: {selector}"
                    )
                    break
                except NoSuchElementException:
                    logger.debug(f"BOOKING_DEBUG: Button group not found with selector: {selector}")
                    continue

            if button_group:
                # Find the button with the correct value
                # The button contains a radio input with the value we want
                try:
                    # Find the radio input with the correct value
                    radio_input = button_group.find_element(
                        By.CSS_SELECTOR, f"input[type='radio'][value='{num_players}']"
                    )
                    # Get the parent div (the clickable button)
                    button_div = radio_input.find_element(By.XPATH, "./..")

                    # Check if the button is disabled
                    button_classes = button_div.get_attribute("class") or ""
                    logger.info(
                        f"BOOKING_DEBUG: Player {num_players} button classes: {button_classes}"
                    )
                    if "ui-state-disabled" in button_classes:
                        logger.error(
                            f"BOOKING_DEBUG: Player count {num_players} button is disabled"
                        )
                        return False

                    # Click the button
                    driver.execute_script("arguments[0].click();", button_div)
                    logger.info(
                        f"BOOKING_DEBUG: Clicked player count button for {num_players} players"
                    )
                    self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

                    # Verify the selection took effect by checking for player rows
                    if not self._verify_player_rows_appeared(driver, num_players):
                        logger.error(
                            f"BOOKING_DEBUG: Player rows did not appear after selecting {num_players} players"
                        )
                        return False

                    logger.debug(f"BOOKING_DEBUG: Successfully selected {num_players} players")
                    return True
                except NoSuchElementException:
                    logger.warning(
                        f"BOOKING_DEBUG: Could not find radio input for {num_players} players"
                    )

                # Alternative strategy: some PrimeFaces/JSF variants render the select-one-button
                # without a visible/usable radio input. In that case, click the button by label.
                try:
                    candidate_buttons = button_group.find_elements(
                        By.CSS_SELECTOR, ".ui-button, button, a, span"
                    )
                    for candidate in candidate_buttons:
                        try:
                            candidate_text = (candidate.text or "").strip()
                            if candidate_text != str(num_players):
                                continue

                            candidate_classes = candidate.get_attribute("class") or ""
                            logger.info(
                                f"BOOKING_DEBUG: Player {num_players} button classes: {candidate_classes}"
                            )
                            if "ui-state-disabled" in candidate_classes:
                                logger.error(
                                    f"BOOKING_DEBUG: Player count {num_players} button is disabled"
                                )
                                return False

                            driver.execute_script("arguments[0].click();", candidate)
                            logger.info(
                                f"BOOKING_DEBUG: Clicked player count button for {num_players} players"
                            )
                            self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

                            if not self._verify_player_rows_appeared(driver, num_players):
                                logger.error(
                                    f"BOOKING_DEBUG: Player rows did not appear after selecting {num_players} players"
                                )
                                return False

                            logger.debug(
                                f"BOOKING_DEBUG: Successfully selected {num_players} players"
                            )
                            return True
                        except Exception:
                            continue
                except Exception:
                    pass

                try:
                    group_html = button_group.get_attribute("outerHTML")
                    if group_html and len(group_html) > 2000:
                        group_html = group_html[:2000] + "... [truncated]"
                    logger.debug(f"BOOKING_DEBUG: Player button group HTML: {group_html}")
                except Exception:
                    pass

            # Fallback: try dropdown selectors
            player_selectors = [
                "select[id*='player']",
                "select[id*='golfer']",
                "select[name*='player']",
                "select[name*='golfer']",
                "select[id*='numPlayers']",
                "select[id*='numberOfPlayers']",
            ]

            for selector in player_selectors:
                try:
                    player_select = driver.find_element(By.CSS_SELECTOR, selector)
                    select = Select(player_select)
                    select.select_by_value(str(num_players))
                    logger.info(f"Selected {num_players} players using selector: {selector}")
                    self.wait_strategy.wait_after_action(driver, fixed_duration=0.5)
                    return True
                except NoSuchElementException:
                    continue
                except Exception as e:
                    logger.debug(f"Unexpected error trying selector {selector}: {e}")
                    continue

            logger.warning(
                f"Could not find player count selector - site may auto-fill or use different control. "
                f"Requested {num_players} players."
            )
            return False

        except Exception as e:
            logger.warning(f"Error selecting player count: {e}")
            return False

    def _verify_player_rows_appeared(self, driver: webdriver.Chrome, expected_players: int) -> bool:
        """
        Verify that the expected number of player rows appeared after selecting player count.

        This is a critical verification step to ensure the booking form properly
        transitioned to show all player slots before attempting to add TBD guests.

        Args:
            driver: The WebDriver instance
            expected_players: Number of player rows expected (including primary player)

        Returns:
            True if expected number of rows found, False otherwise
        """
        logger.debug(f"BOOKING_DEBUG: Verifying {expected_players} player rows appeared")

        # Wait a bit for the DOM to update after player count selection
        self.wait_strategy.wait_for_element(
            driver,
            (By.CSS_SELECTOR, "[id*='playersTable'] tbody tr, table[id*='player'] tbody tr"),
            fixed_duration=2.0,
            timeout=5.0,
        )

        row_selectors = [
            "[id*='playersTable'] tbody tr[data-ri]",
            "[id*='player'] tbody tr[data-ri]",
            "table[id*='player'] tbody tr",
            ".player-row",
            "[class*='player-row']",
        ]

        for selector in row_selectors:
            try:
                player_rows = driver.find_elements(By.CSS_SELECTOR, selector)
                if len(player_rows) >= expected_players:
                    logger.info(
                        f"BOOKING_DEBUG: Found {len(player_rows)} player rows using selector: {selector}"
                    )
                    return True
                elif len(player_rows) > 0:
                    logger.info(
                        f"BOOKING_DEBUG: Found {len(player_rows)} rows (need {expected_players}) "
                        f"using selector: {selector}"
                    )
            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Error checking selector {selector}: {e}")

        # Log diagnostic info about what we found
        try:
            tables = driver.find_elements(By.TAG_NAME, "table")
            logger.debug(f"BOOKING_DEBUG: Page has {len(tables)} tables total")
            for i, table in enumerate(tables[:5]):
                table_id = table.get_attribute("id") or "no-id"
                table_class = table.get_attribute("class") or "no-class"
                rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                logger.info(
                    f"BOOKING_DEBUG: Table {i}: id='{table_id}', class='{table_class}', rows={len(rows)}"
                )
        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Error logging table info: {e}")

        logger.error(
            f"BOOKING_DEBUG: Could not find {expected_players} player rows. "
            f"The player count selection may not have taken effect."
        )
        return False

    def _add_tbd_registered_guests_sync(
        self, driver: webdriver.Chrome, num_tbd_guests: int
    ) -> bool:
        """
        Add TBD Registered Guests for additional player slots.

        After selecting the player count, the booking form shows player rows.
        For players 2, 3, 4, we need to click the "TBD" button to register them
        as TBD Registered Guests.

        Note: After clicking a TBD button, the DOM updates and element references
        become stale. We re-find the player rows after each click to avoid
        stale element reference errors.

        Args:
            driver: The WebDriver instance
            num_tbd_guests: Number of TBD guests to add (1-3)

        Returns:
            True if TBD guests were successfully added, False otherwise
        """
        try:
            logger.info(
                f"BOOKING_DEBUG: Starting TBD guest registration for {num_tbd_guests} guests"
            )
            # Wait for the player table to update after selecting player count
            self.wait_strategy.wait_for_element(
                driver,
                (By.CSS_SELECTOR, "[id*='playersTable'] tbody tr, table[id*='player'] tbody tr"),
                fixed_duration=2.0,
                timeout=5.0,
            )

            tbd_buttons_added = 0

            # Process each guest slot one at a time, re-finding rows after each click
            # to avoid stale element references
            for guest_index in range(num_tbd_guests):
                player_num = guest_index + 2  # Players 2, 3, 4
                logger.info(
                    f"BOOKING_DEBUG: Processing TBD guest {guest_index + 1}/{num_tbd_guests} (player {player_num})"
                )

                # Re-find player rows each iteration to avoid stale references
                # Try multiple selectors for player rows as the DOM structure may vary
                player_rows = []
                row_selectors = [
                    "[id*='playersTable'] tbody tr[data-ri]",
                    "[id*='player'] tbody tr[data-ri]",
                    "table[id*='player'] tbody tr",
                    ".player-row",
                    "[class*='player-row']",
                    "form table tbody tr",
                ]

                for row_selector in row_selectors:
                    player_rows = driver.find_elements(By.CSS_SELECTOR, row_selector)
                    if len(player_rows) > 1:  # Need at least 2 rows (primary + guests)
                        logger.info(
                            f"BOOKING_DEBUG: Found {len(player_rows)} player rows using: {row_selector}"
                        )
                        break

                if guest_index == 0:
                    logger.debug(f"BOOKING_DEBUG: Initial player row count: {len(player_rows)}")
                    if len(player_rows) == 0:
                        # Log page structure for debugging
                        try:
                            tables = driver.find_elements(By.TAG_NAME, "table")
                            logger.error(
                                f"BOOKING_DEBUG: No player rows found. Page has {len(tables)} tables"
                            )
                            for i, table in enumerate(tables[:3]):
                                table_id = table.get_attribute("id") or "no-id"
                                table_class = table.get_attribute("class") or "no-class"
                                logger.info(
                                    f"BOOKING_DEBUG: Table {i}: id={table_id}, class={table_class}"
                                )
                        except Exception:
                            pass

                # Check if we have enough rows
                if len(player_rows) <= guest_index + 1:
                    logger.error(
                        f"BOOKING_DEBUG: Not enough player rows for player {player_num}. Have {len(player_rows)} rows, need at least {guest_index + 2}"
                    )
                    break

                row = player_rows[guest_index + 1]  # Skip first row (primary player)

                try:
                    # Look for the TBD button in this row using multiple strategies
                    tbd_button = None

                    # Strategy 1: CSS selectors for TBD button/link
                    tbd_selectors = [
                        "a[id*='tbd']",
                        "span[id*='tbd']",
                        "button[id*='tbd']",
                        "[class*='btn-tbd']",
                        "a[class*='tbd']",
                        "span[class*='tbd']",
                        "button[class*='tbd']",
                        "a[id*='TBD']",
                        "span[id*='TBD']",
                        "button[id*='TBD']",
                        "[class*='TBD']",
                        # Common button patterns
                        "a.ui-commandlink",
                        "button.ui-button",
                    ]

                    for selector in tbd_selectors:
                        try:
                            tbd_button = row.find_element(By.CSS_SELECTOR, selector)
                            if tbd_button and tbd_button.is_displayed():
                                logger.info(f"Found TBD button using CSS: {selector}")
                                break
                            tbd_button = None
                        except NoSuchElementException:
                            continue

                    # Strategy 2: XPath text matching for "TBD" text
                    if not tbd_button:
                        try:
                            # Look for any clickable element containing "TBD" text
                            tbd_button = row.find_element(
                                By.XPATH,
                                ".//a[contains(text(), 'TBD')] | "
                                ".//span[contains(text(), 'TBD')] | "
                                ".//button[contains(text(), 'TBD')] | "
                                ".//*[contains(@title, 'TBD')] | "
                                ".//*[contains(@aria-label, 'TBD')]",
                            )
                            if tbd_button and tbd_button.is_displayed():
                                logger.info("Found TBD button using XPath text match")
                        except NoSuchElementException:
                            pass

                    # Strategy 3: Look for any link/button that might be the TBD action
                    if not tbd_button:
                        try:
                            # Find all clickable elements in the row
                            clickables = row.find_elements(
                                By.CSS_SELECTOR, "a, button, span[onclick], div[onclick]"
                            )
                            for elem in clickables:
                                elem_text = elem.text.strip().lower()
                                elem_id = (elem.get_attribute("id") or "").lower()
                                elem_class = (elem.get_attribute("class") or "").lower()
                                if (
                                    "tbd" in elem_text
                                    or "tbd" in elem_id
                                    or "tbd" in elem_class
                                    or "guest" in elem_text
                                ):
                                    if elem.is_displayed():
                                        tbd_button = elem
                                        logger.info(
                                            f"Found TBD button via clickable scan: "
                                            f"text='{elem_text}', id='{elem_id}'"
                                        )
                                        break
                        except Exception as e:
                            logger.debug(f"Clickable scan failed: {e}")

                    if tbd_button:
                        # Click the TBD button
                        driver.execute_script("arguments[0].click();", tbd_button)
                        logger.info(f"Clicked TBD button for player {player_num}")
                        tbd_buttons_added += 1
                        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                    else:
                        # If no TBD button, try to find the player name input and type "TBD"
                        player_input = None
                        input_selectors = [
                            "input[id*='player_input']",
                            "input[id*='player']",
                            "input[name*='player']",
                            "input[type='text']",
                            "input.ui-autocomplete-input",
                        ]

                        for input_selector in input_selectors:
                            try:
                                player_input = row.find_element(By.CSS_SELECTOR, input_selector)
                                if player_input and player_input.is_displayed():
                                    break
                                player_input = None
                            except NoSuchElementException:
                                continue

                        if player_input and not player_input.get_attribute("disabled"):
                            player_input.clear()
                            player_input.send_keys("TBD Registered Guest")
                            logger.info(f"Entered TBD Registered Guest for player {player_num}")
                            tbd_buttons_added += 1
                            self.wait_strategy.wait_after_action(driver, fixed_duration=0.5)
                        else:
                            logger.warning(
                                f"BOOKING_DEBUG: Could not find TBD button or input for player {player_num}"
                            )
                            # Log detailed element state for debugging
                            self._log_row_element_state(driver, row, player_num)

                except Exception as e:
                    logger.warning(
                        f"BOOKING_DEBUG: Error adding TBD guest for player {player_num}: {e}"
                    )

            if tbd_buttons_added == num_tbd_guests:
                logger.info(f"Successfully added {tbd_buttons_added} TBD Registered Guests")
                return True
            else:
                logger.error(
                    f"BOOKING_DEBUG: Failed to add all TBD guests. "
                    f"Added {tbd_buttons_added} of {num_tbd_guests} required. "
                    f"This will cause the booking to fail."
                )
                return False

        except Exception as e:
            logger.error(f"Error adding TBD Registered Guests: {e}")
            return False

    def _find_and_book_time_slot_sync(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        times_to_exclude: set[time] | None = None,
        tee_time_interval_minutes: int = 8,
        skip_scroll: bool = False,
        use_fast_js: bool = False,
        prelocated_slot: dict[str, Any] | None = None,
    ) -> BookingResult:
        """
        Find an available time slot and book it.

        First scrolls through the datascroller to load all relevant time slots,
        then searches for the requested time within the fallback window.

        Uses _find_empty_slots for all bookings (both single and multi-player)
        to ensure both completely empty slots (with Reserve button) and partially
        filled slots (with Available spans) are found.

        When times_to_exclude is provided (typically during batch booking), the
        method will avoid selecting those times as fallback slots to prevent
        conflicts with other bookings in the batch.

        When use_fast_js is True, uses a single JavaScript execution to find and
        click the target slot, reducing slot finding from ~17s to ~100ms.

        When prelocated_slot is provided (a dict returned by _find_target_slot_js),
        the fast-JS path will reuse it instead of re-scanning the DOM, unless
        the slot's time is in times_to_exclude, in which case it falls back to
        a fresh _find_target_slot_js call.

        Args:
            driver: The WebDriver instance
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: Window to search for alternatives
            times_to_exclude: Optional set of times to avoid when selecting fallback slots.
                             Used during batch booking to prevent conflicts.
            tee_time_interval_minutes: Spacing between tee times (e.g., 8 for Northgate, 10 for Walden).
                             Fallback times must be multiples of this interval from the requested time.
            use_fast_js: If True, use JavaScript-based slot finding and clicking for speed.
            prelocated_slot: Optional pre-computed slot dict from _find_target_slot_js.
                            Used to skip DOM re-scanning when the slot was located before
                            the booking window opened.

        Returns:
            BookingResult with booking outcome
        """
        if times_to_exclude is None:
            times_to_exclude = set()
        target_minutes = target_time.hour * 60 + target_time.minute

        if not skip_scroll:
            self._scroll_to_load_all_slots(driver, target_time, fallback_window_minutes)

        # === FAST PATH: JavaScript-based slot finding and clicking ===
        if use_fast_js:
            # Use prelocated slot if available and its time is not excluded
            slot_info = None
            if prelocated_slot is not None:
                prelocated_time = time(prelocated_slot["hours"], prelocated_slot["minutes"])
                if prelocated_time not in times_to_exclude:
                    slot_info = prelocated_slot
                    logger.info(
                        f"FAST_JS: Using prelocated slot at {prelocated_time.strftime('%I:%M %p')} "
                        f"(index={prelocated_slot['index']})"
                    )
                else:
                    logger.info(
                        f"FAST_JS: Prelocated slot at {prelocated_time.strftime('%I:%M %p')} "
                        f"is now excluded, re-scanning DOM"
                    )

            if slot_info is None:
                slot_info = self._find_target_slot_js(
                    driver,
                    target_time,
                    num_players,
                    fallback_window_minutes,
                    tee_time_interval_minutes,
                    times_to_exclude,
                )

            if slot_info is None:
                return BookingResult(
                    success=False,
                    course_name=self.NORTHGATE_COURSE_NAME,
                    error_message=(
                        f"No time slots with {num_players} available spots within "
                        f"{fallback_window_minutes} minutes of {target_time.strftime('%I:%M %p')}"
                    ),
                )

            booked_time = time(slot_info["hours"], slot_info["minutes"])
            is_exact = slot_info["isExact"]

            if not self._click_slot_by_index_js(driver, slot_info["index"]):
                return BookingResult(
                    success=False,
                    error_message="Failed to click Reserve button via JavaScript",
                    booked_time=booked_time,
                )

            fallback_reason = None
            if not is_exact:
                fallback_reason = (
                    f"Exact time {target_time.strftime('%I:%M %p')} not available, "
                    f"using {booked_time.strftime('%I:%M %p')}"
                )

            result = self._complete_booking_sync(
                driver,
                None,
                booked_time,
                num_players,
                fallback_reason,
                already_clicked=True,
            )
            result.course_name = self.NORTHGATE_COURSE_NAME
            return result

        # === EXISTING SLOW PATH (Python-based Selenium iteration) ===

        northgate_section = None
        try:
            sections = driver.find_elements(By.CSS_SELECTOR, ".course-section, [class*='course']")
            for section in sections:
                if self.NORTHGATE_COURSE_NAME.lower() in section.text.lower():
                    northgate_section = section
                    logger.info("BOOKING_DEBUG: Found Northgate course section for slot search")
                    break
        except NoSuchElementException:
            pass

        search_context: Any
        if northgate_section:
            search_context = northgate_section
        else:
            logger.warning(
                "BOOKING_DEBUG: Could not find dedicated Northgate section. "
                "Will search entire page and filter slots by course name."
            )
            search_context = driver

        slots_with_capacity = self._find_empty_slots(
            search_context, min_available_spots=num_players
        )

        if not slots_with_capacity:
            # Extract event blocks that may be causing the lack of availability
            event_blocks = self._extract_event_blocks(
                search_context, target_time, fallback_window_minutes
            )

            error_message = (
                f"No time slots with {num_players} available spots found on this date. "
                f"All slots are either fully booked or have fewer than {num_players} spots available."
            )

            event_message = self._format_event_block_message(event_blocks)
            if event_message:
                error_message = (
                    f"No time slots with {num_players} available spots found. {event_message}"
                )

            return BookingResult(
                success=False,
                error_message=error_message,
            )

        min_time_minutes = max(0, target_minutes - fallback_window_minutes)
        max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)

        eligible_slots: list[tuple[time, Any]] = []
        for slot_time, slot_element in slots_with_capacity:
            slot_minutes = slot_time.hour * 60 + slot_time.minute
            diff = abs(slot_minutes - target_minutes)
            if diff > fallback_window_minutes:
                continue
            if diff % tee_time_interval_minutes != 0:
                continue
            eligible_slots.append((slot_time, slot_element))

        if eligible_slots:
            walden_course_name = "walden on lake conroe"
            course_filtered_slots: list[tuple[time, Any]] = []
            filtered_out_count = 0

            # Even when we find a "Northgate" section, the DOM may still contain
            # Walden slots. For safety, always reject slots that look like Walden.
            # If the Northgate section is present, we use a non-strict filter that
            # only rejects slots with explicit Walden indicators.
            strict_course_check = northgate_section is None

            for slot_time, slot_element in eligible_slots:
                is_northgate = False
                try:
                    is_northgate = self._is_northgate_slot(
                        slot_element,
                        walden_course_name,
                        strict=strict_course_check,
                    )
                except TypeError:
                    is_northgate = self._is_northgate_slot(slot_element, walden_course_name)

                if is_northgate:
                    course_filtered_slots.append((slot_time, slot_element))
                else:
                    filtered_out_count += 1

            if filtered_out_count:
                logger.info(
                    f"BOOKING_DEBUG: Filtered {filtered_out_count} non-Northgate slots. "
                    f"{len(course_filtered_slots)} Northgate slots remain."
                )
            eligible_slots = course_filtered_slots

        logger.info(
            f"Found {len(slots_with_capacity)} slots with {num_players}+ available spots, "
            f"{len(eligible_slots)} eligible within "
            f"{time(min_time_minutes // 60, min_time_minutes % 60).strftime('%I:%M %p')}-"
            f"{time(max_time_minutes // 60, max_time_minutes % 60).strftime('%I:%M %p')} "
            f"at {tee_time_interval_minutes}-minute intervals"
        )

        all_available_times = [t for t, _ in eligible_slots]
        logger.info(
            f"BOOKING_DEBUG: Available times with {num_players}+ spots: "
            f"{[t.strftime('%I:%M %p') for t in all_available_times[:10]]}"
            f"{'...' if len(all_available_times) > 10 else ''}"
        )

        exact_match = None
        best_slot = None
        best_diff = float("inf")

        # Log excluded times if any
        if times_to_exclude:
            logger.info(
                f"BOOKING_DEBUG: Excluding times from fallback selection: "
                f"{[t.strftime('%I:%M %p') for t in sorted(times_to_exclude)]}"
            )

        for slot_time, slot_element in eligible_slots:
            slot_minutes = slot_time.hour * 60 + slot_time.minute
            diff = abs(slot_minutes - target_minutes)

            if diff == 0:
                exact_match = (slot_time, slot_element)
                logger.info(
                    f"BOOKING_DEBUG: Found exact match for requested time "
                    f"{target_time.strftime('%I:%M %p')}"
                )

            # When selecting fallback slots, skip times that are excluded
            # (e.g., times needed by other bookings in a batch)
            if slot_time in times_to_exclude and diff != 0:
                logger.debug(
                    f"BOOKING_DEBUG: Skipping {slot_time.strftime('%I:%M %p')} - "
                    f"excluded to avoid conflict with another booking"
                )
                continue

            # eligible_slots already enforces fallback window and interval alignment
            if diff < best_diff:
                best_diff = diff
                best_slot = (slot_time, slot_element)

        if exact_match:
            booked_time, reserve_element = exact_match
            logger.info(
                f"Attempting to book exact requested time at "
                f"{booked_time.strftime('%I:%M %p')} for {num_players} players"
            )
            result = self._complete_booking_sync(driver, reserve_element, booked_time, num_players)
            result.course_name = self.NORTHGATE_COURSE_NAME
            return result

        if best_slot:
            booked_time, reserve_element = best_slot
            time_diff_minutes = int(best_diff)
            logger.warning(
                f"BOOKING_DEBUG: Exact requested time {target_time.strftime('%I:%M %p')} "
                f"not available with {num_players} spots. "
                f"Using fallback time {booked_time.strftime('%I:%M %p')} "
                f"({time_diff_minutes} minutes {'earlier' if booked_time < target_time else 'later'})"
            )

            fallback_reason = None
            requested_slot = self._find_slot_by_time(search_context, target_time)
            if requested_slot:
                bookers = self._extract_bookers_from_slot(requested_slot)
                if bookers:
                    booker_names = ", ".join(bookers[:2])
                    if len(bookers) > 2:
                        booker_names += f" and {len(bookers) - 2} others"
                    fallback_reason = f"Tee time {target_time.strftime('%I:%M %p')} was already booked by {booker_names}"
                    logger.info(f"BOOKING_DEBUG: Fallback reason: {fallback_reason}")
                else:
                    fallback_reason = (
                        f"Tee time {target_time.strftime('%I:%M %p')} did not have "
                        f"{num_players} available spots"
                    )
            else:
                fallback_reason = f"Tee time {target_time.strftime('%I:%M %p')} was not available"

            logger.info(
                f"Attempting to book fallback slot at {booked_time.strftime('%I:%M %p')} "
                f"for {num_players} players (requested: {target_time.strftime('%I:%M %p')})"
            )
            result = self._complete_booking_sync(
                driver, reserve_element, booked_time, num_players, fallback_reason
            )
            result.course_name = self.NORTHGATE_COURSE_NAME
            return result
        else:
            all_times = [t.strftime("%I:%M %p") for t, _ in eligible_slots[:5]]

            # Extract event blocks that may be blocking the requested time window
            event_blocks = self._extract_event_blocks(
                search_context, target_time, fallback_window_minutes
            )

            error_message = (
                f"No time slots with {num_players} available spots within "
                f"{fallback_window_minutes} minutes of {target_time.strftime('%I:%M %p')}"
            )

            event_message = self._format_event_block_message(event_blocks)
            if event_message:
                error_message += f". {event_message}"

            return BookingResult(
                success=False,
                course_name=self.NORTHGATE_COURSE_NAME,
                error_message=error_message,
                alternatives=f"Slots with {num_players}+ spots: {', '.join(all_times)}"
                if all_times
                else None,
            )

    def _find_target_slot_js(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        tee_time_interval_minutes: int = 8,
        times_to_exclude: set[time] | None = None,
    ) -> dict[str, Any] | None:
        """
        Find the best available slot using a single JavaScript execution.

        This replaces the Python-based _find_empty_slots + _is_northgate_slot pipeline
        with a single browser-side DOM traversal, reducing slot finding from ~17s to ~100ms.

        The JavaScript iterates all slot items in the browser, checking:
        - Course membership via element ID patterns (teeTimeCourses:0 for Northgate)
        - Time extraction from labels
        - Availability via div.Empty (full slot) or span.custom-free-slot-span count
        - Fallback window, interval alignment, and time exclusions

        Args:
            driver: The WebDriver instance
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: Window to search for alternatives
            tee_time_interval_minutes: Spacing between tee times (default 8 for Northgate)
            times_to_exclude: Times to skip when selecting fallback slots

        Returns:
            Dict with slot info {index, hours, minutes, timeStr, diff, available, isExact}
            or None if no suitable slot found
        """
        if times_to_exclude is None:
            times_to_exclude = set()

        exclude_list = [{"h": t.hour, "m": t.minute} for t in times_to_exclude]

        js_code = """
        var targetHour = arguments[0];
        var targetMinute = arguments[1];
        var minPlayers = arguments[2];
        var fallbackMinutes = arguments[3];
        var intervalMinutes = arguments[4];
        var excludeTimes = arguments[5];
        var northgateIndex = arguments[6];
        var maxPlayers = arguments[7];

        var targetMinutes = targetHour * 60 + targetMinute;
        var items = document.querySelectorAll('li.ui-datascroller-item');
        var bestSlot = null;
        var exactMatch = null;
        var bestDiff = Infinity;

        // Build exclude set for O(1) lookup
        var excludeSet = {};
        for (var e = 0; e < excludeTimes.length; e++) {
            excludeSet[excludeTimes[e].h * 60 + excludeTimes[e].m] = true;
        }

        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var itemHtml = item.innerHTML;

            // Check course via element ID pattern: teeTimeCourses:X
            // Northgate uses index "0", Walden uses index "1"
            var courseMatch = itemHtml.match(/teeTimeCourses:(\\d+)/);
            if (courseMatch && courseMatch[1] !== northgateIndex) {
                continue; // Skip non-Northgate slots
            }

            // Extract time from label or text content
            var label = item.querySelector('label');
            var timeText = label ? label.textContent.trim() : '';
            if (!timeText) {
                var allText = item.textContent;
                var timeMatch = allText.match(/(\\d{1,2}):(\\d{2})\\s*([AaPp][Mm])/);
                if (timeMatch) {
                    timeText = timeMatch[0];
                }
            }
            if (!timeText) continue;

            // Parse time
            var tmatch = timeText.match(/(\\d{1,2}):(\\d{2})\\s*([AaPp][Mm])/i);
            if (!tmatch) continue;
            var h = parseInt(tmatch[1]);
            var m = parseInt(tmatch[2]);
            var ampm = tmatch[3].toUpperCase();
            if (ampm === 'PM' && h !== 12) h += 12;
            if (ampm === 'AM' && h === 12) h = 0;

            var slotMinutes = h * 60 + m;
            var diff = Math.abs(slotMinutes - targetMinutes);

            // Check fallback window
            if (diff > fallbackMinutes) continue;

            // Check interval alignment
            if (diff % intervalMinutes !== 0) continue;

            // Check availability
            var emptyDivs = item.querySelectorAll('div.Empty');
            var availableSpans = item.querySelectorAll('span.custom-free-slot-span');
            var isAvailable = false;
            var availableCount = 0;

            if (emptyDivs.length > 0) {
                availableCount = maxPlayers;
                isAvailable = (minPlayers <= maxPlayers);
            } else if (availableSpans.length >= minPlayers) {
                availableCount = availableSpans.length;
                isAvailable = true;
            }

            if (!isAvailable) continue;

            var slotInfo = {
                timeStr: h + ':' + (m < 10 ? '0' : '') + m,
                hours: h,
                minutes: m,
                index: i,
                diff: diff,
                available: availableCount,
                isExact: (diff === 0)
            };

            if (diff === 0) {
                exactMatch = slotInfo;
            }

            // For fallback slots, skip excluded times
            if (diff !== 0 && excludeSet[slotMinutes]) continue;

            if (diff < bestDiff) {
                bestDiff = diff;
                bestSlot = slotInfo;
            }
        }

        return exactMatch || bestSlot;
        """

        result = driver.execute_script(
            js_code,
            target_time.hour,
            target_time.minute,
            num_players,
            fallback_window_minutes,
            tee_time_interval_minutes,
            exclude_list,
            self.NORTHGATE_COURSE_INDEX,
            self.MAX_PLAYERS,
        )

        if result:
            logger.info(
                f"BOOKING_DEBUG: JS slot finder found slot at "
                f"{result['hours']:02d}:{result['minutes']:02d} "
                f"(index={result['index']}, exact={result['isExact']}, "
                f"available={result['available']})"
            )
        else:
            logger.warning(
                f"BOOKING_DEBUG: JS slot finder found no suitable Northgate slot "
                f"within {fallback_window_minutes} min of {target_time.strftime('%I:%M %p')} "
                f"for {num_players} players"
            )

        return result

    def _click_slot_by_index_js(self, driver: webdriver.Chrome, slot_index: int) -> bool:
        """
        Click the Reserve button for the slot at the given DOM index.

        Uses a single JavaScript execution to find and click the appropriate
        clickable element (Reserve button, Available span, or slot link) within
        the slot item at the specified index.

        Args:
            driver: The WebDriver instance
            slot_index: Index of the slot in the li.ui-datascroller-item NodeList

        Returns:
            True if the click was performed, False if the element was not found
        """
        js_click = """
        var items = document.querySelectorAll('li.ui-datascroller-item');
        var item = items[arguments[0]];
        if (!item) return false;

        // Find the clickable element in priority order
        var btn = item.querySelector("a[id*='reserve_button']");
        if (!btn) {
            var spans = item.querySelectorAll('span.custom-free-slot-span');
            btn = spans.length > 0 ? spans[0] : null;
        }
        if (!btn) btn = item.querySelector("a.slot-link");
        if (!btn) return false;

        btn.scrollIntoView({block: 'center'});
        btn.click();
        return true;
        """
        result = driver.execute_script(js_click, slot_index)
        if result:
            logger.info(f"BOOKING_DEBUG: JS clicked Reserve at slot index {slot_index}")
        else:
            logger.warning(f"BOOKING_DEBUG: JS failed to click Reserve at slot index {slot_index}")
        return bool(result)

    def _precision_wait_until(self, execute_at: datetime) -> None:
        """
        Wait until the exact execute_at time with millisecond precision.

        Uses coarse sleep for most of the wait, then a tight busy-wait loop
        for the final 200ms to hit the target time as precisely as possible.

        Args:
            execute_at: Naive datetime in CT timezone to wait until
        """
        ct_tz = ZoneInfo(settings.timezone)
        now_ct = datetime.now(ct_tz).replace(tzinfo=None)
        if now_ct >= execute_at:
            logger.warning(
                f"BATCH_BOOKING: Already past execute_at "
                f"{execute_at.strftime('%H:%M:%S')} - proceeding immediately"
            )
            return

        wait_seconds = (execute_at - now_ct).total_seconds()
        logger.info(
            f"BATCH_BOOKING: Precision wait {wait_seconds:.1f}s until "
            f"{execute_at.strftime('%H:%M:%S.%f')}"
        )

        # Coarse sleep until 200ms before target
        if wait_seconds > 0.2:
            time_module.sleep(wait_seconds - 0.2)

        # Precision busy-wait for the final ~200ms
        while datetime.now(ct_tz).replace(tzinfo=None) < execute_at:
            pass  # spin

        logger.info("BATCH_BOOKING: Precision wait complete - GO!")

    def _scroll_to_load_all_slots(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        fallback_window_minutes: int,
        max_time_minutes_override: int | None = None,
    ) -> None:
        """
        Scroll through the datascroller to load all tee time slots.

        The Walden Golf tee sheet uses a PrimeFaces datascroller component that
        lazy-loads rows as the user scrolls. This method scrolls through the
        datascroller to ensure all relevant time slots are loaded before searching.

        The scrolling stops when either:
        1. The last visible time is past target_time + fallback_window, or
        2. No new items appear after multiple scroll attempts

        Args:
            driver: The WebDriver instance
            target_time: The target tee time being searched for
            fallback_window_minutes: The fallback window in minutes
        """
        max_scroll_attempts = 50
        no_change_threshold = 3
        no_change_count = 0
        previous_item_count = 0

        target_minutes = target_time.hour * 60 + target_time.minute
        if max_time_minutes_override is None:
            max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)
        else:
            max_time_minutes = min(24 * 60 - 1, max_time_minutes_override)

        logger.info(
            f"BOOKING_DEBUG: Starting datascroller scroll to load slots up to "
            f"{time(max_time_minutes // 60, max_time_minutes % 60).strftime('%I:%M %p')}"
        )

        for attempt in range(max_scroll_attempts):
            try:
                slot_items = driver.find_elements(By.CSS_SELECTOR, "li.ui-datascroller-item")
                current_item_count = len(slot_items)

                if current_item_count == previous_item_count:
                    no_change_count += 1
                    if no_change_count >= no_change_threshold:
                        logger.info(
                            f"BOOKING_DEBUG: No new items after {no_change_threshold} scrolls. "
                            f"Total items loaded: {current_item_count}"
                        )
                        break
                else:
                    no_change_count = 0
                    previous_item_count = current_item_count

                if slot_items:
                    last_slot = slot_items[-1]
                    last_time = None
                    max_candidates = 10
                    for candidate in reversed(slot_items[-max_candidates:]):
                        last_time = self._extract_time_from_slot_item(candidate)
                        if last_time:
                            break

                    if last_time:
                        last_time_minutes = last_time.hour * 60 + last_time.minute
                        logger.debug(
                            f"BOOKING_DEBUG: Scroll attempt {attempt + 1}: "
                            f"{current_item_count} items, last time: {last_time.strftime('%I:%M %p')}"
                        )

                        if last_time_minutes >= max_time_minutes:
                            logger.info(
                                f"BOOKING_DEBUG: Loaded slots past target window. "
                                f"Last time: {last_time.strftime('%I:%M %p')}, "
                                f"Total items: {current_item_count}"
                            )
                            break

                    driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", last_slot)
                    self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)

                    datascroller = driver.find_elements(
                        By.CSS_SELECTOR, ".ui-datascroller-content, .ui-datascroller-list"
                    )
                    if datascroller:
                        driver.execute_script(
                            "arguments[0].scrollTop = arguments[0].scrollHeight;",
                            datascroller[0],
                        )
                        self.wait_strategy.simple_wait(
                            fixed_duration=0.3, event_driven_duration=0.1
                        )

            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Scroll attempt {attempt + 1} error: {e}")
                self.wait_strategy.simple_wait(fixed_duration=0.2, event_driven_duration=0.1)

        logger.info(
            f"BOOKING_DEBUG: Finished scrolling. Total slot items loaded: {previous_item_count}"
        )

    def _find_empty_slots(
        self, search_context: Any, min_available_spots: int | None = None
    ) -> list[tuple[time, Any]]:
        """
        Find time slots that have at least min_available_spots available.

        The Walden Golf tee sheet has two different slot structures:

        1. Completely empty slots (all MAX_PLAYERS spots available):
           - The slot div has class="Empty"
           - Contains a "reserve_button" element with "Reserve" text
           - Structure: <div class="Empty">...<a id="...reserve_button">Reserve</a>...</div>

        2. Partially filled slots (1 to MAX_PLAYERS-1 spots available):
           - The slot div has class="Reserved"
           - Contains <span class="custom-free-slot-span">Available</span> for each open spot
           - Count the spans to determine available spots

        Args:
            search_context: The WebDriver element to search within
            min_available_spots: Minimum number of available spots required (default MAX_PLAYERS)

        Returns:
            List of (time, clickable_element) tuples for slots with enough spots
        """
        if min_available_spots is None:
            min_available_spots = self.MAX_PLAYERS
        empty_slots: list[tuple[time, Any]] = []
        completely_empty_count = 0
        partial_slots_count = 0

        try:
            # Find all time slot list items
            slot_items = search_context.find_elements(By.CSS_SELECTOR, "li.ui-datascroller-item")

            logger.info(f"Found {len(slot_items)} time slot items")

            for slot_item in slot_items:
                try:
                    # First check for completely empty slots (class="Empty" with reserve_button)
                    # These have all MAX_PLAYERS spots available
                    empty_divs = slot_item.find_elements(By.CSS_SELECTOR, "div.Empty")

                    if empty_divs:
                        # This is a completely empty slot - all MAX_PLAYERS spots available
                        if min_available_spots <= self.MAX_PLAYERS:
                            slot_time = self._extract_time_from_slot_item(slot_item)
                            if slot_time:
                                # Find the reserve button or the Available link
                                reserve_btn = None
                                try:
                                    reserve_btn = slot_item.find_element(
                                        By.CSS_SELECTOR, "a[id*='reserve_button']"
                                    )
                                except NoSuchElementException:
                                    try:
                                        reserve_btn = slot_item.find_element(
                                            By.CSS_SELECTOR, "a.slot-link"
                                        )
                                    except NoSuchElementException:
                                        reserve_btn = slot_item

                                empty_slots.append((slot_time, reserve_btn))
                                completely_empty_count += 1
                                logger.debug(
                                    f"Found completely empty slot at {slot_time.strftime('%I:%M %p')}"
                                )
                        continue

                    # Check for partially filled slots (class="Reserved" with Available spans)
                    available_spans = slot_item.find_elements(
                        By.CSS_SELECTOR, "span.custom-free-slot-span"
                    )
                    num_available = len(available_spans)

                    if num_available >= min_available_spots:
                        # This slot has enough available spots
                        slot_time = self._extract_time_from_slot_item(slot_item)

                        if slot_time:
                            # Get the first Available span as the clickable element
                            clickable = available_spans[0] if available_spans else slot_item
                            empty_slots.append((slot_time, clickable))
                            partial_slots_count += 1
                            logger.debug(
                                f"Found partial slot at {slot_time.strftime('%I:%M %p')} "
                                f"with {num_available} available spots"
                            )

                except Exception as e:
                    logger.debug(f"Could not process slot item: {e}")
                    continue

        except NoSuchElementException:
            logger.debug("No slot items found")

        empty_slots.sort(key=lambda x: x[0])
        logger.info(
            f"Found {completely_empty_count} completely empty slots and "
            f"{partial_slots_count} partial slots with {min_available_spots}+ spots"
        )
        return empty_slots

    def _extract_time_from_slot_item(self, slot_item: Any) -> time | None:
        """
        Extract the time from a time slot list item.

        The time is typically in a <label> element or in the slot's text content.

        Args:
            slot_item: The <li> element containing the time slot

        Returns:
            The parsed time, or None if not found
        """
        try:
            # Try to find a label element with the time
            try:
                time_label = slot_item.find_element(By.TAG_NAME, "label")
                time_text = time_label.text.strip()
                if time_text:
                    parsed = self._parse_time(time_text)
                    if parsed:
                        return parsed
            except NoSuchElementException:
                pass

            # Try to find time in the slot's text content
            slot_text = slot_item.text
            # Look for time pattern like "07:46 AM" or "1:30 PM"
            time_pattern = r"\b(\d{1,2}:\d{2}\s*[AaPp][Mm])\b"
            match = re.search(time_pattern, slot_text)
            if match:
                return self._parse_time(match.group(1))

            # Try to find time in any span or div
            for tag in ["span", "div"]:
                elements = slot_item.find_elements(By.TAG_NAME, tag)
                for elem in elements:
                    text = elem.text.strip()
                    if text:
                        parsed = self._parse_time(text)
                        if parsed:
                            return parsed

        except Exception as e:
            logger.debug(f"Error extracting time from slot item: {e}")

        return None

    def _find_slot_by_time(self, search_context: Any, target_time: time) -> Any | None:
        """
        Find a specific time slot by its time.

        Args:
            search_context: The WebDriver element to search within
            target_time: The time to search for

        Returns:
            The slot item element if found, None otherwise
        """
        try:
            slot_items = search_context.find_elements(By.CSS_SELECTOR, "li.ui-datascroller-item")
            for slot_item in slot_items:
                slot_time = self._extract_time_from_slot_item(slot_item)
                if slot_time and slot_time == target_time:
                    return slot_item
        except Exception as e:
            logger.debug(f"Error finding slot by time: {e}")
        return None

    def _get_course_index_from_element_id(self, element_id: str) -> str | None:
        """
        Extract the course index from an element ID.

        The Walden Golf website uses a consistent naming pattern in element IDs:
        - teeTimeCourses:0 = Northgate
        - teeTimeCourses:1 = Walden on Lake Conroe

        Args:
            element_id: The element's ID attribute

        Returns:
            The course index ("0" or "1") if found, None otherwise.
        """
        match = re.search(r"teeTimeCourses:(\d+)", element_id)
        if match:
            return match.group(1)
        return None

    def _is_northgate_slot(
        self, slot_element: Any, walden_course_name: str, strict: bool = True
    ) -> bool:
        """
        Check if a slot element belongs to the Northgate course.

        This method uses element ID patterns to reliably determine which course
        a slot belongs to. The Walden Golf website uses consistent IDs:
        - teeTimeCourses:0 = Northgate
        - teeTimeCourses:1 = Walden on Lake Conroe

        This approach is more reliable than the previous DOM-walking strategy
        because the course index is embedded directly in element IDs.

        Args:
            slot_element: The slot element (button, link, span, or container) to check
            walden_course_name: The name of the other course (unused, kept for API compatibility)
            strict: If True, return False when course cannot be determined.
                   If False, return True when course cannot be determined.

        Returns:
            True if the slot belongs to Northgate, False otherwise.
        """
        try:
            # Strategy 1: Check the element's own ID for course index
            element_id = slot_element.get_attribute("id") or ""
            course_index = self._get_course_index_from_element_id(element_id)

            if course_index is not None:
                is_northgate = course_index == self.NORTHGATE_COURSE_INDEX
                logger.debug(
                    f"COURSE_CHECK: Element ID '{element_id[:80]}...' -> "
                    f"course index {course_index} -> "
                    f"{'Northgate' if is_northgate else 'Walden'}"
                )
                return is_northgate

            # Strategy 2: Walk up the DOM tree to find a parent with course info
            # This handles elements like <span> that may not have their own ID
            current = slot_element
            for level in range(10):  # Check up to 10 parent levels
                try:
                    parent = current.find_element(By.XPATH, "..")
                    if parent:
                        parent_id = parent.get_attribute("id") or ""
                        course_index = self._get_course_index_from_element_id(parent_id)

                        if course_index is not None:
                            is_northgate = course_index == self.NORTHGATE_COURSE_INDEX
                            logger.debug(
                                f"COURSE_CHECK: Parent ID at level {level} "
                                f"'{parent_id[:80]}...' -> course index {course_index} -> "
                                f"{'Northgate' if is_northgate else 'Walden'}"
                            )
                            return is_northgate

                        current = parent
                except Exception:
                    break

            # Strategy 3: Could not determine course from element IDs
            # This should be rare if the page structure is consistent
            if strict:
                logger.warning(
                    "COURSE_CHECK: Could not determine course from element IDs. "
                    "Rejecting slot for safety (strict mode)."
                )
                return False
            else:
                logger.debug(
                    "COURSE_CHECK: Could not determine course from element IDs. "
                    "Accepting slot (non-strict mode)."
                )
                return True

        except Exception as e:
            logger.warning(
                f"COURSE_CHECK: Error checking slot course: {e} - rejecting slot for safety"
            )
            return False

    def _extract_bookers_from_slot(self, slot_item: Any) -> list[str]:
        """
        Extract the names of people who have booked spots in a time slot.

        The Walden Golf tee sheet shows booked spots with the member's name.
        This method extracts those names to provide context when a requested
        time is not available.

        Args:
            slot_item: The <li> element containing the time slot

        Returns:
            List of booker names found in the slot
        """
        bookers: list[str] = []
        try:
            reserved_divs = slot_item.find_elements(By.CSS_SELECTOR, "div.Reserved")
            for div in reserved_divs:
                div_text = div.text.strip()
                if div_text and "Available" not in div_text:
                    lines = [line.strip() for line in div_text.split("\n") if line.strip()]
                    for line in lines:
                        if line and "Available" not in line and "Reserve" not in line:
                            if re.match(r"^[A-Za-z]", line) and "," in line:
                                bookers.append(line)

            if not bookers:
                slot_text = slot_item.text
                # Match names like "O'Donnell, Deborah", "mcghee, mike", "Garrett, Steve"
                # Handles apostrophes, lowercase names, and multi-part first names
                name_pattern = r"([A-Za-z][A-Za-z']+,\s*[A-Za-z][A-Za-z' ]*)"
                matches = re.findall(name_pattern, slot_text)
                # Filter out non-name matches like "Available" or "Reserve"
                for match in matches:
                    if "Available" not in match and "Reserve" not in match:
                        bookers.append(match)

            if not bookers:
                spans = slot_item.find_elements(By.TAG_NAME, "span")
                for span in spans:
                    span_text = span.text.strip()
                    if span_text and "Available" not in span_text and "Reserve" not in span_text:
                        # Match names with apostrophes and lowercase (e.g., "O'Donnell,", "mcghee,")
                        if re.match(r"^[A-Za-z][A-Za-z']+,", span_text):
                            bookers.append(span_text)

        except Exception as e:
            logger.debug(f"Error extracting bookers from slot: {e}")

        unique_bookers = list(dict.fromkeys(bookers))
        return unique_bookers

    def _format_event_block_message(self, event_blocks: list[str]) -> str | None:
        """
        Format event block names into a human-readable message suffix.

        Args:
            event_blocks: List of event names that are blocking tee times

        Returns:
            Formatted message string, or None if no events
        """
        if not event_blocks:
            return None

        if len(event_blocks) == 1:
            return f"Time blocked by event: {event_blocks[0]}"
        else:
            event_list = ", ".join(event_blocks[:3])
            if len(event_blocks) > 3:
                event_list += f" and {len(event_blocks) - 3} more"
            return f"Times blocked by events: {event_list}"

    def _extract_event_blocks(
        self,
        search_context: Any,
        target_time: time,
        fallback_window_minutes: int,
    ) -> list[str]:
        """
        Extract event/tournament block names that may be blocking tee times.

        The Walden Golf tee sheet displays events and tournaments as blocked time ranges
        with format like "08:26 AM-10:42 AM" followed by an event name such as
        "Northgate SGA 3 Man ABC - 3318".

        This method scans slot items for these blocked time ranges and extracts
        the event names to provide more informative error messages.

        Args:
            search_context: The WebDriver element to search within
            target_time: The target tee time being searched for
            fallback_window_minutes: The fallback window in minutes

        Returns:
            List of event names that overlap with the requested time window
        """
        event_names: list[str] = []
        target_minutes = target_time.hour * 60 + target_time.minute
        min_time_minutes = max(0, target_minutes - fallback_window_minutes)
        max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)

        # Pattern to match time ranges like "08:26 AM-10:42 AM" or "9:00 AM - 11:00 AM"
        time_range_pattern = re.compile(
            r"(\d{1,2}:\d{2}\s*[AaPp][Mm])\s*-\s*(\d{1,2}:\d{2}\s*[AaPp][Mm])"
        )

        try:
            slot_items = search_context.find_elements(By.CSS_SELECTOR, "li.ui-datascroller-item")

            for slot_item in slot_items:
                try:
                    slot_text = slot_item.text.strip()
                    if not slot_text:
                        continue

                    # Check if this is an event block (contains a time range)
                    time_range_match = time_range_pattern.search(slot_text)
                    if not time_range_match:
                        continue

                    # Parse the start and end times
                    start_time_str = time_range_match.group(1).upper()
                    end_time_str = time_range_match.group(2).upper()

                    start_time = None
                    end_time = None
                    for fmt in ["%I:%M %p", "%I:%M%p"]:
                        try:
                            start_time = datetime.strptime(start_time_str.strip(), fmt).time()
                            end_time = datetime.strptime(end_time_str.strip(), fmt).time()
                            break
                        except ValueError:
                            continue

                    if not start_time or not end_time:
                        continue

                    # Check if this event block overlaps with our target window
                    start_minutes = start_time.hour * 60 + start_time.minute
                    end_minutes = end_time.hour * 60 + end_time.minute

                    # Handle events spanning midnight (e.g., 11:00 PM - 1:00 AM)
                    # If end time is before start time, the event spans midnight
                    if end_minutes < start_minutes:
                        # Event spans midnight - it overlaps if:
                        # 1. Target window overlaps with the evening portion (start to midnight)
                        # 2. Target window overlaps with the morning portion (midnight to end)
                        overlaps = (
                            start_minutes <= max_time_minutes  # Evening portion overlaps
                            or end_minutes >= min_time_minutes  # Morning portion overlaps
                        )
                    else:
                        # Normal event (doesn't span midnight)
                        # Event overlaps if: event_start <= window_end AND event_end >= window_start
                        overlaps = (
                            start_minutes <= max_time_minutes and end_minutes >= min_time_minutes
                        )

                    if overlaps:
                        # Extract the event name - it's the text after the time range
                        # Remove the time range from the text to get the event name
                        event_name = slot_text[time_range_match.end() :].strip()

                        # Clean up the event name
                        # Remove leading/trailing whitespace, newlines
                        event_name = " ".join(event_name.split())

                        if event_name and event_name not in event_names:
                            logger.debug(
                                f"Found blocking event: '{event_name}' "
                                f"({start_time_str}-{end_time_str})"
                            )
                            event_names.append(event_name)

                except Exception as e:
                    logger.debug(f"Error processing slot item for event: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error extracting event blocks: {e}")

        if event_names:
            logger.info(
                f"Found {len(event_names)} event(s) blocking times in requested window: "
                f"{event_names}"
            )

        return event_names

    @with_retry(max_attempts=3, backoff_base=0.5)
    def _find_available_slots(self, search_context: Any) -> list[tuple[time, Any]]:
        """
        Find all available time slots in the tee sheet.

        The Northstar Technologies tee sheet uses a div-based layout:
        - Available slots: <span class="custom-free-slot-span">Available</span>
        - Immediate parent: <div class="ui-bar ui-bar-a custom-free-slot-div">
        - Row container: <div class="block-available"> (ancestor level ~6)
        - Time is embedded in the row container's text content (e.g., "07:46 AM")

        Returns:
            List of (time, element) tuples for available slots
        """
        available_slots: list[tuple[time, Any]] = []

        available_spans = search_context.find_elements(
            By.CSS_SELECTOR, "span.custom-free-slot-span"
        )

        if available_spans:
            logger.info(f"Found {len(available_spans)} available slot spans (div-based layout)")
            for span in available_spans:
                try:
                    row_container = self._find_row_container(span)
                    if row_container is None:
                        logger.debug("Could not find row container for slot")
                        continue

                    slot_time = self._extract_time_from_container(row_container)
                    if slot_time:
                        # Find the clickable "Available" link inside the span
                        # The span contains an <a> link with class "custom-free-slot-link"
                        clickable_element = None
                        try:
                            # Look for the Available link inside the span
                            clickable_element = span.find_element(
                                By.CSS_SELECTOR, "a.custom-free-slot-link"
                            )
                        except NoSuchElementException:
                            try:
                                # Fallback: any <a> link inside the span
                                clickable_element = span.find_element(By.TAG_NAME, "a")
                            except NoSuchElementException:
                                # Last resort: use the span itself
                                clickable_element = span

                        available_slots.append((slot_time, clickable_element))
                        logger.debug(f"Found available slot at {slot_time.strftime('%I:%M %p')}")
                    else:
                        logger.debug("Could not extract time from row container")

                except (NoSuchElementException, ValueError) as e:
                    logger.debug(f"Could not parse div-based slot: {e}")
                    continue

        if not available_slots:
            logger.info("No div-based slots found, trying table-based layout fallback")
            try:
                reserve_buttons = search_context.find_elements(
                    By.XPATH,
                    ".//a[contains(text(), 'Reserve')] | .//button[contains(text(), 'Reserve')]",
                )

                for button in reserve_buttons:
                    try:
                        row = button.find_element(By.XPATH, "./ancestor::tr")
                        time_cell = row.find_element(By.CSS_SELECTOR, "td:first-child, .time-cell")
                        time_text = time_cell.text.strip()

                        slot_time = self._parse_time(time_text)
                        if slot_time:
                            available_slots.append((slot_time, button))

                    except (NoSuchElementException, ValueError) as e:
                        logger.debug(f"Could not parse table slot: {e}")
                        continue

            except NoSuchElementException:
                pass

            try:
                available_links = search_context.find_elements(
                    By.XPATH, ".//a[contains(text(), 'Available')]"
                )

                for link in available_links:
                    try:
                        row = link.find_element(By.XPATH, "./ancestor::tr")
                        time_cell = row.find_element(By.CSS_SELECTOR, "td:first-child, .time-cell")
                        time_text = time_cell.text.strip()

                        slot_time = self._parse_time(time_text)
                        if slot_time:
                            available_slots.append((slot_time, link))

                    except (NoSuchElementException, ValueError) as e:
                        logger.debug(f"Could not parse available link: {e}")
                        continue

            except NoSuchElementException:
                pass

        available_slots.sort(key=lambda x: x[0])
        logger.info(f"Total available slots found: {len(available_slots)}")
        return available_slots

    def _find_row_container(self, span: Any) -> Any | None:
        """
        Find the row container element for an available slot span.

        The DOM structure is:
        - span.custom-free-slot-span (level 0)
        - div.ui-bar.ui-bar-a.custom-free-slot-div (level 1, immediate parent)
        - ... intermediate divs ...
        - div.block-available (level ~6, the row container with time info)

        Args:
            span: The span element with class "custom-free-slot-span"

        Returns:
            The row container element, or None if not found
        """
        row_container_selectors = [
            "./ancestor::div[contains(@class, 'block-available')][1]",
            "./ancestor::div[contains(@class, 'ui-grid-a') and contains(@class, 'full-width')][1]",
            "./ancestor::div[contains(@class, 'teetime-row')][1]",
        ]

        for selector in row_container_selectors:
            try:
                container = span.find_element(By.XPATH, selector)
                return container
            except NoSuchElementException:
                continue

        try:
            current = span
            for _ in range(10):
                current = current.find_element(By.XPATH, "./..")
                text_content = current.get_attribute("textContent") or ""

                if re.search(r"\d{1,2}:\d{2}\s*[AP]M", text_content, re.IGNORECASE):
                    return current
        except (NoSuchElementException, Exception):
            pass

        return None

    def _extract_time_from_container(self, container: Any) -> time | None:
        """
        Extract the tee time from a row container element.

        The time may be in a dedicated element or embedded in the container's text.
        Uses textContent for more reliable extraction than element.text.

        Args:
            container: The row container element

        Returns:
            The parsed time, or None if extraction fails
        """
        try:
            time_selectors = [
                ".teetime-player-col-4",
                "[class*='time']",
                ".time-cell",
            ]
            for selector in time_selectors:
                try:
                    time_element = container.find_element(By.CSS_SELECTOR, selector)
                    time_text = time_element.text.strip()
                    if time_text:
                        slot_time = self._parse_time(time_text)
                        if slot_time:
                            return slot_time
                except NoSuchElementException:
                    continue
        except Exception:
            pass

        try:
            text_content = container.get_attribute("textContent") or container.text or ""

            time_match = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", text_content, re.IGNORECASE)
            if time_match:
                slot_time = self._parse_time(time_match.group(1))
                if slot_time:
                    return slot_time

            time_match_24h = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text_content)
            if time_match_24h:
                slot_time = self._parse_time(time_match_24h.group(0))
                if slot_time:
                    return slot_time
        except Exception as e:
            logger.debug(f"Error extracting time from container text: {e}")

        return None

    def _parse_time(self, time_text: str) -> time | None:
        """
        Parse a time string like '07:30 AM' or '12:42 PM' into a time object.

        Handles time range strings (e.g., '08:26 AM-10:42 AM') by returning None
        silently, as these represent tournament blocks or maintenance windows
        that are not bookable slots.
        """
        original_text = time_text
        time_text = time_text.strip().upper()

        if not time_text:
            return None

        # Check for time range patterns (e.g., "08:26 AM-10:42 AM", "09:00 AM-09:00 AM")
        # These are tournament blocks or maintenance windows, not bookable slots
        # Skip them silently without logging a warning
        if "-" in time_text and re.search(
            r"\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M", time_text
        ):
            logger.debug(f"Skipping time range string (tournament/event block): '{original_text}'")
            return None

        formats = ["%I:%M %p", "%I:%M%p", "%H:%M"]

        for fmt in formats:
            try:
                parsed = datetime.strptime(time_text, fmt)
                return parsed.time()
            except ValueError:
                continue

        logger.warning(
            f"Failed to parse time string: '{original_text}' (normalized: '{time_text}')"
        )
        return None

    def _capture_diagnostic_info(self, driver: webdriver.Chrome, context: str) -> None:
        """
        Capture diagnostic information (screenshot and page source) on failure.

        Args:
            driver: The WebDriver instance
            context: Description of what operation failed
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bucket_name = os.getenv("DEBUG_ARTIFACTS_BUCKET")

            if bucket_name:
                screenshot_bytes = driver.get_screenshot_as_png()
                html_bytes = driver.page_source.encode("utf-8", errors="replace")

                try:
                    screenshot_object = f"walden/{context}/{timestamp}/screenshot.png"
                    html_object = f"walden/{context}/{timestamp}/page.html"

                    screenshot_uri = self._upload_bytes_to_gcs(
                        bucket_name=bucket_name,
                        object_name=screenshot_object,
                        content_type="image/png",
                        data=screenshot_bytes,
                    )
                    logger.info(f"Saved debug screenshot to {screenshot_uri}")

                    html_uri = self._upload_bytes_to_gcs(
                        bucket_name=bucket_name,
                        object_name=html_object,
                        content_type="text/html; charset=utf-8",
                        data=html_bytes,
                    )
                    logger.info(f"Saved debug HTML to {html_uri}")

                except Exception as upload_error:
                    screenshot_path = f"/tmp/walden_debug_{context}_{timestamp}.png"
                    html_path = f"/tmp/walden_debug_{context}_{timestamp}.html"

                    driver.save_screenshot(screenshot_path)
                    logger.info(f"Saved debug screenshot to {screenshot_path}")

                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(driver.page_source)
                    logger.info(f"Saved debug HTML to {html_path}")

                    logger.warning(
                        f"Failed to upload diagnostic artifacts to GCS bucket '{bucket_name}': {upload_error}"
                    )
            else:
                screenshot_path = f"/tmp/walden_debug_{context}_{timestamp}.png"
                html_path = f"/tmp/walden_debug_{context}_{timestamp}.html"

                driver.save_screenshot(screenshot_path)
                logger.info(f"Saved debug screenshot to {screenshot_path}")

                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                logger.info(f"Saved debug HTML to {html_path}")
                logger.info("DEBUG_ARTIFACTS_BUCKET not set; remote artifact upload disabled")

        except Exception as e:
            logger.warning(f"Failed to capture diagnostic info: {e}")

    def _upload_bytes_to_gcs(
        self, *, bucket_name: str, object_name: str, content_type: str, data: bytes
    ) -> str:
        """Upload bytes to GCS using ADC and the JSON upload API.

        Returns the gs:// URI for the uploaded object.
        """
        credentials, _ = google.auth.default(  # type: ignore[no-untyped-call]
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
        )
        credentials.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]
        token = credentials.token
        if not token:
            raise RuntimeError("Failed to obtain access token for GCS upload")

        url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o"
        params = {"uploadType": "media", "name": object_name}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, params=params, headers=headers, content=data)
            resp.raise_for_status()

        return f"gs://{bucket_name}/{object_name}"

    def _log_row_element_state(self, driver: webdriver.Chrome, row: Any, player_num: int) -> None:
        """
        Log detailed element state when TBD button detection fails.

        Captures HTML snippet and element attributes to help debug why
        the TBD button couldn't be found.

        Args:
            driver: The WebDriver instance
            row: The player row element that was being processed
            player_num: The player number (2, 3, or 4) for context
        """
        try:
            # Log current page context
            logger.debug(
                f"BOOKING_DEBUG: TBD detection failed for player {player_num}. "
                f"URL: {driver.current_url}, Title: {driver.title}"
            )

            # Log row HTML snippet (truncated to avoid log bloat)
            try:
                row_html = row.get_attribute("outerHTML")
                # Truncate to 2KB to stay within log limits
                if len(row_html) > 2000:
                    row_html = row_html[:2000] + "... [truncated]"
                logger.debug(f"BOOKING_DEBUG: Row HTML for player {player_num}: {row_html}")
            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Could not get row HTML: {e}")

            # Log summary of clickable elements in the row
            try:
                clickables = row.find_elements(
                    By.CSS_SELECTOR, "a, button, span[onclick], input, select"
                )
                element_summary = []
                for i, elem in enumerate(clickables[:10]):  # Limit to first 10
                    try:
                        elem_info = {
                            "tag": elem.tag_name,
                            "id": elem.get_attribute("id") or "",
                            "class": elem.get_attribute("class") or "",
                            "text": (elem.text or "")[:50],
                            "displayed": elem.is_displayed(),
                            "enabled": elem.is_enabled(),
                        }
                        element_summary.append(elem_info)
                    except Exception:
                        continue

                logger.debug(
                    f"BOOKING_DEBUG: Clickable elements in row {player_num}: {element_summary}"
                )
            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Could not enumerate clickables: {e}")

            # Log the player table container if we can find it
            try:
                tables = driver.find_elements(
                    By.CSS_SELECTOR, "[id*='player'], [class*='player'], table"
                )
                for table in tables[:3]:
                    table_id = table.get_attribute("id") or "no-id"
                    table_class = table.get_attribute("class") or "no-class"
                    rows = table.find_elements(By.CSS_SELECTOR, "tr")
                    logger.debug(
                        f"BOOKING_DEBUG: Table context - id='{table_id}', "
                        f"class='{table_class}', row_count={len(rows)}"
                    )
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Error logging row element state: {e}")

    @with_retry(max_attempts=2, backoff_base=1.0)
    def _complete_booking_sync(
        self,
        driver: webdriver.Chrome,
        reserve_element: Any,
        booked_time: time,
        num_players: int,
        fallback_reason: str | None = None,
        already_clicked: bool = False,
    ) -> BookingResult:
        """
        Complete the booking by clicking Reserve, selecting player count, and confirming.

        Args:
            driver: The WebDriver instance
            reserve_element: The Reserve button/link element to click (ignored if already_clicked)
            booked_time: The time being booked
            num_players: Number of players (1-4)
            fallback_reason: Optional reason why a fallback time was used
            already_clicked: If True, the Reserve button was already clicked via JS.
                           Skip the scroll+click and go straight to player count selection.

        Returns:
            BookingResult with booking outcome
        """
        try:
            logger.info(
                f"BOOKING_DEBUG: Starting booking completion for time={booked_time}, "
                f"players={num_players}, already_clicked={already_clicked}"
            )

            wait = WebDriverWait(driver, 10)

            if not already_clicked:
                # Scroll element into view with offset to account for sticky header
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", reserve_element
                )
                self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

                wait.until(expected_conditions.element_to_be_clickable(reserve_element))

                # Use JavaScript click to bypass any overlay issues
                driver.execute_script("arguments[0].click();", reserve_element)
                logger.debug("BOOKING_DEBUG: Clicked Reserve button")

            try:
                wait.until(
                    expected_conditions.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            ".modal, .dialog, [class*='popup'], form[class*='booking'], [class*='confirm']",
                        )
                    )
                )
                logger.debug("BOOKING_DEBUG: Booking dialog/modal appeared")
            except TimeoutException:
                logger.debug("BOOKING_DEBUG: No modal detected, continuing with page")

            logger.debug(f"BOOKING_DEBUG: Selecting player count: {num_players}")
            if not self._select_player_count_sync(driver, num_players):
                logger.error(f"BOOKING_DEBUG: Failed to select {num_players} players")
                self._capture_diagnostic_info(driver, "player_count_selection_failed")
                return BookingResult(
                    success=False,
                    error_message=f"Failed to select {num_players} players",
                    booked_time=booked_time,
                )
            logger.debug("BOOKING_DEBUG: Player count selection successful")

            # If booking for multiple players, add TBD Registered Guests for the additional slots
            if num_players > 1:
                num_tbd_guests = num_players - 1
                logger.debug(f"BOOKING_DEBUG: Adding {num_tbd_guests} TBD Registered Guests")
                if not self._add_tbd_registered_guests_sync(driver, num_tbd_guests):
                    logger.error(f"BOOKING_DEBUG: Failed to add {num_tbd_guests} TBD guests")
                    self._capture_diagnostic_info(driver, "tbd_guest_registration_failed")
                    return BookingResult(
                        success=False,
                        error_message=f"Failed to add {num_tbd_guests} TBD Registered Guests",
                        booked_time=booked_time,
                    )
                logger.debug("BOOKING_DEBUG: TBD guest registration successful")

            try:
                # Wait for the booking form to load
                logger.debug("BOOKING_DEBUG: Looking for Book Now button")
                self.wait_strategy.wait_for_element(
                    driver,
                    (
                        By.CSS_SELECTOR,
                        "a[id*='bookTeeTimeAction'], a:contains('Book Now'), button:contains('Book')",
                    ),
                    fixed_duration=2.0,
                    timeout=10.0,
                )

                # Scroll down to make sure the Book Now button is visible
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                self.wait_strategy.simple_wait(fixed_duration=1.0, event_driven_duration=0.2)

                # Look for "Book Now" link/button - it's an <a> element on Walden Golf
                # Try to find by ID first (most reliable), then by text content
                confirm_button = None
                try:
                    # First try to find by ID (most specific)
                    confirm_button = driver.find_element(
                        By.CSS_SELECTOR, "a[id*='bookTeeTimeAction']"
                    )
                    logger.debug("BOOKING_DEBUG: Found Book Now button by ID")
                except NoSuchElementException:
                    logger.debug("BOOKING_DEBUG: Book Now button not found by ID, trying XPath")
                    # Fallback to XPath with text content
                    confirm_button = wait.until(
                        expected_conditions.element_to_be_clickable(
                            (
                                By.XPATH,
                                "//a[contains(., 'Book Now')] | "
                                "//a[contains(., 'Book')] | "
                                "//button[contains(., 'Confirm')] | "
                                "//button[contains(., 'Submit')] | "
                                "//button[contains(., 'Book')] | "
                                "//input[@type='submit']",
                            )
                        )
                    )

                button_id = confirm_button.get_attribute("id") or "no-id"
                button_text = confirm_button.text[:50] if confirm_button.text else "no-text"
                logger.info(
                    f"BOOKING_DEBUG: Found Book Now button: id='{button_id}', text='{button_text}'"
                )

                # Scroll to the button and use JavaScript click
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", confirm_button
                )
                self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

                current_url = driver.current_url
                driver.execute_script("arguments[0].click();", confirm_button)
                logger.debug("BOOKING_DEBUG: Clicked Book Now button")

                try:
                    wait.until(expected_conditions.url_changes(current_url))
                    logger.info(
                        f"BOOKING_DEBUG: URL changed after clicking Book Now. New URL: {driver.current_url}"
                    )
                except TimeoutException:
                    logger.info(
                        "BOOKING_DEBUG: URL did not change, checking for success indicators"
                    )
                    try:
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (
                                    By.XPATH,
                                    "//*[contains(text(), 'success') or contains(text(), 'confirm') or contains(text(), 'thank')]",
                                )
                            )
                        )
                    except TimeoutException:
                        logger.debug(
                            "BOOKING_DEBUG: No success indicators found after clicking Book Now"
                        )

            except TimeoutException:
                logger.debug("BOOKING_DEBUG: No confirmation dialog found - booking may be direct")

            confirmation_number = self._extract_confirmation_number(driver)
            logger.debug(f"BOOKING_DEBUG: Extracted confirmation number: {confirmation_number}")

            logger.debug("BOOKING_DEBUG: Verifying booking success")
            if self._verify_booking_success(driver):
                logger.debug("BOOKING_DEBUG: Booking verification PASSED")
                return BookingResult(
                    success=True,
                    booked_time=booked_time,
                    confirmation_number=confirmation_number,
                    fallback_reason=fallback_reason,
                )
            else:
                logger.error("BOOKING_DEBUG: Booking verification FAILED")
                self._capture_diagnostic_info(driver, "booking_verification_failed")
                error_details = self._extract_booking_error_message(driver)
                if error_details:
                    logger.error(f"BOOKING_DEBUG: Extracted booking error text: {error_details}")
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Booking may not have completed successfully"
                        f"{': ' + error_details if error_details else ''}"
                    ),
                    booked_time=booked_time,
                )

        except TimeoutException as e:
            logger.error(f"BOOKING_DEBUG: Booking confirmation timeout: {e}")
            self._capture_diagnostic_info(driver, "booking_timeout")
            return BookingResult(
                success=False,
                error_message=f"Booking confirmation timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"BOOKING_DEBUG: Booking click error: {e}")
            self._capture_diagnostic_info(driver, "booking_error")
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )

    def _extract_confirmation_number(self, driver: webdriver.Chrome) -> str | None:
        """Try to extract a confirmation number from the page after booking."""
        try:
            page_text = self._get_visible_page_text(driver)
            page_text_lower = page_text.lower()

            if (
                "confirmation" in page_text_lower
                or "booked" in page_text_lower
                or "reserved" in page_text_lower
            ):
                # Require at least one digit to avoid matching DOM ids/classes (e.g. "DialogDIV")
                patterns = [
                    r"confirmation[:\s#]*([A-Z0-9-]*\d[A-Z0-9-]*)",
                    r"booking[:\s#]*([A-Z0-9-]*\d[A-Z0-9-]*)",
                    r"reference[:\s#]*([A-Z0-9-]*\d[A-Z0-9-]*)",
                ]

                for pattern in patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        return match.group(1)

        except Exception as e:
            logger.debug(f"Could not extract confirmation number: {e}")

        return None

    def _verify_booking_success(self, driver: webdriver.Chrome) -> bool:
        """
        Verify that the booking was successful by checking page content.

        Returns False if verification is ambiguous - we should not assume success
        without positive confirmation.
        """
        try:
            logger.info(
                f"BOOKING_DEBUG: Verifying booking success. Current URL: {driver.current_url}"
            )
            page_text = self._get_visible_page_text(driver).lower()

            success_indicators = [
                "successfully",
                "confirmed",
                "booked",
                "reservation complete",
                "thank you",
                "your tee time",
            ]

            failure_indicators = [
                "error",
                "failed",
                "unavailable",
                "could not",
                "unable to",
                "already booked",
                "no longer available",
            ]

            # Check for failure indicators first
            found_failures = []
            for indicator in failure_indicators:
                if indicator in page_text:
                    found_failures.append(indicator)

            if found_failures:
                logger.error(f"BOOKING_DEBUG: Found failure indicator(s): {found_failures}")
                return False

            # Check for success indicators
            found_successes = []
            for indicator in success_indicators:
                if indicator in page_text:
                    found_successes.append(indicator)

            if found_successes:
                logger.debug(f"BOOKING_DEBUG: Found success indicator(s): {found_successes}")
                return True

            logger.warning(
                f"BOOKING_DEBUG: No clear success or failure indicators found - treating as failure. "
                f"URL: {driver.current_url}"
            )
            return False

        except Exception as e:
            logger.error(f"BOOKING_DEBUG: Error verifying booking: {e}")
            return False

    async def get_available_times(self, target_date: date) -> list[time]:
        """
        Get all available tee times for a given date.

        This method runs the entire workflow in a background thread:
        1. Creates a new WebDriver instance
        2. Logs in to the member portal
        3. Navigates to the tee time page
        4. Retrieves available time slots
        5. Closes the WebDriver

        Args:
            target_date: The date to check availability for

        Returns:
            List of available times
        """
        return await asyncio.to_thread(self._get_available_times_sync, target_date)

    def _get_available_times_sync(self, target_date: date) -> list[time]:
        """Synchronous implementation with full driver lifecycle."""
        driver = self._create_driver()
        try:
            if not self._perform_login(driver):
                return []

            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))

            self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME)
            if not self._select_date_sync(driver, target_date):
                logger.error(f"Failed to select date {target_date} for availability check")
                return []

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form",
                    )
                )
            )

            available_slots = self._find_available_slots(driver)
            return [slot_time for slot_time, _ in available_slots]

        except WebDriverException as e:
            logger.error(f"Error getting available times: {e}")
            return []
        finally:
            driver.quit()

    async def cancel_booking(self, confirmation_number: str) -> bool:
        """
        Cancel an existing booking on the Walden Golf website.

        This method navigates to the member home page where reservations are displayed,
        finds the reservation matching the confirmation number (which contains date/time info),
        and clicks the cancel button.

        The confirmation_number format is expected to be: "YYYY-MM-DD_HH:MM" (e.g., "2025-12-19_09:46")
        This allows us to identify the correct reservation by date and time.

        Args:
            confirmation_number: The booking identifier in format "YYYY-MM-DD_HH:MM"

        Returns:
            True if cancellation was successful, False otherwise
        """
        return await asyncio.to_thread(self._cancel_booking_sync, confirmation_number)

    def _cancel_booking_sync(self, confirmation_number: str) -> bool:
        """
        Synchronous cancellation implementation with full driver lifecycle.

        Creates driver, performs cancellation, and ensures cleanup in finally block.
        Includes retry logic for transient failures (slow page loads, missed clicks).
        """
        max_retries = 3
        retry_delay = 2

        driver = self._create_driver()
        try:
            if not self._perform_login(driver):
                logger.error("Failed to log in for cancellation")
                return False

            logger.info(f"Attempting to cancel booking: {confirmation_number}")

            for attempt in range(max_retries):
                try:
                    logger.info(
                        f"Navigating to member home page for reservations "
                        f"(attempt {attempt + 1}/{max_retries})..."
                    )
                    driver.get(self.DASHBOARD_URL)

                    wait = WebDriverWait(driver, 15)
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (By.CSS_SELECTOR, "form, .reservations, [class*='reservation']")
                        )
                    )

                    self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)

                    result = self._find_and_cancel_reservation_sync(driver, confirmation_number)
                    if result:
                        return True

                    # If we didn't find/cancel the reservation, it might be a timing issue
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Cancellation attempt {attempt + 1} failed, "
                            f"retrying in {retry_delay} seconds..."
                        )
                        time_module.sleep(retry_delay)
                        driver.refresh()
                        continue

                    return False

                except TimeoutException as e:
                    logger.warning(f"Cancellation timeout on attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        time_module.sleep(retry_delay)
                        continue
                    logger.error(f"Cancellation failed after {max_retries} attempts")
                    return False

            return False

        except WebDriverException as e:
            logger.error(f"Cancellation WebDriver error: {e}")
            return False
        finally:
            driver.quit()

    def _find_and_cancel_reservation_sync(
        self, driver: webdriver.Chrome, confirmation_number: str
    ) -> bool:
        """
        Find and cancel a specific reservation on the member home page.

        The confirmation_number is expected to be in format "YYYY-MM-DD_HH:MM".
        We parse this to match against the reservation date and time displayed on the page.

        Args:
            driver: The WebDriver instance
            confirmation_number: The booking identifier in format "YYYY-MM-DD_HH:MM"

        Returns:
            True if cancellation was successful, False otherwise
        """
        try:
            target_date_str, target_time_str = confirmation_number.split("_")
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
            target_time = datetime.strptime(target_time_str, "%H:%M").time()

            display_date = target_date.strftime("%m/%d/%Y")
            display_time_12h = target_time.strftime("%I:%M %p").lstrip("0")

            logger.info(f"Looking for reservation on {display_date} at {display_time_12h}")
        except (ValueError, AttributeError) as e:
            logger.error(f"Invalid confirmation number format: {confirmation_number}. Error: {e}")
            return False

        try:
            reservations_form = None
            try:
                reservations_form = driver.find_element(
                    By.CSS_SELECTOR, "form[name*='memberReservations']"
                )
                logger.info("Found reservations form, scoping search to it")
            except NoSuchElementException:
                logger.warning("Reservations form not found, searching entire page")

            if reservations_form:
                reservation_rows = reservations_form.find_elements(
                    By.CSS_SELECTOR, "table tbody tr"
                )
            else:
                reservation_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

            logger.info(f"Found {len(reservation_rows)} potential reservation rows")

            for row in reservation_rows:
                try:
                    row_text = row.text.lower()

                    if "tee time" not in row_text:
                        continue

                    date_match = False
                    time_match = False

                    if display_date in row.text:
                        date_match = True
                    else:
                        alt_date = target_date.strftime("%m/%d/%y")
                        if alt_date in row.text:
                            date_match = True

                    time_variations = [
                        display_time_12h,
                        target_time.strftime("%H:%M"),
                        target_time.strftime("%I:%M%p").lstrip("0"),
                        target_time.strftime("%I:%M %p"),
                    ]
                    for time_var in time_variations:
                        if time_var.lower() in row_text or time_var in row.text:
                            time_match = True
                            break

                    if date_match and time_match:
                        logger.info(f"Found matching reservation row: {row.text[:100]}...")

                        cancel_link = None
                        try:
                            cancel_link = row.find_element(
                                By.CSS_SELECTOR,
                                "a[aria-label='Cancel Reservation'], "
                                "a[title='Cancel Reservation'], "
                                "a[class*='cancel'], "
                                "button[class*='cancel']",
                            )
                        except NoSuchElementException:
                            cancel_links = row.find_elements(By.TAG_NAME, "a")
                            for link in cancel_links:
                                aria_label = link.get_attribute("aria-label")
                                if aria_label and "cancel" in aria_label.lower():
                                    cancel_link = link
                                    break
                                title = link.get_attribute("title")
                                if title and "cancel" in title.lower():
                                    cancel_link = link
                                    break

                        if cancel_link:
                            logger.info("Clicking cancel button...")
                            cancel_link.click()

                            return self._confirm_cancellation_sync(
                                driver, display_date, display_time_12h
                            )
                        else:
                            logger.warning("Cancel link not found in matching row")

                except StaleElementReferenceException:
                    continue

            logger.warning(f"No matching reservation found for {confirmation_number}")
            return False

        except Exception as e:
            logger.error(f"Error finding reservation: {e}")
            return False

    def _confirm_cancellation_sync(
        self,
        driver: webdriver.Chrome,
        target_date: str | None = None,
        target_time: str | None = None,
    ) -> bool:
        """
        Handle any confirmation dialog that appears after clicking cancel.

        Args:
            driver: The WebDriver instance
            target_date: The date of the reservation being cancelled (for verification)
            target_time: The time of the reservation being cancelled (for verification)

        Returns:
            True if cancellation was confirmed successfully, False otherwise
        """
        try:
            self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

            try:
                alert = driver.switch_to.alert
                logger.info(f"Alert detected: {alert.text}")
                alert.accept()
                logger.info("Alert accepted")
                self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                return self._verify_cancellation_success(driver, target_date, target_time)
            except Exception:
                pass

            css_selectors = [
                "button[class*='confirm']",
                "button[class*='yes']",
                "input[type='submit'][value*='Yes']",
                "input[type='submit'][value*='Confirm']",
                ".modal button[class*='primary']",
            ]

            for selector in css_selectors:
                try:
                    confirm_btn = driver.find_element(By.CSS_SELECTOR, selector)
                    if confirm_btn.is_displayed():
                        logger.info(f"Found confirm button with CSS selector: {selector}")
                        confirm_btn.click()
                        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                        return self._verify_cancellation_success(driver, target_date, target_time)
                except NoSuchElementException:
                    continue

            xpath_selectors = [
                "//button[contains(text(), 'Yes')]",
                "//button[contains(text(), 'Confirm')]",
                "//button[contains(text(), 'OK')]",
                "//a[contains(text(), 'Yes')]",
                "//a[contains(text(), 'Confirm')]",
                "//*[contains(@class, 'ui-dialog')]//button[contains(text(), 'Yes')]",
            ]

            for xpath in xpath_selectors:
                try:
                    confirm_btn = driver.find_element(By.XPATH, xpath)
                    if confirm_btn.is_displayed():
                        logger.info(f"Found confirm button with XPath: {xpath}")
                        confirm_btn.click()
                        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                        return self._verify_cancellation_success(driver, target_date, target_time)
                except NoSuchElementException:
                    continue

            self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)

            return self._verify_cancellation_success(driver, target_date, target_time)

        except Exception as e:
            logger.error(f"Error confirming cancellation: {e}")
            return False

    def _verify_cancellation_success(
        self,
        driver: webdriver.Chrome,
        target_date: str | None = None,
        target_time: str | None = None,
    ) -> bool:
        """
        Verify that the cancellation was successful by checking page content.

        This method uses multiple verification strategies:
        1. Look for explicit success/failure messages within the reservations form
        2. If target_date and target_time are provided, verify the reservation row is gone
        3. Default to False if no positive confirmation is found (fail-safe)

        Args:
            driver: The WebDriver instance
            target_date: The date of the cancelled reservation (for row verification)
            target_time: The time of the cancelled reservation (for row verification)

        Returns:
            True if cancellation is confirmed successful, False otherwise
        """
        # First, try to find the reservations form to scope our search
        reservations_text = ""
        try:
            reservations_form = driver.find_element(
                By.CSS_SELECTOR, "form[name*='memberReservations']"
            )
            reservations_text = reservations_form.text.lower()
            logger.info("Scoped verification to reservations form")
        except NoSuchElementException:
            # Fall back to page source but log a warning
            logger.warning("Reservations form not found, using full page for verification")
            reservations_text = driver.page_source.lower()

        # Check for explicit success messages (scoped to reservations area)
        success_indicators = [
            "cancelled successfully",
            "canceled successfully",
            "reservation cancelled",
            "reservation canceled",
            "successfully cancelled",
            "successfully canceled",
        ]

        # Check for explicit failure messages
        failure_indicators = [
            "error cancelling",
            "error canceling",
            "failed to cancel",
            "unable to cancel",
            "cannot cancel",
            "cancellation failed",
        ]

        # Check for failure indicators first
        for indicator in failure_indicators:
            if indicator in reservations_text:
                logger.warning(f"Cancellation failed - found '{indicator}' in reservations area")
                return False

        # Check for success indicators
        for indicator in success_indicators:
            if indicator in reservations_text:
                logger.info(f"Cancellation confirmed - found '{indicator}' in reservations area")
                return True

        # If we have target date/time, verify the reservation row is gone
        if target_date and target_time:
            try:
                reservations_form = driver.find_element(
                    By.CSS_SELECTOR, "form[name*='memberReservations']"
                )
                rows = reservations_form.find_elements(By.CSS_SELECTOR, "table tbody tr")

                for row in rows:
                    row_text = row.text.lower()
                    if "tee time" in row_text:
                        # Check if this row matches our cancelled reservation
                        if target_date.lower() in row_text and target_time.lower() in row_text:
                            logger.warning(
                                f"Reservation row still present for {target_date} {target_time}"
                            )
                            return False

                # Row not found - reservation was removed
                logger.info(
                    f"Reservation row for {target_date} {target_time} no longer present - "
                    "cancellation confirmed"
                )
                return True

            except NoSuchElementException:
                logger.warning("Could not verify reservation removal - form not found")

        # No positive confirmation found - fail-safe: return False
        logger.warning(
            "No explicit success confirmation found and could not verify row removal - "
            "treating as failed"
        )
        return False

    async def close(self) -> None:
        """
        Close any resources.

        Note: With the refactored design, each operation manages its own WebDriver
        lifecycle, so there is nothing to clean up here. This method is kept for
        interface compatibility.
        """
        pass


class MockWaldenProvider(ReservationProvider):
    """Mock provider for testing without hitting the real booking system."""

    def __init__(self) -> None:
        pass

    async def login(self) -> bool:
        return True

    async def book_tee_time(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int = 32,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        await asyncio.sleep(0.5)

        return BookingResult(
            success=True,
            booked_time=target_time,
            confirmation_number=f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        )

    async def book_multiple_tee_times(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None = None,
    ) -> BatchBookingResult:
        results: list[BatchBookingItemResult] = []
        total_succeeded = 0

        for req in requests:
            await asyncio.sleep(0.1)
            result = BookingResult(
                success=True,
                booked_time=req.target_time,
                confirmation_number=f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}-{req.booking_id[:8]}",
            )
            results.append(BatchBookingItemResult(booking_id=req.booking_id, result=result))
            total_succeeded += 1

        return BatchBookingResult(
            results=results,
            total_succeeded=total_succeeded,
            total_failed=0,
        )

    async def get_available_times(self, target_date: date) -> list[time]:
        base_time = datetime.combine(target_date, datetime.min.time().replace(hour=7))
        times = []
        for i in range(20):
            slot_time = (base_time + timedelta(minutes=i * 8)).time()
            times.append(slot_time)
        return times

    async def cancel_booking(self, confirmation_number: str) -> bool:
        return True

    async def close(self) -> None:
        pass
