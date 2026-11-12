"""Keeping the club's cookie-consent banner from intercepting clicks.

On 2026-11-12 a banner (``#ccBanner``, ``role="dialog"``) sat over the login
button, and a real Selenium click raised ElementClickInterceptedException in
both the racer and the observer. Each driver starts from a fresh profile and
never consents, so the banner can render on every page it loads. Hiding it
with a stylesheet installed on every new document covers the tee sheet too,
not just the login page.

Imports nothing that can reserve, so the read-only observer can share it.
"""

import json
import logging

from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException
from selenium.webdriver.remote.webelement import WebElement

from app.providers.walden_dom_schema import DOM

logger = logging.getLogger(__name__)

CONSENT_BANNER_CSS = f"{DOM.LOGIN.consent_banner} {{ display: none !important; }}"

HIDE_CONSENT_BANNER_SCRIPT = f"""
(() => {{
  const add = () => {{
    const style = document.createElement('style');
    style.textContent = {json.dumps(CONSENT_BANNER_CSS)};
    (document.head || document.documentElement).appendChild(style);
  }};
  if (document.documentElement) {{
    add();
  }} else {{
    document.addEventListener('readystatechange', add, {{ once: true }});
  }}
}})();
"""


def hide_consent_banner(driver: webdriver.Chrome) -> None:
    """Hide the banner in every document this driver loads from now on."""
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument", {"source": HIDE_CONSENT_BANNER_SCRIPT}
    )


def click_past_overlay(driver: webdriver.Chrome, element: WebElement, *, log_prefix: str) -> None:
    """A real click, falling back to a JS-dispatched one if an overlay intercepts it.

    The real click goes first so a button that is hidden or not interactable
    still fails loudly. The fallback logs, so a banner that gets past the
    stylesheet shows up in the logs instead of passing silently.
    """
    try:
        element.click()
    except ElementClickInterceptedException as e:
        logger.warning(
            "%s: click intercepted (%s); dispatching it via JS",
            log_prefix,
            (e.msg or "no message").splitlines()[0],
        )
        driver.execute_script("arguments[0].click();", element)
