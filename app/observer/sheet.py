"""Driving a browser to the target tee sheet, and re-reading it once a second.

Read-only throughout. The only interactions performed here are logging in,
loading the tee sheet, and clicking a day tab - and the day tab that is clicked
during the window is the one already selected, which the club re-renders as the
same date (see ``_refresh_view`` in ``walden_http_booker``, which replays the
same element for the racer).

Two things this module does *not* do, both deliberate:

* **No course selection.** The racer selects Northgate because a Reserve is
  addressed against the form's selected course. The rendered sheet is not
  filtered by it: 2026-09-04's captured pre-window sheet carries 1052 row ids
  under ``teeTimeCourses:0`` (Northgate) *and* 686 under ``:1`` (Walden). Since
  the observer only reads, the ~400 lines of course-dropdown fallbacks in the
  provider would be duplicated for no gain. ``northgate_row_count`` is recorded
  during preparation instead, so a run that somehow renders no Northgate rows
  says so in its own manifest rather than being discovered a Friday later.
* **No parsing.** Nothing here builds a DOM tree or extracts slot state; each
  snapshot is the page's bytes, stored as-is. A 670KB sheet costs ~37ms to
  parse and that cost must not land inside the window (issue #189, trap 5). The
  two substring counts below run during preparation and after the last
  snapshot, never between them.
"""

import logging
import os
import re
import time as time_module
from dataclasses import dataclass, field
from datetime import date

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

from app.providers.walden_dom_schema import DOM

logger = logging.getLogger(__name__)

BASE_URL = "https://www.waldengolf.com"
LOGIN_URL = f"{BASE_URL}/web/pages/login"
TEE_TIME_URL = f"{BASE_URL}/group/pages/book-a-tee-time"

LOGIN_TIMEOUT_S = 15
PAGE_LOAD_TIMEOUT_S = 20
# The measured cost of a day-tab re-render is ~730ms (the racer spent that on
# 2026-08-07), so a second is enough headroom to notice a stall without eating
# into the next snapshot's slot.
REFRESH_SETTLE_TIMEOUT_S = 4.0

# Northgate is course 0 in every element id the site emits; the racer relies on
# the same constant (WaldenGolfProvider.NORTHGATE_COURSE_INDEX).
_NORTHGATE_ROW_ID_FRAGMENT = "teeTimeCourses:0:teeTimeSlots:"


@dataclass
class Snapshot:
    """One photograph of the tee sheet, and when it was actually taken.

    ``sent_offset_ms`` is the offset the object is named for: it is when the
    re-render was requested, which is the instant the returned sheet describes.
    The racer's ledger reasons the same way (``sentMsPastWindow`` plus a
    round-trip bound, §7e) so the two records line up on one clock.
    """

    index: int
    planned_offset_ms: int
    sent_offset_ms: int
    settled_offset_ms: int | None
    captured_offset_ms: int
    html: bytes
    refresh_ok: bool
    note: str | None = None


@dataclass
class Preparation:
    """What the pre-window preparation established, for the run record."""

    target_date: date
    selected_tab_text: str
    clicked_tab: bool
    northgate_row_count: int
    sheet_bytes: int
    ready_at_epoch_ms: int
    notes: list[str] = field(default_factory=list)


def create_driver() -> webdriver.Chrome:
    """A headless Chrome, configured exactly as the racer configures its own.

    Same flags on purpose: the sheet the club serves can depend on the client it
    thinks it is talking to, and a snapshot is only comparable with the racer's
    view if both asked as the same browser.
    """
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

    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path and os.path.exists(chromedriver_path):
        service = Service(chromedriver_path)
    else:
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def log_in(driver: webdriver.Chrome, member_number: str, password: str) -> bool:
    """Log the observer's own session in. Mirrors the provider's login flow."""
    try:
        logger.info("OBSERVER: navigating to the login page")
        driver.get(LOGIN_URL)

        wait = WebDriverWait(driver, LOGIN_TIMEOUT_S)
        member_input = wait.until(
            expected_conditions.presence_of_element_located((By.NAME, DOM.LOGIN.member_input_name))
        )
        password_input = driver.find_element(By.NAME, DOM.LOGIN.password_input_name)

        member_input.clear()
        member_input.send_keys(member_number)
        password_input.clear()
        password_input.send_keys(password)

        current_url = driver.current_url
        driver.find_element(By.CSS_SELECTOR, DOM.LOGIN.submit_button).click()

        try:
            wait.until(expected_conditions.url_changes(current_url))
        except TimeoutException:
            pass

        url = driver.current_url
        if "login" not in url.lower() or "home" in url.lower():
            logger.info("OBSERVER: login successful (url=%s)", url)
            return True

        logger.error("OBSERVER: login failed, still on %s", url)
        return False
    except (TimeoutException, WebDriverException) as e:
        logger.error("OBSERVER: login error: %s", e)
        return False


def open_tee_sheet(driver: webdriver.Chrome) -> bool:
    """Load the tee sheet page and wait for its form to exist."""
    try:
        driver.get(TEE_TIME_URL)
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT_S).until(
            expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form"))
        )
        logger.info("OBSERVER: tee sheet loaded (url=%s)", driver.current_url)
        return True
    except (TimeoutException, WebDriverException) as e:
        logger.error("OBSERVER: could not load the tee sheet: %s", e)
        return False


def _tab_matches(tab_text: str, target: date) -> bool:
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


def _selected_tab(driver: webdriver.Chrome) -> WebElement | None:
    """The day tab the sheet currently considers selected, if any."""
    tabs = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.selected_day_tab)
    return tabs[0] if tabs else None


def park_on_date(driver: webdriver.Chrome, target_date: date) -> Preparation | None:
    """Point this session's view at ``target_date`` and confirm it landed there.

    Returns the preparation record, or None when the view could not be confirmed
    to be on the target date.

    Refusing to continue is the right failure here. The observer exists to say
    what the club's sheet looked like on one particular date; bytes captured
    from the wrong date but named for the right one would be worse than no
    bytes at all, because a post-mortem would read them as evidence. An observer
    that gives up costs nothing - it never had a tee time at stake.
    """
    notes: list[str] = []

    selected = _selected_tab(driver)
    selected_text = selected.text if selected is not None else ""
    clicked = False

    if selected is not None and _tab_matches(selected_text, target_date):
        logger.info(
            "OBSERVER: sheet already parked on %s (tab=%r)",
            target_date,
            " ".join(selected_text.split()),
        )
    else:
        notes.append(f"selected tab was {' '.join(selected_text.split())!r}, wanted {target_date}")
        logger.info(
            "OBSERVER: sheet is on %r, clicking the day tab for %s",
            " ".join(selected_text.split()),
            target_date,
        )
        if not _click_day_tab(driver, target_date):
            logger.error(
                "OBSERVER: no day tab for %s in the date strip - capturing nothing rather "
                "than photographing the wrong date",
                target_date,
            )
            return None
        clicked = True

        selected = _selected_tab(driver)
        selected_text = selected.text if selected is not None else ""
        if not _tab_matches(selected_text, target_date):
            logger.error(
                "OBSERVER: after clicking, the sheet reports %r rather than %s",
                " ".join(selected_text.split()),
                target_date,
            )
            return None

    # Pre-window, so the parse-cost rule does not apply yet. A substring count
    # rather than a parse even so - it answers the only question being asked.
    page = driver.page_source
    row_count = page.count(_NORTHGATE_ROW_ID_FRAGMENT)
    if row_count == 0:
        # Not fatal: bytes with no Northgate rows still record what the club
        # served, and refusing to capture would throw away a morning over a
        # guess about markup. Loud, though - it makes the run unreadable.
        logger.error(
            "OBSERVER: the parked sheet carries no Northgate (%s) rows; snapshots will "
            "still be taken but may not show the contested block",
            _NORTHGATE_ROW_ID_FRAGMENT,
        )
        notes.append("no Northgate rows in the parked sheet")

    logger.info(
        "OBSERVER: parked on %s - %d Northgate row ids, %d bytes of sheet",
        target_date,
        row_count,
        len(page.encode("utf-8", errors="replace")),
    )
    return Preparation(
        target_date=target_date,
        selected_tab_text=" ".join(selected_text.split()),
        clicked_tab=clicked,
        northgate_row_count=row_count,
        sheet_bytes=len(page.encode("utf-8", errors="replace")),
        ready_at_epoch_ms=int(time_module.time() * 1000),
        notes=notes,
    )


def _click_day_tab(driver: webdriver.Chrome, target_date: date) -> bool:
    """Click the day tab naming ``target_date``, if the strip offers one."""
    try:
        tabs = driver.find_elements(By.CSS_SELECTOR, DOM.DATE_SELECTION.day_tab_links)
    except WebDriverException as e:
        logger.error("OBSERVER: could not read the date strip: %s", e)
        return False

    logger.info("OBSERVER: date strip offers %d tab(s)", len(tabs))
    for tab in tabs:
        try:
            if not _tab_matches(tab.text, target_date):
                continue
            previous = _selected_tab(driver)
            driver.execute_script("arguments[0].click();", tab)
            if previous is not None:
                _await_rerender(driver, previous)
            else:
                WebDriverWait(driver, REFRESH_SETTLE_TIMEOUT_S).until(
                    expected_conditions.presence_of_element_located(
                        (By.CSS_SELECTOR, DOM.DATE_SELECTION.selected_day_tab)
                    )
                )
            return True
        except WebDriverException as e:
            logger.warning("OBSERVER: a day tab could not be clicked (%s); trying the next", e)
            continue
    return False


def _await_rerender(driver: webdriver.Chrome, previous: WebElement) -> bool:
    """Wait for the day-tab AJAX to replace the form.

    The handler's ``u:`` renders the whole tee time form, so the element that
    was clicked is detached when the response is applied. Staleness is therefore
    the signal that the *club's* answer has landed, rather than a fixed sleep
    that could photograph a half-applied render.
    """
    try:
        WebDriverWait(driver, REFRESH_SETTLE_TIMEOUT_S).until(
            expected_conditions.staleness_of(previous)
        )
        return True
    except TimeoutException:
        return False


def wait_until_epoch_ms(target_epoch_ms: int) -> None:
    """Sleep coarsely, then busy-wait the last 200ms, as the racer does.

    Same shape as ``WaldenGolfProvider._precision_wait_until`` so the observer's
    and the racer's ideas of an instant do not differ by scheduler granularity.
    """
    remaining_s = (target_epoch_ms - time_module.time() * 1000) / 1000
    if remaining_s <= 0:
        return
    if remaining_s > 0.2:
        time_module.sleep(remaining_s - 0.2)
    while time_module.time() * 1000 < target_epoch_ms:
        time_module.sleep(0.0001)


def capture_across_window(
    driver: webdriver.Chrome,
    *,
    window_epoch_ms: int,
    count: int,
    interval_ms: int,
) -> list[Snapshot]:
    """Photograph the sheet ``count`` times, ``interval_ms`` apart, from the window.

    Each tick re-requests the sheet before reading it. That is the whole point
    of the job: a parked browser asked for its ``page_source`` nine times hands
    back nine copies of its 06:26 DOM, which is precisely the trap that makes
    the racer's own refusal bodies useless as evidence (§7d - the verdict is
    live, the body is not). A snapshot is only worth storing if the club
    re-rendered it.

    Nothing is uploaded here. The bytes are held in memory - nine sheets is
    about 6MB - and written to GCS after the last one, so no network round trip
    sits between two snapshots.
    """
    snapshots: list[Snapshot] = []

    for index in range(count):
        planned_offset_ms = index * interval_ms
        wait_until_epoch_ms(window_epoch_ms + planned_offset_ms)

        sent_epoch_ms = int(time_module.time() * 1000)
        refresh_ok = False
        settled_epoch_ms: int | None = None
        note: str | None = None

        # Two attempts, because the one likely failure here is a benign race:
        # the parked page runs its own refresh timers (the racer was caught by
        # the same thing - see PR #166, which cleared them mid-race), and one
        # firing between finding the tab and clicking it detaches the element.
        # Re-finding costs a few milliseconds and saves the snapshot.
        for attempt in (1, 2):
            try:
                previous = _selected_tab(driver)
                if previous is None:
                    note = "no selected day tab to replay; captured without a refresh"
                    logger.warning("OBSERVER: snapshot %d - %s", index, note)
                    break

                driver.execute_script("arguments[0].click();", previous)
                refresh_ok = _await_rerender(driver, previous)
                settled_epoch_ms = int(time_module.time() * 1000)
                if not refresh_ok:
                    note = (
                        f"re-render did not land within {REFRESH_SETTLE_TIMEOUT_S}s; "
                        "these bytes may repeat the previous snapshot"
                    )
                    logger.warning("OBSERVER: snapshot %d - %s", index, note)
                break
            except StaleElementReferenceException as e:
                if attempt == 1:
                    logger.info(
                        "OBSERVER: snapshot %d - the day tab went stale under the page's "
                        "own refresh; re-finding it",
                        index,
                    )
                    continue
                note = f"day tab stale on both attempts: {e}"
                logger.warning("OBSERVER: snapshot %d - %s", index, note)
            except WebDriverException as e:
                note = f"refresh raised {type(e).__name__}: {e}"
                logger.warning("OBSERVER: snapshot %d - %s", index, note)
                break

        try:
            html = driver.page_source.encode("utf-8", errors="replace")
        except WebDriverException as e:
            logger.error("OBSERVER: snapshot %d could not be read at all: %s", index, e)
            continue

        captured_epoch_ms = int(time_module.time() * 1000)
        snapshots.append(
            Snapshot(
                index=index,
                planned_offset_ms=planned_offset_ms,
                sent_offset_ms=sent_epoch_ms - window_epoch_ms,
                settled_offset_ms=(
                    None if settled_epoch_ms is None else settled_epoch_ms - window_epoch_ms
                ),
                captured_offset_ms=captured_epoch_ms - window_epoch_ms,
                html=html,
                refresh_ok=refresh_ok,
                note=note,
            )
        )
        logger.info(
            "OBSERVER: snapshot %d - sent +%dms, settled %s, read +%dms, %d bytes%s",
            index,
            sent_epoch_ms - window_epoch_ms,
            ("never" if settled_epoch_ms is None else f"+{settled_epoch_ms - window_epoch_ms}ms"),
            captured_epoch_ms - window_epoch_ms,
            len(html),
            "" if refresh_ok else " (STALE RISK)",
        )

    return snapshots
