"""
Integration tests for the async booking chain against a synthetic page.

The fixture page (tests/fixtures/async_chain_test_page.html) reproduces the
event-loop dynamics of the real Walden tee sheet: the 'disable-div' gate is
removed by a page timer, and the player page renders via async callbacks -
none of which can happen while a script blocks the event loop. These tests
would all fail (timeout waiting for disable-div) under the old synchronous
spin-wait implementation.

Requires Chrome. When it isn't available these tests skip locally and fail
under CI - see ``_browser_required`` for why the two differ.
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.providers.walden_provider import WaldenGolfProvider

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "async_chain_test_page.html"

# Wall-clock assertions on a loaded CI runner can exceed tight bounds;
# override via env when needed (e.g. CHAIN_DRIFT_TOLERANCE_MS=500 on CI).
DRIFT_TOLERANCE_MS = int(os.environ.get("CHAIN_DRIFT_TOLERANCE_MS", "150"))


_TRUTHY = {"1", "true", "yes", "on"}


def _browser_required() -> bool:
    """Whether a missing Chrome should fail the run rather than skip it.

    Skipping is right on a laptop that has no Chrome. In CI it is the hole
    #159 describes: the seven tests below vanish, the job still reports
    success, and the only automated coverage of the async booking chain is
    gone with nothing to say so.

    ``ALLOW_BROWSER_TEST_SKIP`` is the escape hatch for an environment that
    sets ``CI`` but genuinely has no browser - the remote dev container, where
    only Playwright's Chromium is installed and ``google-chrome`` is not on
    ``PATH``, is the case this exists for.
    """
    if os.environ.get("ALLOW_BROWSER_TEST_SKIP", "").strip().lower() in _TRUTHY:
        return False
    return os.environ.get("CI", "").strip().lower() in _TRUTHY


def _chrome_options():  # type: ignore[no-untyped-def]
    """Headless Chrome options, pointed at ``CHROME_BINARY`` when one is named.

    Without it, chromedriver resolves the browser itself and lands on
    ``/usr/bin/google-chrome`` - the runner image's Chrome, whatever version
    that happens to be, regardless of what the workflow installed or what is
    first on ``PATH``. That is how CI got a driver and a browser a major
    version apart. Naming the binary is what ties the two together.
    """
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    binary = os.environ.get("CHROME_BINARY", "").strip()
    if binary:
        options.binary_location = binary
    return options


def _chrome_service():  # type: ignore[no-untyped-def]
    """The driver to talk to, pinned to ``CHROMEDRIVER_BINARY`` when named.

    A chromedriver already on ``PATH`` is used in preference to fetching a
    matching one, so naming the browser alone is not enough: the runner image
    ships its own driver, and pairing it with a Chrome the workflow installed
    separately is the same major-version mismatch from the other side. Both
    halves come from the same install step, or neither does.
    """
    from selenium.webdriver.chrome.service import Service

    driver = os.environ.get("CHROMEDRIVER_BINARY", "").strip()
    return Service(executable_path=driver) if driver else Service()


def _make_headless_driver():  # type: ignore[no-untyped-def]
    from selenium import webdriver

    return webdriver.Chrome(options=_chrome_options(), service=_chrome_service())


@pytest.fixture(scope="module")
def driver():  # type: ignore[no-untyped-def]
    try:
        drv = _make_headless_driver()
    except Exception as exc:  # noqa: BLE001 - any driver startup issue
        if _browser_required():
            pytest.fail(
                f"Chrome is required here but could not start: {exc}. "
                "These tests are the only coverage of the async booking chain, so a "
                "missing browser fails the run rather than shrinking the suite silently. "
                "Set ALLOW_BROWSER_TEST_SKIP=1 to downgrade this back to a skip."
            )
        pytest.skip(f"Chrome not available: {exc}")
    drv.set_script_timeout(30)
    yield drv
    drv.quit()


@pytest.fixture
def provider() -> WaldenGolfProvider:
    prov = WaldenGolfProvider.__new__(WaldenGolfProvider)  # skip __init__ (no creds needed)
    prov._capture_diagnostic_info = MagicMock()  # type: ignore[method-assign]
    return prov


def load_page(driver, **params: int | str) -> None:  # type: ignore[no-untyped-def]
    query = "&".join(f"{k}={v}" for k, v in params.items())
    driver.get(FIXTURE.resolve().as_uri() + (f"?{query}" if query else ""))
    time.sleep(0.2)  # let the page's scripts install their handlers


class TestTimedChain:
    def test_wins_race_with_disable_div_removed_by_page_timer(self, driver, provider) -> None:
        """The chain must click within ~ms of the page's own timer removing the gate."""
        load_page(driver, enableAfter=400, playerDelay=300)
        target_ms = int((time.time() + 1.0) * 1000)

        result = provider._stage_timed_booking_chain_js(driver, 0, 4, target_ms)

        assert result["success"], f"chain failed: {result}"
        assert result["phase"] == "complete"
        timing = result["timing"]
        assert timing["disableDivPresentAtStart"] is False or timing["slotsEnabledAfterWait"]
        assert abs(timing["clickDriftMs"]) < DRIFT_TOLERANCE_MS
        assert driver.execute_script("return window.bookNowClicked") is True
        log = driver.execute_script("return window.testLog")
        assert not any("CLICKED WHILE DISABLED" in entry for entry in log)

    def test_click_waits_for_enable_when_timer_fires_after_target(self, driver, provider) -> None:
        """Gate removal ~300ms AFTER the target: chain must wait, then click.

        The page's enable timer starts at load; load_page sleeps 0.2s, and the
        target is 1.0s after that, so the 1500ms timer fires roughly 300ms
        past the target.
        """
        load_page(driver, enableAfter=1500, playerDelay=200)
        target_ms = int((time.time() + 1.0) * 1000)

        result = provider._stage_timed_booking_chain_js(driver, 0, 2, target_ms)

        assert result["success"], f"chain failed: {result}"
        timing = result["timing"]
        assert timing["disableDivPresentAtStart"] is True
        assert timing["slotsEnabledAfterWait"] is True
        # Waited for the overlay, then clicked. Loose bound for CI noise.
        assert timing["disableDivWaitMs"] < 700

    def test_blocked_popup_detected(self, driver, provider) -> None:
        load_page(driver, enableAfter=100, blocked=1)
        target_ms = int((time.time() + 0.5) * 1000)

        result = provider._stage_timed_booking_chain_js(driver, 0, 4, target_ms)

        assert not result["success"]
        assert result["blocked"] is True


class TestFastChain:
    def test_completes_full_flow(self, driver, provider) -> None:
        load_page(driver, enableAfter=0, playerDelay=400)
        time.sleep(0.2)  # allow the enable timer to fire before the fast path

        result = provider._execute_fast_booking_chain_js(driver, 1, 4)

        assert result["success"], f"chain failed: {result}"
        assert driver.execute_script("return window.bookNowClicked") is True
        # All three TBD guests filled
        log = driver.execute_script("return window.testLog")
        assert sum("filled" in entry for entry in log) == 3

    def test_preselected_player_count_not_toggled(self, driver, provider) -> None:
        """When the page restores the player count, clicking again would deselect it."""
        load_page(driver, enableAfter=0, playerDelay=200, preselect=1)
        time.sleep(0.2)

        result = provider._execute_fast_booking_chain_js(driver, 0, 4)

        assert result["success"], f"chain failed: {result}"
        assert result["timing"].get("playerCountAlreadySelected") is True

    def test_single_player_skips_tbd_phase(self, driver, provider) -> None:
        load_page(driver, enableAfter=0, playerDelay=200)
        time.sleep(0.2)

        result = provider._execute_fast_booking_chain_js(driver, 0, 1)

        assert result["success"], f"chain failed: {result}"
        assert "tbdGuestsAdded" not in result["timing"]

    def test_missing_slot_index_errors_cleanly(self, driver, provider) -> None:
        load_page(driver, enableAfter=0)

        result = provider._execute_fast_booking_chain_js(driver, 99, 4)

        assert not result["success"]
        assert "Slot item not found" in result["error"]
