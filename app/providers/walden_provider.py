import asyncio
import functools
import logging
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
                        logger.error(
                            f"All {max_attempts} attempts failed for {func.__name__}: {e}"
                        )
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

    def __init__(self) -> None:
        pass

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
                    (By.CSS_SELECTOR, ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form")
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
                            (By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], button.search, .btn-search")
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
                "span.icon-calendar, i.fa-calendar"
            )

            if calendar_triggers:
                calendar_triggers[0].click()
                logger.info("Clicked calendar trigger")

                wait = WebDriverWait(driver, 5)
                try:
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (By.CSS_SELECTOR, ".ui-datepicker, .datepicker, [class*='calendar-popup']")
                        )
                    )

                    day_str = str(target_date.day)
                    day_elements = driver.find_elements(
                        By.XPATH,
                        f"//td[@data-date='{target_date.day}'] | "
                        f"//a[text()='{day_str}'] | "
                        f"//td[contains(@class, 'day') and text()='{day_str}']"
                    )

                    for day_el in day_elements:
                        if day_el.is_displayed() and day_el.is_enabled():
                            day_el.click()
                            logger.info(f"Selected day {day_str} from calendar")
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
                "[data-day], .teetime-day-tab, .nav-tabs a"
            )

            for tab in day_tabs:
                tab_text = tab.text.lower()
                if day_name.lower() in tab_text or date_str in tab.text:
                    wait = WebDriverWait(driver, 10)
                    try:
                        wait.until(expected_conditions.element_to_be_clickable(tab))
                        tab.click()
                        logger.info(f"Clicked day tab: {day_name}")
                        wait.until(
                            expected_conditions.staleness_of(tab)
                        )
                    except TimeoutException:
                        tab.click()
                        logger.info(f"Clicked day tab (no staleness wait): {day_name}")
                    return True

            logger.warning(f"Could not find day tab for {day_name}")
            return False

        except NoSuchElementException:
            logger.warning("No day tabs found")
            return False


    def _select_player_count_sync(self, driver: webdriver.Chrome, num_players: int) -> None:
        """
        Select the number of players in the booking dialog.

        The player count selector is visible in the tee sheet interface after clicking Reserve.
        This method attempts to find and set the player count dropdown or input.

        Args:
            driver: The WebDriver instance
            num_players: Number of players (1-4)
        """
        try:
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
                    return
                except (NoSuchElementException, Exception):
                    continue

            player_input_selectors = [
                "input[id*='player']",
                "input[id*='golfer']",
                "input[name*='player']",
                "input[name*='golfer']",
            ]

            for selector in player_input_selectors:
                try:
                    player_input = driver.find_element(By.CSS_SELECTOR, selector)
                    player_input.clear()
                    player_input.send_keys(str(num_players))
                    logger.info(f"Entered {num_players} players using input: {selector}")
                    time_module.sleep(0.5)
                    return
                except NoSuchElementException:
                    continue

            logger.warning(
                f"Could not find player count selector - site may auto-fill or use different control. "
                f"Requested {num_players} players."
            )

        except Exception as e:
            logger.warning(f"Error selecting player count: {e}")

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
        logger.info(f"Attempting to book {booked_time.strftime('%I:%M %p')} for {num_players} players")

        return self._complete_booking_sync(driver, reserve_element, booked_time, num_players)


    def _find_available_slots(self, search_context: Any) -> list[tuple[time, Any]]:
        """
        Find all available time slots in the tee sheet.

        The Northstar Technologies tee sheet uses a div-based layout (not tables):
        - Available slots: <span class="custom-free-slot-span">Available</span>
        - Container: <div class="custom-free-slot-div">
        - Time displayed in elements with class "teetime-player-col-4"

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
                    slot_container = span.find_element(By.XPATH, "./ancestor::div[contains(@class, 'custom-free-slot-div')]")

                    row_container = slot_container.find_element(By.XPATH, "./ancestor::div[contains(@class, 'ui-bar') or contains(@class, 'teetime-row')]")

                    time_element = row_container.find_element(
                        By.CSS_SELECTOR, ".teetime-player-col-4, [class*='time'], .time-cell"
                    )
                    time_text = time_element.text.strip()

                    if not time_text:
                        time_elements = row_container.find_elements(By.XPATH, ".//span[contains(@class, 'teetime')]")
                        for te in time_elements:
                            text = te.text.strip()
                            if text and re.match(r"\d{1,2}:\d{2}", text):
                                time_text = text
                                break

                    slot_time = self._parse_time(time_text)
                    if slot_time:
                        available_slots.append((slot_time, span))
                        logger.debug(f"Found available slot at {slot_time.strftime('%I:%M %p')}")

                except (NoSuchElementException, ValueError) as e:
                    logger.debug(f"Could not parse div-based slot: {e}")
                    try:
                        parent = span.find_element(By.XPATH, "./..")
                        all_text = parent.text
                        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM)?)", all_text, re.IGNORECASE)
                        if time_match:
                            slot_time = self._parse_time(time_match.group(1))
                            if slot_time:
                                available_slots.append((slot_time, span))
                                logger.debug(f"Found available slot at {slot_time.strftime('%I:%M %p')} (fallback)")
                    except Exception:
                        pass
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

    def _parse_time(self, time_text: str) -> time | None:
        """Parse a time string like '07:30 AM' or '12:42 PM' into a time object."""
        time_text = time_text.strip().upper()

        formats = ["%I:%M %p", "%I:%M%p", "%H:%M"]

        for fmt in formats:
            try:
                parsed = datetime.strptime(time_text, fmt)
                return parsed.time()
            except ValueError:
                continue

        return None


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
            driver.execute_script("arguments[0].scrollIntoView(true);", reserve_element)

            wait = WebDriverWait(driver, 10)
            wait.until(expected_conditions.element_to_be_clickable(reserve_element))

            reserve_element.click()
            logger.info("Clicked Reserve button")

            try:
                wait.until(
                    expected_conditions.presence_of_element_located(
                        (By.CSS_SELECTOR, ".modal, .dialog, [class*='popup'], form[class*='booking'], [class*='confirm']")
                    )
                )
            except TimeoutException:
                pass

            self._select_player_count_sync(driver, num_players)

            try:
                confirm_button = wait.until(
                    expected_conditions.element_to_be_clickable(
                        (
                            By.XPATH,
                            "//button[contains(text(), 'Confirm')] | "
                            "//button[contains(text(), 'Submit')] | "
                            "//button[contains(text(), 'Book')] | "
                            "//input[@type='submit']",
                        )
                    )
                )
                current_url = driver.current_url
                confirm_button.click()
                logger.info("Clicked confirmation button")

                try:
                    wait.until(expected_conditions.url_changes(current_url))
                except TimeoutException:
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (By.XPATH, "//*[contains(text(), 'success') or contains(text(), 'confirm') or contains(text(), 'thank')]")
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
                return BookingResult(
                    success=False,
                    error_message="Booking may not have completed successfully",
                    booked_time=booked_time,
                )

        except TimeoutException as e:
            logger.error(f"Booking confirmation timeout: {e}")
            return BookingResult(
                success=False,
                error_message=f"Booking confirmation timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"Booking click error: {e}")
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
        """Verify that the booking was successful by checking page content."""
        try:
            page_text = driver.page_source.lower()

            success_indicators = [
                "successfully",
                "confirmed",
                "booked",
                "reservation complete",
                "thank you",
            ]

            failure_indicators = [
                "error",
                "failed",
                "unavailable",
                "could not",
                "unable to",
            ]

            for indicator in failure_indicators:
                if indicator in page_text:
                    logger.warning(f"Found failure indicator: {indicator}")
                    return False

            for indicator in success_indicators:
                if indicator in page_text:
                    logger.info(f"Found success indicator: {indicator}")
                    return True

            if "home" in driver.current_url.lower() or "tee" in driver.current_url.lower():
                return True

            return True

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
                    (By.CSS_SELECTOR, ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form")
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
        Cancel an existing booking.

        Note: This is a placeholder - actual cancellation flow needs to be implemented
        based on the site cancellation interface.

        Args:
            confirmation_number: The booking confirmation number

        Returns:
            True if cancellation was successful, False otherwise
        """
        logger.warning("Cancellation not yet implemented")
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
