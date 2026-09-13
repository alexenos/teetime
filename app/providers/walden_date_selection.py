"""Pointing the club's tee sheet at a date, for whoever needs it pointed there.

The racer and the observer both have to reach the same date on the same page,
and until issue #199 they did it differently: the racer through the calendar
widget, the observer by looking for a day tab that could never be there. One of
those was proven daily against the live site and the other had never once
worked. This module is the racer's routine, moved rather than rewritten, so that
there is one implementation of a fiddly interaction to maintain and one place a
markup change has to be answered.

It deliberately imports nothing that can send a Reserve. The observer's
read-only guarantee is enforced by tests that refuse any import path from
``app/observer`` to the booking modules (``test_observer.py``), so a shared
module has to stay on the harmless side of that line to be shareable at all.

The three strategies, in the order they are tried:

1. **A date input.** None of the nine selectors matches anything on the current
   tee sheet - the club's date field is a readonly PrimeFaces input whose id,
   name and class carry no "date" - so this is dead against today's markup and
   kept only as the cheap first guess it always was.
2. **The calendar widget.** What actually works. The captured sheet has no
   ``<td>`` elements at all, so the day lookup below can only match inside a
   datepicker that genuinely opened - which is why the racer's "Selected day N
   from calendar" is real evidence and not a self-congratulating log line.
3. **The date strip.** ``.horizontal-dates`` renders a seven-day horizon and
   paginates. This is the fallback the racer never had: before #199 a calendar
   that failed simply lost the booking.
"""

import logging
import re
from collections.abc import Callable
from datetime import date, datetime

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import Select, WebDriverWait

from app.providers.wait_helper import WaitStrategy
from app.providers.walden_dom_schema import DOM

logger = logging.getLogger(__name__)

# How many times the date strip may be paged forward while looking for the
# target date. One click is what an ordinary morning needs - the strip spans
# seven days from the selected date and the target is the eighth - so this is
# mostly a ceiling on a strip that pages without ever arriving, which would
# otherwise spend the whole pre-window budget on round trips. Eight covers a
# target a fortnight out, at roughly 730ms a click.
MAX_STRIP_ADVANCES = 8

# The measured cost of a day-tab re-render is ~730ms (the racer spent that on
# 2026-08-07), so a second is enough headroom to notice a stall.
STRIP_SETTLE_TIMEOUT_S = 4.0


def select_date(
    driver: webdriver.Chrome,
    target_date: date,
    *,
    wait_strategy: WaitStrategy,
    log_prefix: str,
) -> bool:
    """Point the tee sheet at ``target_date``. False when it could not be done.

    Callers must not treat True as proof of where the sheet landed. Nothing here
    re-reads the page to confirm the date changed, because the two callers check
    it differently: the racer finds out when the club answers its Reserve, and
    the observer asserts the parked date itself before it will store a single
    byte. A shared "did it work" would be weaker than either.
    """
    day_name = target_date.strftime("%A")
    date_str = target_date.strftime("%m/%d/%Y")
    date_str_alt = target_date.strftime("%Y-%m-%d")
    logger.info("%s: Selecting date %s (%s)", log_prefix, target_date, day_name)

    for selector in DOM.DATE_SELECTION.date_inputs:
        try:
            date_input = driver.find_element(By.CSS_SELECTOR, selector)
            input_type = date_input.get_attribute("type")

            date_input.clear()
            date_input.send_keys(date_str_alt if input_type == "date" else date_str)
            logger.info("%s: Entered date %s using selector: %s", log_prefix, date_str, selector)

            try:
                WebDriverWait(driver, 5).until(
                    expected_conditions.element_to_be_clickable(
                        (By.CSS_SELECTOR, DOM.DATE_SELECTION.search_submit)
                    )
                ).click()
                logger.info("%s: Clicked search/submit button after date entry", log_prefix)
            except TimeoutException:
                pass

            return True
        except NoSuchElementException:
            continue

    logger.info("%s: No date input found, using calendar picker...", log_prefix)
    if select_via_calendar(driver, target_date, wait_strategy=wait_strategy, log_prefix=log_prefix):
        logger.info("%s: Date selection via calendar successful", log_prefix)
        return True

    logger.warning(
        "%s: Calendar date selection failed for %s; falling back to the date strip",
        log_prefix,
        target_date,
    )
    if select_via_strip(driver, target_date, log_prefix=log_prefix):
        logger.info("%s: Date selection via the date strip successful", log_prefix)
        return True

    logger.error("%s: Could not reach %s by any route", log_prefix, target_date)
    return False


# -- the calendar widget ----------------------------------------------------


def select_via_calendar(
    driver: webdriver.Chrome,
    target_date: date,
    *,
    wait_strategy: WaitStrategy,
    log_prefix: str,
) -> bool:
    """Select ``target_date`` through the calendar popup."""
    try:
        calendar_triggers = driver.find_elements(
            By.CSS_SELECTOR, DOM.DATE_SELECTION.calendar_triggers
        )
        if not calendar_triggers:
            return False

        calendar_triggers[0].click()
        logger.info("%s: Clicked calendar trigger", log_prefix)

        try:
            WebDriverWait(driver, 5).until(
                expected_conditions.presence_of_element_located(
                    (By.CSS_SELECTOR, DOM.DATE_SELECTION.calendar_popup)
                )
            )
        except TimeoutException:
            logger.warning("%s: Calendar popup did not appear", log_prefix)
            return False

        # Weak on its own: the club ships an empty `#ui-datepicker-div` in every
        # sheet, so this condition is already satisfied before anything is
        # clicked. The day lookup below is what actually distinguishes an open
        # calendar from a closed one.
        logger.info("%s: Calendar popup appeared", log_prefix)

        if not navigate_calendar_to_month(
            driver, target_date, wait_strategy=wait_strategy, log_prefix=log_prefix
        ):
            logger.warning(
                "%s: Failed to navigate calendar to %s",
                log_prefix,
                target_date.strftime("%B %Y"),
            )
            return False

        day_str = str(target_date.day)
        day_elements = driver.find_elements(
            By.XPATH, " | ".join(xp.format(day=day_str) for xp in DOM.DATE_SELECTION.day_xpaths)
        )
        logger.info("%s: Found %d day elements for day %s", log_prefix, len(day_elements), day_str)

        for day_el in day_elements:
            if not (day_el.is_displayed() and day_el.is_enabled()):
                continue
            day_class = day_el.get_attribute("class") or ""
            if any(other in day_class for other in DOM.DATE_SELECTION.other_month_classes):
                logger.debug("%s: Skipping day element with class: %s", log_prefix, day_class)
                continue

            day_el.click()
            logger.info(
                "%s: Selected day %s from calendar for date %s", log_prefix, day_str, target_date
            )
            wait_strategy.wait_after_action(driver, fixed_duration=2.0)
            try:
                WebDriverWait(driver, 10).until(
                    expected_conditions.presence_of_element_located(
                        (By.CSS_SELECTOR, DOM.DATE_SELECTION.tee_time_presence)
                    )
                )
            except TimeoutException:
                logger.debug("%s: Tee time slots not found after calendar selection", log_prefix)
            return True

        logger.warning("%s: No clickable day element found for day %s", log_prefix, day_str)
    except Exception as e:
        logger.warning("%s: Calendar selection failed: %s", log_prefix, e)

    return False


def navigate_calendar_to_month(
    driver: webdriver.Chrome,
    target_date: date,
    *,
    wait_strategy: WaitStrategy,
    log_prefix: str,
) -> bool:
    """Bring the calendar to ``target_date``'s month, by dropdown or by arrow."""
    target_month = target_date.month
    target_year = target_date.year
    target_month_name = target_date.strftime("%B")
    target_month_abbr = target_date.strftime("%b")

    logger.info("%s: Navigating calendar to %s %s", log_prefix, target_month_name, target_year)

    try:
        month_selects = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.month_dropdown)
        year_selects = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.year_dropdown)

        if month_selects and year_selects:
            logger.info("%s: Found month/year dropdowns, using select strategy", log_prefix)

            year_select = Select(year_selects[0])
            try:
                year_select.select_by_value(str(target_year))
            except Exception:
                try:
                    year_select.select_by_visible_text(str(target_year))
                except Exception as e:
                    logger.warning("%s: Could not select year: %s", log_prefix, e)

            wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)

            # 0-indexed in some implementations, 1-indexed in others, and named
            # in the rest; the site has never shown these, so all four remain.
            month_select = Select(month_selects[0])
            attempts: tuple[Callable[[], None], ...] = (
                lambda: month_select.select_by_value(str(target_month - 1)),
                lambda: month_select.select_by_value(str(target_month)),
                lambda: month_select.select_by_visible_text(target_month_name),
                lambda: month_select.select_by_visible_text(target_month_abbr),
            )
            for attempt in attempts:
                try:
                    attempt()
                    logger.info("%s: Selected month %s", log_prefix, target_month_name)
                    break
                except Exception:
                    continue
            else:
                logger.warning("%s: Could not select month %s", log_prefix, target_month_name)

            wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.2)
            return True
    except Exception as e:
        logger.debug("%s: Dropdown strategy failed: %s", log_prefix, e)

    try:
        current_month, current_year = calendar_current_month(driver)
        if current_month is None or current_year is None:
            logger.warning("%s: Could not determine current calendar month", log_prefix)
            current_month = datetime.now().month
            current_year = datetime.now().year

        logger.info(
            "%s: Calendar currently showing %s/%s, need %s/%s",
            log_prefix,
            current_month,
            current_year,
            target_month,
            target_year,
        )

        months_diff = (target_year - current_year) * 12 + (target_month - current_month)
        if months_diff == 0:
            logger.info("%s: Already on correct month", log_prefix)
            return True

        if months_diff > 0:
            nav_selectors, direction = DOM.DATE_SELECTION.nav_next, "next"
        else:
            nav_selectors, direction = DOM.DATE_SELECTION.nav_prev, "prev"
            months_diff = abs(months_diff)

        for step in range(months_diff):
            # Re-found every time: the calendar replaces its own header as it
            # moves, so the button held from the last iteration is detached.
            nav_button = _first_interactable(driver, nav_selectors)
            if nav_button is None:
                logger.warning("%s: Could not find %s navigation button", log_prefix, direction)
                return False
            try:
                nav_button.click()
            except Exception as e:
                logger.warning("%s: Error clicking nav button: %s", log_prefix, e)
                return False
            logger.debug(
                "%s: Clicked %s button (%d/%d)", log_prefix, direction, step + 1, months_diff
            )
            wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)

        logger.info(
            "%s: Navigated %d months %s to reach %s %s",
            log_prefix,
            months_diff,
            direction,
            target_month_name,
            target_year,
        )
        return True
    except Exception as e:
        logger.warning("%s: Navigation arrow strategy failed: %s", log_prefix, e)

    return False


def calendar_current_month(driver: webdriver.Chrome) -> tuple[int | None, int | None]:
    """The month and year the calendar is displaying, if it can be read."""
    try:
        month_selects = driver.find_elements(
            By.CSS_SELECTOR, "select.ui-datepicker-month, select[class*='month']"
        )
        year_selects = driver.find_elements(
            By.CSS_SELECTOR, "select.ui-datepicker-year, select[class*='year']"
        )

        if month_selects and year_selects:
            month_val = Select(month_selects[0]).first_selected_option.get_attribute("value")
            year_val = Select(year_selects[0]).first_selected_option.get_attribute("value")
            if month_val is not None and year_val is not None:
                month_int = int(month_val)
                if month_int < 12:  # Likely 0-indexed
                    month_int += 1
                return month_int, int(year_val)

        for selector in DOM.DATE_SELECTION.calendar_headers:
            for header in driver.find_elements(By.CSS_SELECTOR, selector):
                text = header.text.strip()
                for fmt in ("%B %Y", "%b %Y"):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        return parsed.month, parsed.year
                    except ValueError:
                        continue
    except Exception as e:
        logger.debug("Error getting current calendar month: %s", e)

    return None, None


def _first_interactable(driver: webdriver.Chrome, selectors: tuple[str, ...]) -> WebElement | None:
    """The first displayed, enabled element any of ``selectors`` finds."""
    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue
    return None


# -- the date strip ---------------------------------------------------------


def tab_matches(tab_text: str, target: date) -> bool:
    """Whether a day tab's rendered text names ``target``.

    The tabs render as e.g. ``"Friday Fri 11 September Sep"`` - weekday long and
    short, day of month, month long and short (verified against the 2026-09-04
    pre-window sheet). All three of weekday, day number and month are required,
    so a strip spanning a month boundary cannot match the wrong tab, and the day
    number is matched as a whole word so ``1`` never matches ``11``.
    """
    text = " ".join(tab_text.split())
    if not text:
        return False
    day_ok = re.search(rf"(?<!\d){target.day}(?!\d)", text) is not None
    month_ok = target.strftime("%B").lower() in text.lower()
    weekday_ok = target.strftime("%A").lower() in text.lower()
    return day_ok and month_ok and weekday_ok


def selected_tab(driver: webdriver.Chrome) -> WebElement | None:
    """The day tab the sheet currently considers selected, if any."""
    tabs = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.selected_day_tab)
    return tabs[0] if tabs else None


def select_via_strip(driver: webdriver.Chrome, target_date: date, *, log_prefix: str) -> bool:
    """Click the day tab naming ``target_date``, paging the strip to find it.

    The strip renders a seven-day horizon starting at the selected date, so a
    target seven days out - the date that opens at the booking window - sits one
    page past its end on a freshly loaded sheet. Paging is therefore the normal
    path here rather than a rare fallback.
    """
    advances = 0
    while True:
        if _click_day_tab(driver, target_date, log_prefix=log_prefix):
            return True
        if advances >= MAX_STRIP_ADVANCES:
            logger.error(
                "%s: %s is still not in the date strip after paging forward %d time(s); "
                "the strip offers %s",
                log_prefix,
                target_date,
                advances,
                strip_tab_texts(driver),
            )
            return False
        if not _advance_strip(driver, log_prefix=log_prefix):
            return False
        advances += 1


def strip_tab_texts(driver: webdriver.Chrome) -> list[str]:
    """What the date strip currently spans, as rendered text. For the log."""
    try:
        tabs = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.day_tab_links)
        return [" ".join(tab.text.split()) for tab in tabs]
    except WebDriverException:
        return []


def _click_day_tab(driver: webdriver.Chrome, target_date: date, *, log_prefix: str) -> bool:
    """Click the day tab naming ``target_date``, if the strip offers one."""
    try:
        tabs = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.day_tab_links)
    except WebDriverException as e:
        logger.error("%s: could not read the date strip: %s", log_prefix, e)
        return False

    logger.info("%s: date strip offers %d tab(s)", log_prefix, len(tabs))
    for tab in tabs:
        try:
            if not tab_matches(tab.text, target_date):
                continue
            previous = selected_tab(driver)
            driver.execute_script("arguments[0].click();", tab)
            if previous is not None:
                await_rerender(driver, previous)
            else:
                # Paging leaves the old selection out of view, so the arrival of
                # a selected tab is itself the signal the click landed.
                WebDriverWait(driver, STRIP_SETTLE_TIMEOUT_S).until(
                    expected_conditions.presence_of_element_located(
                        (By.CSS_SELECTOR, DOM.DATE_SELECTION.selected_day_tab)
                    )
                )
            return True
        except WebDriverException as e:
            logger.warning(
                "%s: a day tab could not be clicked (%s); trying the next", log_prefix, e
            )
            continue
    return False


def _advance_strip(driver: webdriver.Chrome, *, log_prefix: str) -> bool:
    """Page the date strip a day forward. False when it did not move.

    The forward control re-renders the whole tee time form exactly as a day tab
    does, so the click is awaited the same way. The before/after comparison is
    the real check rather than the re-render landing: a strip the club refuses
    to page past its horizon would answer the click without moving, and clicking
    it another seven times would just burn the pre-window budget.
    """
    try:
        controls = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.strip_forward_links)
    except WebDriverException as e:
        logger.error("%s: could not read the strip's forward controls: %s", log_prefix, e)
        return False

    if not controls:
        logger.error(
            "%s: the date strip offers no enabled forward control, so nothing past its "
            "horizon can be reached; it spans %s",
            log_prefix,
            strip_tab_texts(driver),
        )
        return False

    before = strip_tab_texts(driver)
    control = _single_day_control(controls)
    try:
        driver.execute_script("arguments[0].click();", control)
    except WebDriverException as e:
        logger.error("%s: the strip's forward control could not be clicked: %s", log_prefix, e)
        return False
    await_rerender(driver, control)

    after = strip_tab_texts(driver)
    if after == before:
        logger.error("%s: the date strip did not move; it still offers %s", log_prefix, before)
        return False
    logger.info("%s: paged the date strip forward; it now offers %s", log_prefix, after)
    return True


def _single_day_control(controls: list[WebElement]) -> WebElement:
    """The one-day-forward link among the strip's forward controls.

    The two are told apart only by the icon they carry - ``fa-angle-right`` for
    a day, ``fa-angle-double-right`` for a week. Picking the wrong one is not
    fatal, which is why an unrecognised pair falls back to the last rather than
    refusing: either brings a target seven days out into view, a day forward
    putting it last in the strip and a week forward putting it first.
    """
    for control in controls:
        try:
            icons = control.find_elements(By.TAG_NAME, "i")
        except WebDriverException:
            continue
        if any("fa-angle-right" in (icon.get_attribute("class") or "") for icon in icons):
            return control
    return controls[-1]


def await_rerender(driver: webdriver.Chrome, previous: WebElement) -> bool:
    """Wait for a strip click's AJAX to replace the form.

    The handler's ``u:`` renders the whole tee time form, so the element that
    was clicked is detached when the response is applied. Staleness is therefore
    the signal that the *club's* answer has landed, rather than a fixed sleep
    that could photograph a half-applied render.
    """
    try:
        WebDriverWait(driver, STRIP_SETTLE_TIMEOUT_S).until(
            expected_conditions.staleness_of(previous)
        )
        return True
    except TimeoutException:
        return False
