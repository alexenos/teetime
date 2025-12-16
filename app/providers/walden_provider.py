import asyncio
import functools
import logging
import os
import re
import time as time_module
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, TypeVar

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
from app.providers.base import BookingResult, ReservationProvider

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

    def __init__(self) -> None:
        """
        Initialize the WaldenGolfProvider.

        Validates that required credentials are configured. Logs a warning if
        credentials are missing - operations will fail at login time.
        """
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
        fallback_window_minutes: int = 30,
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
        )

    def _book_tee_time_sync(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
    ) -> BookingResult:
        """
        Synchronous booking implementation with full driver lifecycle.

        Creates driver, performs booking, and ensures cleanup in finally block.
        """
        driver = self._create_driver()
        try:
            if not self._perform_login(driver):
                return BookingResult(
                    success=False,
                    error_message="Failed to log in to Walden Golf",
                )

            logger.info("Navigating to tee time booking page...")
            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))

            self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME)
            self._select_date_sync(driver, target_date)

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form",
                    )
                )
            )

            result = self._find_and_book_time_slot_sync(
                driver, target_time, num_players, fallback_window_minutes
            )

            return result

        except TimeoutException as e:
            logger.error(f"Booking timeout: {e}")
            return BookingResult(
                success=False,
                error_message=f"Booking timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"Booking WebDriver error: {e}")
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )
        finally:
            driver.quit()

    def _select_course_sync(self, driver: webdriver.Chrome, course_name: str) -> None:
        """Select the course from the dropdown."""
        try:
            course_select = driver.find_element(By.CSS_SELECTOR, "select[id*='course']")
            select = Select(course_select)

            for option in select.options:
                if course_name.lower() in option.text.lower():
                    select.select_by_visible_text(option.text)
                    logger.info(f"Selected course: {option.text}")
                    wait = WebDriverWait(driver, 10)
                    try:
                        wait.until(expected_conditions.staleness_of(course_select))
                    except TimeoutException:
                        pass
                    return

            logger.warning(f"Course '{course_name}' not found in dropdown, using default")

        except NoSuchElementException:
            logger.info("No course dropdown found - may already be on correct course page")

    def _select_date_sync(self, driver: webdriver.Chrome, target_date: date) -> None:
        """
        Select the target date using various date selection mechanisms.

        The Northstar Technologies tee sheet may use different date selection methods:
        1. Date input field (various selectors)
        2. Date picker widget
        3. Day-of-week tabs
        4. Calendar navigation

        This method tries multiple approaches in order of likelihood.
        """
        date_str = target_date.strftime("%m/%d/%Y")
        date_str_alt = target_date.strftime("%Y-%m-%d")

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
                logger.info(f"Entered date {date_str} using selector: {selector}")

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
                    logger.info("Clicked search/submit button after date entry")
                except TimeoutException:
                    pass

                return

            except NoSuchElementException:
                continue

        logger.info("No date input found with standard selectors, trying day tabs...")
        if self._select_date_via_tabs_sync(driver, target_date):
            return

        logger.info("Day tabs not found, trying calendar picker...")
        self._select_date_via_calendar_sync(driver, target_date)

    def _select_date_via_calendar_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select date using a calendar picker widget if available.

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
                logger.info("Clicked calendar trigger")

                wait = WebDriverWait(driver, 5)
                try:
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                ".ui-datepicker, .datepicker, [class*='calendar-popup']",
                            )
                        )
                    )

                    day_str = str(target_date.day)
                    day_elements = driver.find_elements(
                        By.XPATH,
                        f"//td[@data-date='{target_date.day}'] | "
                        f"//a[text()='{day_str}'] | "
                        f"//td[contains(@class, 'day') and text()='{day_str}']",
                    )

                    for day_el in day_elements:
                        if day_el.is_displayed() and day_el.is_enabled():
                            day_el.click()
                            logger.info(f"Selected day {day_str} from calendar")
                            # Wait for page to reload after date selection
                            time_module.sleep(2)
                            # Wait for tee time slots to appear
                            try:
                                WebDriverWait(driver, 10).until(
                                    expected_conditions.presence_of_element_located(
                                        (
                                            By.CSS_SELECTOR,
                                            ".custom-free-slot-span, .teetime-row, [class*='tee-time']",
                                        )
                                    )
                                )
                            except TimeoutException:
                                logger.debug("Tee time slots not found after calendar selection")
                            return True

                except TimeoutException:
                    logger.debug("Calendar popup did not appear")

        except Exception as e:
            logger.debug(f"Calendar selection failed: {e}")

        return False

    def _select_date_via_tabs_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select date using the day-of-week tabs if date picker not available.

        Returns:
            True if date was selected successfully, False otherwise.
        """
        day_name = target_date.strftime("%A")
        date_str = target_date.strftime("%m/%d")

        try:
            day_tabs = driver.find_elements(
                By.CSS_SELECTOR,
                ".day-tab, [class*='day-tab'], a[href*='day'], "
                "[data-day], .teetime-day-tab, .nav-tabs a",
            )

            for tab in day_tabs:
                tab_text = tab.text.lower()
                if day_name.lower() in tab_text or date_str in tab.text:
                    wait = WebDriverWait(driver, 10)
                    try:
                        wait.until(expected_conditions.element_to_be_clickable(tab))
                        tab.click()
                        logger.info(f"Clicked day tab: {day_name}")
                        wait.until(expected_conditions.staleness_of(tab))
                    except TimeoutException:
                        tab.click()
                        logger.info(f"Clicked day tab (no staleness wait): {day_name}")
                    return True

            logger.warning(f"Could not find day tab for {day_name}")
            return False

        except NoSuchElementException:
            logger.warning("No day tabs found")
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
            # Wait for the player count button group to appear
            time_module.sleep(1)

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
                    logger.debug(f"Found player button group with selector: {selector}")
                    break
                except NoSuchElementException:
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
                    if "ui-state-disabled" in button_classes:
                        logger.warning(f"Player count {num_players} button is disabled")
                        return False

                    # Click the button
                    driver.execute_script("arguments[0].click();", button_div)
                    logger.info(f"Selected {num_players} players using button group")
                    time_module.sleep(1)  # Wait for the form to update
                    return True
                except NoSuchElementException:
                    logger.warning(f"Could not find radio input for {num_players} players")

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
                    time_module.sleep(0.5)
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
            # Wait for the player table to update after selecting player count
            time_module.sleep(2)

            tbd_buttons_added = 0

            # Process each guest slot one at a time, re-finding rows after each click
            # to avoid stale element references
            for guest_index in range(num_tbd_guests):
                player_num = guest_index + 2  # Players 2, 3, 4

                # Re-find player rows each iteration to avoid stale references
                player_rows = driver.find_elements(
                    By.CSS_SELECTOR, "[id*='playersTable'] tbody tr[data-ri]"
                )

                if guest_index == 0:
                    logger.info(f"Found {len(player_rows)} player rows")

                # Check if we have enough rows
                if len(player_rows) <= guest_index + 1:
                    logger.warning(f"Not enough player rows for player {player_num}")
                    break

                row = player_rows[guest_index + 1]  # Skip first row (primary player)

                try:
                    # Look for the TBD button in this row
                    tbd_button = None
                    tbd_selectors = [
                        "a[id*='tbd']",
                        "span[id*='tbd']",
                        "[class*='btn-tbd']",
                        "a[class*='tbd']",
                        "span[class*='tbd']",
                    ]

                    for selector in tbd_selectors:
                        try:
                            tbd_button = row.find_element(By.CSS_SELECTOR, selector)
                            break
                        except NoSuchElementException:
                            continue

                    if tbd_button:
                        # Click the TBD button
                        driver.execute_script("arguments[0].click();", tbd_button)
                        logger.info(f"Clicked TBD button for player {player_num}")
                        tbd_buttons_added += 1
                        time_module.sleep(1)  # Wait for the form to update
                    else:
                        # If no TBD button, try to find the player name input and type "TBD"
                        try:
                            player_input = row.find_element(
                                By.CSS_SELECTOR, "input[id*='player_input']"
                            )
                            # Check if input is enabled
                            if not player_input.get_attribute("disabled"):
                                player_input.clear()
                                player_input.send_keys("TBD Registered Guest")
                                logger.info(f"Entered TBD Registered Guest for player {player_num}")
                                tbd_buttons_added += 1
                                time_module.sleep(0.5)
                        except NoSuchElementException:
                            logger.warning(
                                f"Could not find TBD button or input for player {player_num}"
                            )

                except Exception as e:
                    logger.warning(f"Error adding TBD guest for player {player_num}: {e}")

            if tbd_buttons_added == num_tbd_guests:
                logger.info(f"Successfully added {tbd_buttons_added} TBD Registered Guests")
                return True
            else:
                logger.warning(f"Only added {tbd_buttons_added} of {num_tbd_guests} TBD guests")
                return tbd_buttons_added > 0

        except Exception as e:
            logger.error(f"Error adding TBD Registered Guests: {e}")
            return False

    def _find_and_book_time_slot_sync(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
    ) -> BookingResult:
        """
        Find an available time slot and book it.

        First tries the exact requested time, then searches within the fallback window
        for the nearest available slot.

        For 4-player bookings, prioritizes completely empty slots (with Reserve button)
        since partially filled slots may not have enough spots.

        Args:
            driver: The WebDriver instance
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: Window to search for alternatives

        Returns:
            BookingResult with booking outcome
        """
        target_minutes = target_time.hour * 60 + target_time.minute

        northgate_section = None
        try:
            sections = driver.find_elements(By.CSS_SELECTOR, ".course-section, [class*='course']")
            for section in sections:
                if self.NORTHGATE_COURSE_NAME.lower() in section.text.lower():
                    northgate_section = section
                    break
        except NoSuchElementException:
            pass

        search_context = northgate_section if northgate_section else driver

        # For multi-player bookings, find slots with enough available spots
        # This ensures we don't book a slot that can't accommodate all players
        if num_players > 1:
            slots_with_capacity = self._find_empty_slots(
                search_context, min_available_spots=num_players
            )
            if slots_with_capacity:
                logger.info(
                    f"Found {len(slots_with_capacity)} slots with {num_players}+ available spots"
                )

                best_slot = None
                best_diff = float("inf")

                for slot_time, slot_element in slots_with_capacity:
                    slot_minutes = slot_time.hour * 60 + slot_time.minute
                    diff = abs(slot_minutes - target_minutes)

                    if diff <= fallback_window_minutes and diff < best_diff:
                        best_diff = diff
                        best_slot = (slot_time, slot_element)

                if best_slot:
                    booked_time, reserve_element = best_slot
                    logger.info(
                        f"Attempting to book slot at {booked_time.strftime('%I:%M %p')} for {num_players} players"
                    )
                    return self._complete_booking_sync(
                        driver, reserve_element, booked_time, num_players
                    )
                else:
                    # No slots with enough capacity within the fallback window
                    # Return error with helpful information
                    all_times = [t.strftime("%I:%M %p") for t, _ in slots_with_capacity[:5]]
                    return BookingResult(
                        success=False,
                        error_message=(
                            f"No time slots with {num_players} available spots within "
                            f"{fallback_window_minutes} minutes of {target_time.strftime('%I:%M %p')}"
                        ),
                        alternatives=f"Slots with {num_players}+ spots: {', '.join(all_times)}"
                        if all_times
                        else None,
                    )
            else:
                # No slots with enough capacity on this date
                return BookingResult(
                    success=False,
                    error_message=(
                        f"No time slots with {num_players} available spots found on this date. "
                        f"All slots are either fully booked or have fewer than {num_players} spots available."
                    ),
                )

        # For single-player bookings, use the standard available slots logic
        available_slots = self._find_available_slots(search_context)

        if not available_slots:
            return BookingResult(
                success=False,
                error_message="No available time slots found for the selected date",
            )

        logger.info(f"Found {len(available_slots)} available slots")

        best_slot = None
        best_diff = float("inf")

        for slot_time, slot_element in available_slots:
            slot_minutes = slot_time.hour * 60 + slot_time.minute
            diff = abs(slot_minutes - target_minutes)

            if diff <= fallback_window_minutes and diff < best_diff:
                best_diff = diff
                best_slot = (slot_time, slot_element)

        if best_slot is None:
            available_times = [t.strftime("%I:%M %p") for t, _ in available_slots[:5]]
            return BookingResult(
                success=False,
                error_message=(
                    f"No available times within {fallback_window_minutes} minutes "
                    f"of {target_time.strftime('%I:%M %p')}"
                ),
                alternatives=f"Available times: {', '.join(available_times)}",
            )

        booked_time, reserve_element = best_slot
        logger.info(
            f"Attempting to book {booked_time.strftime('%I:%M %p')} for {num_players} players"
        )

        return self._complete_booking_sync(driver, reserve_element, booked_time, num_players)

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
        """Parse a time string like '07:30 AM' or '12:42 PM' into a time object."""
        original_text = time_text
        time_text = time_text.strip().upper()

        if not time_text:
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
            screenshot_path = f"/tmp/walden_debug_{context}_{timestamp}.png"
            html_path = f"/tmp/walden_debug_{context}_{timestamp}.html"

            driver.save_screenshot(screenshot_path)
            logger.info(f"Saved debug screenshot to {screenshot_path}")

            with open(html_path, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logger.info(f"Saved debug HTML to {html_path}")

        except Exception as e:
            logger.warning(f"Failed to capture diagnostic info: {e}")

    @with_retry(max_attempts=2, backoff_base=1.0)
    def _complete_booking_sync(
        self,
        driver: webdriver.Chrome,
        reserve_element: Any,
        booked_time: time,
        num_players: int,
    ) -> BookingResult:
        """
        Complete the booking by clicking Reserve, selecting player count, and confirming.

        Args:
            driver: The WebDriver instance
            reserve_element: The Reserve button/link element to click
            booked_time: The time being booked
            num_players: Number of players (1-4)

        Returns:
            BookingResult with booking outcome
        """
        try:
            # Scroll element into view with offset to account for sticky header
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", reserve_element
            )
            time_module.sleep(0.5)  # Wait for scroll to complete

            wait = WebDriverWait(driver, 10)
            wait.until(expected_conditions.element_to_be_clickable(reserve_element))

            # Use JavaScript click to bypass any overlay issues
            driver.execute_script("arguments[0].click();", reserve_element)
            logger.info("Clicked Reserve button")

            try:
                wait.until(
                    expected_conditions.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            ".modal, .dialog, [class*='popup'], form[class*='booking'], [class*='confirm']",
                        )
                    )
                )
            except TimeoutException:
                pass

            if not self._select_player_count_sync(driver, num_players):
                self._capture_diagnostic_info(driver, "player_count_selection_failed")
                return BookingResult(
                    success=False,
                    error_message=f"Failed to select {num_players} players",
                    booked_time=booked_time,
                )

            # If booking for multiple players, add TBD Registered Guests for the additional slots
            if num_players > 1:
                num_tbd_guests = num_players - 1
                logger.info(f"Adding {num_tbd_guests} TBD Registered Guests")
                if not self._add_tbd_registered_guests_sync(driver, num_tbd_guests):
                    self._capture_diagnostic_info(driver, "tbd_guest_registration_failed")
                    return BookingResult(
                        success=False,
                        error_message=f"Failed to add {num_tbd_guests} TBD Registered Guests",
                        booked_time=booked_time,
                    )

            try:
                # Wait for the booking form to load
                time_module.sleep(2)

                # Scroll down to make sure the Book Now button is visible
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time_module.sleep(1)

                # Look for "Book Now" link/button - it's an <a> element on Walden Golf
                # Try to find by ID first (most reliable), then by text content
                confirm_button = None
                try:
                    # First try to find by ID (most specific)
                    confirm_button = driver.find_element(
                        By.CSS_SELECTOR, "a[id*='bookTeeTimeAction']"
                    )
                    logger.info("Found Book Now button by ID")
                except NoSuchElementException:
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

                logger.info(f"Found Book Now button: {confirm_button.get_attribute('id')}")

                # Scroll to the button and use JavaScript click
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", confirm_button
                )
                time_module.sleep(0.5)

                current_url = driver.current_url
                driver.execute_script("arguments[0].click();", confirm_button)
                logger.info("Clicked Book Now button")

                try:
                    wait.until(expected_conditions.url_changes(current_url))
                except TimeoutException:
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (
                                By.XPATH,
                                "//*[contains(text(), 'success') or contains(text(), 'confirm') or contains(text(), 'thank')]",
                            )
                        )
                    )

            except TimeoutException:
                logger.info("No confirmation dialog found - booking may be direct")

            confirmation_number = self._extract_confirmation_number(driver)

            if self._verify_booking_success(driver):
                return BookingResult(
                    success=True,
                    booked_time=booked_time,
                    confirmation_number=confirmation_number,
                )
            else:
                self._capture_diagnostic_info(driver, "booking_verification_failed")
                return BookingResult(
                    success=False,
                    error_message="Booking may not have completed successfully",
                    booked_time=booked_time,
                )

        except TimeoutException as e:
            logger.error(f"Booking confirmation timeout: {e}")
            self._capture_diagnostic_info(driver, "booking_timeout")
            return BookingResult(
                success=False,
                error_message=f"Booking confirmation timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"Booking click error: {e}")
            self._capture_diagnostic_info(driver, "booking_error")
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )

    def _extract_confirmation_number(self, driver: webdriver.Chrome) -> str | None:
        """Try to extract a confirmation number from the page after booking."""
        try:
            page_source = driver.page_source
            page_text_lower = page_source.lower()

            if (
                "confirmation" in page_text_lower
                or "booked" in page_text_lower
                or "reserved" in page_text_lower
            ):
                patterns = [
                    r"confirmation[:\s#]*([A-Z0-9-]+)",
                    r"booking[:\s#]*([A-Z0-9-]+)",
                    r"reference[:\s#]*([A-Z0-9-]+)",
                ]

                for pattern in patterns:
                    match = re.search(pattern, page_source, re.IGNORECASE)
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
            page_text = driver.page_source.lower()

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

            for indicator in failure_indicators:
                if indicator in page_text:
                    logger.warning(f"Found failure indicator: {indicator}")
                    return False

            for indicator in success_indicators:
                if indicator in page_text:
                    logger.info(f"Found success indicator: {indicator}")
                    return True

            logger.warning(
                "No clear success or failure indicators found - treating as failure. " "URL: %s",
                driver.current_url,
            )
            return False

        except Exception as e:
            logger.error(f"Error verifying booking: {e}")
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
            self._select_date_sync(driver, target_date)

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
        """
        driver = self._create_driver()
        try:
            if not self._perform_login(driver):
                logger.error("Failed to log in for cancellation")
                return False

            logger.info(f"Attempting to cancel booking: {confirmation_number}")

            logger.info("Navigating to member home page for reservations...")
            driver.get(self.DASHBOARD_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(
                expected_conditions.presence_of_element_located(
                    (By.CSS_SELECTOR, "form, .reservations, [class*='reservation']")
                )
            )

            time_module.sleep(2)

            return self._find_and_cancel_reservation_sync(driver, confirmation_number)

        except TimeoutException as e:
            logger.error(f"Cancellation timeout: {e}")
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
                                if (
                                    "cancel" in link.get_attribute("aria-label").lower()
                                    if link.get_attribute("aria-label")
                                    else False
                                ):
                                    cancel_link = link
                                    break
                                if (
                                    "cancel" in link.get_attribute("title").lower()
                                    if link.get_attribute("title")
                                    else False
                                ):
                                    cancel_link = link
                                    break

                        if cancel_link:
                            logger.info("Clicking cancel button...")
                            cancel_link.click()

                            return self._confirm_cancellation_sync(driver)
                        else:
                            logger.warning("Cancel link not found in matching row")

                except StaleElementReferenceException:
                    continue

            logger.warning(f"No matching reservation found for {confirmation_number}")
            return False

        except Exception as e:
            logger.error(f"Error finding reservation: {e}")
            return False

    def _confirm_cancellation_sync(self, driver: webdriver.Chrome) -> bool:
        """
        Handle any confirmation dialog that appears after clicking cancel.

        Args:
            driver: The WebDriver instance

        Returns:
            True if cancellation was confirmed successfully, False otherwise
        """
        try:
            time_module.sleep(1)

            try:
                alert = driver.switch_to.alert
                logger.info(f"Alert detected: {alert.text}")
                alert.accept()
                logger.info("Alert accepted")
                time_module.sleep(1)
                return True
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
                        time_module.sleep(1)
                        return self._verify_cancellation_success(driver)
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
                        time_module.sleep(1)
                        return self._verify_cancellation_success(driver)
                except NoSuchElementException:
                    continue

            time_module.sleep(2)

            return self._verify_cancellation_success(driver)

        except Exception as e:
            logger.error(f"Error confirming cancellation: {e}")
            return False

    def _verify_cancellation_success(self, driver: webdriver.Chrome) -> bool:
        """
        Verify that the cancellation was successful by checking page content.

        Args:
            driver: The WebDriver instance

        Returns:
            True if cancellation appears successful, False otherwise
        """
        page_source = driver.page_source.lower()

        success_indicators = [
            "cancelled",
            "canceled",
            "successfully",
            "removed",
            "deleted",
        ]

        failure_indicators = [
            "error",
            "failed",
            "unable",
            "cannot cancel",
        ]

        for indicator in failure_indicators:
            if indicator in page_source:
                logger.warning(f"Cancellation may have failed - found '{indicator}' in page")
                return False

        for indicator in success_indicators:
            if indicator in page_source:
                logger.info(f"Cancellation appears successful - found '{indicator}' in page")
                return True

        logger.info("No explicit success/failure indicators found, assuming cancellation succeeded")
        return True

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
        fallback_window_minutes: int = 30,
    ) -> BookingResult:
        await asyncio.sleep(0.5)

        return BookingResult(
            success=True,
            booked_time=target_time,
            confirmation_number=f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}",
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
