"""
Integration tests for the async booking chain against a synthetic page.

The fixture page (tests/fixtures/async_chain_test_page.html) reproduces the
event-loop dynamics of the real Walden tee sheet: the 'disable-div' gate is
removed by a page timer, and the player page renders via async callbacks -
none of which can happen while a script blocks the event loop. These tests
would all fail (timeout waiting for disable-div) under the old synchronous
spin-wait implementation.

Requires Chrome; tests are skipped automatically when it isn't available.
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


def _make_headless_driver():  # type: ignore[no-untyped-def]
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    return webdriver.Chrome(options=options)


@pytest.fixture(scope="module")
def driver():  # type: ignore[no-untyped-def]
    try:
        drv = _make_headless_driver()
    except Exception as exc:  # noqa: BLE001 - any driver startup issue -> skip
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
