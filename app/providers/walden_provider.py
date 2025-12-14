import asyncio
import functools
import logging
import re
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
                        import time as time_module

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
    """

    BASE_URL = "https://www.waldengolf.com"
    LOGIN_URL = f"{BASE_URL}/web/pages/login"
    DASHBOARD_URL = f"{BASE_URL}/group/pages/home"
    TEE_TIME_URL = f"{BASE_URL}/group/pages/book-a-tee-time"

    NORTHGATE_COURSE_NAME = "Northgate"
    TEE_TIME_INTERVAL_MINUTES = 8

    def __init__(self) -> None:
        self._driver: webdriver.Chrome | None = None
        self._logged_in: bool = False

    async def __aenter__(self) -> "WaldenGolfProvider":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit - ensures cleanup even on exceptions."""
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

    def _ensure_driver(self) -> webdriver.Chrome:
        """Ensure we have an active WebDriver instance."""
        if self._driver is None:
            self._driver = self._create_driver()
        return self._driver

    async def login(self) -> bool:
        """
        Log in to the Walden Golf member portal.

        Uses the member number and password from settings to authenticate.
        The login form uses Liferay's standard login portlet.

        Returns:
            True if login was successful, False otherwise.
        """
        if self._logged_in:
            return True

        driver = self._ensure_driver()

        try:
            logger.info("Navigating to login page...")
            driver.get(self.LOGIN_URL)

            await asyncio.sleep(2)

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
            submit_button.click()

            await asyncio.sleep(3)

            if "login" not in driver.current_url.lower() or "home" in driver.current_url.lower():
                self._logged_in = True
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

        This method:
        1. Logs in if not already authenticated
        2. Navigates to the tee time booking page
        3. Selects the Northgate course
        4. Selects the target date
        5. Finds the requested time slot (or nearest available within fallback window)
        6. Clicks Reserve and confirms the booking

        Note: Selenium operations are synchronous and will block the event loop.
        This is acceptable for scheduled background jobs (Cloud Run Jobs) but callers
        in async contexts should consider using asyncio.to_thread() if needed.

        Args:
            target_date: The date to book (should be 7 days in advance for new bookings)
            target_time: The preferred tee time
            num_players: Number of players (1-4). The player count selector is visible
                in the tee sheet interface and will be set before confirming the booking.
            fallback_window_minutes: If exact time unavailable, try times within this window

        Returns:
            BookingResult with success status, booked time, and confirmation details
        """
        if not await self.login():
            return BookingResult(
                success=False,
                error_message="Failed to log in to Walden Golf",
            )

        driver = self._ensure_driver()

        try:
            logger.info("Navigating to tee time booking page...")
            driver.get(self.TEE_TIME_URL)
            await asyncio.sleep(3)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))

            await self._select_course(driver, self.NORTHGATE_COURSE_NAME)

            await self._select_date(driver, target_date)

            await asyncio.sleep(2)

            result = await self._find_and_book_time_slot(
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

    async def _select_course(self, driver: webdriver.Chrome, course_name: str) -> None:
        """Select the course from the dropdown."""
        try:
            course_select = driver.find_element(By.CSS_SELECTOR, "select[id*='course']")
            select = Select(course_select)

            for option in select.options:
                if course_name.lower() in option.text.lower():
                    select.select_by_visible_text(option.text)
                    logger.info(f"Selected course: {option.text}")
                    await asyncio.sleep(1)
                    return

            logger.warning(f"Course '{course_name}' not found in dropdown, using default")

        except NoSuchElementException:
            logger.info("No course dropdown found - may already be on correct course page")

    async def _select_date(self, driver: webdriver.Chrome, target_date: date) -> None:
        """Select the target date using the date picker or day tabs."""
        try:
            date_str = target_date.strftime("%m/%d/%Y")

            date_input = driver.find_element(By.CSS_SELECTOR, "input[type='text'][id*='date']")
            date_input.clear()
            date_input.send_keys(date_str)
            logger.info(f"Entered date: {date_str}")

            await asyncio.sleep(1)

            try:
                search_button = driver.find_element(
                    By.CSS_SELECTOR, "button[type='submit'], input[type='submit']"
                )
                search_button.click()
                await asyncio.sleep(2)
            except NoSuchElementException:
                pass

        except NoSuchElementException:
            logger.info("No date input found, trying day tabs...")
            await self._select_date_via_tabs(driver, target_date)

    async def _select_date_via_tabs(self, driver: webdriver.Chrome, target_date: date) -> None:
        """Select date using the day-of-week tabs if date picker not available."""
        day_name = target_date.strftime("%A")

        try:
            day_tabs = driver.find_elements(
                By.CSS_SELECTOR, ".day-tab, [class*='day'], a[href*='day']"
            )
            for tab in day_tabs:
                if day_name.lower() in tab.text.lower():
                    tab.click()
                    logger.info(f"Clicked day tab: {day_name}")
                    await asyncio.sleep(2)
                    return

            logger.warning(f"Could not find day tab for {day_name}")

        except NoSuchElementException:
            logger.warning("No day tabs found")

    async def _select_player_count(self, driver: webdriver.Chrome, num_players: int) -> None:
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
                    await asyncio.sleep(0.5)
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
                    await asyncio.sleep(0.5)
                    return
                except NoSuchElementException:
                    continue

            logger.warning(
                f"Could not find player count selector - site may auto-fill or use different control. "
                f"Requested {num_players} players."
            )

        except Exception as e:
            logger.warning(f"Error selecting player count: {e}")

    async def _find_and_book_time_slot(
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

        return await self._complete_booking(driver, reserve_element, booked_time, num_players)

    def _find_available_slots(self, search_context: Any) -> list[tuple[time, Any]]:
        """
        Find all available time slots in the tee sheet.

        Returns:
            List of (time, element) tuples for available slots
        """
        available_slots: list[tuple[time, Any]] = []

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
                    logger.debug(f"Could not parse slot: {e}")
                    continue

        except NoSuchElementException:
            logger.warning("No Reserve buttons found")

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
                    logger.debug(f"Could not parse available slot: {e}")
                    continue

        except NoSuchElementException:
            pass

        available_slots.sort(key=lambda x: x[0])
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

    async def _complete_booking(
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
            await asyncio.sleep(0.5)

            reserve_element.click()
            logger.info("Clicked Reserve button")

            await asyncio.sleep(2)

            wait = WebDriverWait(driver, 10)

            await self._select_player_count(driver, num_players)

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
                confirm_button.click()
                logger.info("Clicked confirmation button")
                await asyncio.sleep(2)

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

        Args:
            target_date: The date to check availability for

        Returns:
            List of available times
        """
        if not await self.login():
            return []

        driver = self._ensure_driver()

        try:
            driver.get(self.TEE_TIME_URL)
            await asyncio.sleep(3)

            await self._select_course(driver, self.NORTHGATE_COURSE_NAME)
            await self._select_date(driver, target_date)
            await asyncio.sleep(2)

            available_slots = self._find_available_slots(driver)
            return [slot_time for slot_time, _ in available_slots]

        except WebDriverException as e:
            logger.error(f"Error getting available times: {e}")
            return []

    async def cancel_booking(self, confirmation_number: str) -> bool:
        """
        Cancel an existing booking.

        Note: This is a placeholder - actual cancellation flow needs to be implemented
        based on the site's cancellation interface.

        Args:
            confirmation_number: The booking confirmation number

        Returns:
            True if cancellation was successful, False otherwise
        """
        logger.warning("Cancellation not yet implemented")
        return False

    async def close(self) -> None:
        """Close the WebDriver and clean up resources."""
        if self._driver:
            try:
                self._driver.quit()
            except WebDriverException:
                pass
            self._driver = None

        self._logged_in = False


class MockWaldenProvider(ReservationProvider):
    """Mock provider for testing without hitting the real booking system."""

    def __init__(self) -> None:
        self._logged_in = False

    async def login(self) -> bool:
        self._logged_in = True
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
