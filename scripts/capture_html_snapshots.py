#!/usr/bin/env python3
"""
Capture HTML snapshots from the live Walden Golf site for testing.

This script:
1. Logs in to the Walden Golf member portal
2. Navigates to the tee time booking page
3. Captures HTML snapshots at various states
4. Saves them as test fixtures

Usage:
    python scripts/capture_html_snapshots.py
"""

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from app.config import settings

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"

# Wait time constants (in seconds)
PAGE_LOAD_WAIT = 2
POST_LOGIN_WAIT = 3
DYNAMIC_CONTENT_WAIT = 3
TEE_TIME_PAGE_WAIT = 5

# Walden Golf URLs
BASE_URL = "https://www.waldengolf.com"
LOGIN_URL = f"{BASE_URL}/web/pages/login"
TEE_TIME_URL = f"{BASE_URL}/group/pages/book-a-tee-time"


def create_driver() -> webdriver.Chrome:
    """Create a headless Chrome WebDriver instance for snapshot capture."""
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
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=options)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )

    return driver


def save_snapshot(driver, name: str, metadata: dict | None = None) -> Path:
    """Save HTML snapshot and metadata."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    html_path = FIXTURES_DIR / f"{name}.html"
    html_path.write_text(driver.page_source, encoding="utf-8")
    print(f"  Saved: {html_path}")

    if metadata:
        meta_path = FIXTURES_DIR / f"{name}.meta.json"
        metadata["url"] = driver.current_url
        metadata["title"] = driver.title
        metadata["captured_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"  Saved: {meta_path}")

    return html_path


def capture_snapshots():
    """Main capture routine."""
    print("=" * 60)
    print("Walden Golf HTML Snapshot Capture")
    print("=" * 60)

    if not settings.walden_member_number or not settings.walden_password:
        print("ERROR: Walden Golf credentials not configured.")
        print("Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD in .env")
        sys.exit(1)

    driver = create_driver()

    try:
        # 1. Capture login page
        print("\n[1/6] Capturing login page...")
        driver.get(LOGIN_URL)
        time.sleep(PAGE_LOAD_WAIT)
        save_snapshot(driver, "walden_login_page", {"state": "login_form"})

        # 2. Perform login
        print("\n[2/6] Performing login...")
        wait = WebDriverWait(driver, 15)
        member_input = wait.until(
            expected_conditions.presence_of_element_located(
                (By.NAME, "_com_liferay_login_web_portlet_LoginPortlet_login")
            )
        )
        password_input = driver.find_element(
            By.NAME, "_com_liferay_login_web_portlet_LoginPortlet_password"
        )

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
            pass  # URL may not change if login fails, continue to check

        time.sleep(POST_LOGIN_WAIT)

        if "login" in driver.current_url.lower() and "home" not in driver.current_url.lower():
            print(f"ERROR: Login failed. URL: {driver.current_url}")
            save_snapshot(driver, "walden_login_failed", {"state": "login_failed"})
            sys.exit(1)

        print(f"  Login successful. URL: {driver.current_url}")
        save_snapshot(driver, "walden_post_login", {"state": "logged_in"})

        # 3. Navigate to tee time page
        print("\n[3/6] Navigating to tee time booking page...")
        driver.get(TEE_TIME_URL)
        time.sleep(TEE_TIME_PAGE_WAIT)
        save_snapshot(driver, "walden_tee_time_initial", {"state": "tee_time_page_initial"})

        # 4. Wait for datascroller to load
        print("\n[4/6] Waiting for tee time slots to load...")
        try:
            wait.until(
                expected_conditions.presence_of_element_located(
                    (By.CSS_SELECTOR, "ul.ui-datascroller-list")
                )
            )
            time.sleep(DYNAMIC_CONTENT_WAIT)
        except TimeoutException as e:
            print(f"  Warning: Datascroller not found: {e}")

        save_snapshot(
            driver,
            "walden_tee_time_loaded",
            {"state": "tee_time_page_loaded", "note": "After waiting for datascroller"},
        )

        # 5. Try to select Northgate course
        print("\n[5/6] Attempting to select Northgate course...")
        course_selected = False

        # Look for course dropdown/selector
        dropdown_selectors = [
            "[class*='select'][class*='course']",
            "div[class*='multiselect']",
            "button[class*='dropdown']",
            ".course-dropdown",
            "[aria-label*='course' i]",
            "select[id*='course']",
            "div.p-multiselect",
        ]

        for selector in dropdown_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"  Found {len(elements)} elements matching: {selector}")
                    course_selected = True
            except Exception:  # noqa: BLE001 - intentionally catching all selector errors
                continue

        if not course_selected:
            print("  No course dropdown found with standard selectors")

        save_snapshot(
            driver,
            "walden_tee_time_with_course",
            {"state": "course_selection", "course_dropdown_found": course_selected},
        )

        # 6. Try to navigate to a future date (7 days out)
        print("\n[6/6] Attempting to navigate to future date...")
        target_date = date.today() + timedelta(days=7)
        print(f"  Target date: {target_date}")

        # Look for date picker elements
        date_selectors = [
            "input[type='date']",
            "[class*='datepicker']",
            "[class*='calendar']",
            "input[id*='date']",
            ".p-datepicker",
            "span.p-datepicker-trigger",
        ]

        for selector in date_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"  Found {len(elements)} elements matching: {selector}")
            except Exception:  # noqa: BLE001 - intentionally catching all selector errors
                continue

        save_snapshot(
            driver, "walden_tee_time_final", {"state": "final", "target_date": str(target_date)}
        )

        # Summary
        print("\n" + "=" * 60)
        print("Snapshot capture complete!")
        print(f"Fixtures saved to: {FIXTURES_DIR}")
        print("=" * 60)

        # List saved files
        print("\nSaved files:")
        for f in sorted(FIXTURES_DIR.glob("walden_*")):
            print(f"  - {f.name}")

    finally:
        driver.quit()
        print("\nDriver closed.")


if __name__ == "__main__":
    capture_snapshots()
