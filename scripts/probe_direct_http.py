"""
Prove out - and time - the direct-HTTP path against the live Walden site.

This is the safe way to answer "is direct HTTP actually faster here?" without
booking anything. It logs in with Selenium exactly as the booking job does,
hands the session to :class:`PrimeFacesSession`, then exercises a **read-only**
PrimeFaces component (the tee sheet's "Legends" dialog) over both transports and
compares round-trip times.

What it verifies
    * the adopted cookie jar authenticates a direct POST
    * the ViewState, encodedURL and namespaced parameters are accepted
    * the server answers with a real <partial-response> and a fresh ViewState
    * how long the same interaction takes through Chrome vs. through httpx

What it does NOT do
    It never clicks Reserve and never submits a booking. The only component it
    touches opens an informational dialog.

Usage (needs WALDEN_MEMBER_NUMBER / WALDEN_PASSWORD in env or .env):

    poetry run python scripts/probe_direct_http.py
    poetry run python scripts/probe_direct_http.py --samples 10
"""

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.providers.walden_http import (  # noqa: E402
    AbConfig,
    DirectHttpError,
    PrimeFacesSession,
    find_ab_for_element,
    parse_html,
)
from app.providers.walden_provider import WaldenGolfProvider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("probe")

# A component with no side effects: it opens the tee sheet's legend dialog.
# Nothing here reserves, cancels or modifies anything.
SAFE_COMPONENT_SUFFIX = "showLegends"


def find_safe_component(page_html: str) -> tuple[str, AbConfig]:
    """Locate the read-only component this probe will exercise."""
    document = parse_html(page_html)
    for node in document.descendants():
        if node.id.endswith(SAFE_COMPONENT_SUFFIX):
            config = find_ab_for_element(node, page_html)
            if config is not None:
                return node.id, config
    raise DirectHttpError(
        f"No safe probe component (*:{SAFE_COMPONENT_SUFFIX}) found on the tee sheet"
    )


def time_direct_http(session: PrimeFacesSession, config: AbConfig, samples: int) -> list[float]:
    """Time the component's request over direct HTTP."""
    timings = []
    body = session.build_body(config)
    for i in range(samples):
        started = time.perf_counter()
        response = session.post(config, body=body)
        elapsed_ms = (time.perf_counter() - started) * 1000
        timings.append(elapsed_ms)
        logger.info(
            "  direct  #%d: %6.1f ms  (%d update(s), viewState=%s)",
            i + 1,
            elapsed_ms,
            len(response.updates),
            "refreshed" if response.view_state else "unchanged",
        )
        # The ViewState may have rolled; re-serialize for the next sample.
        body = session.build_body(config)
    return timings


_BROWSER_TIMING_JS = """
var cfg = arguments[0];
var done = arguments[arguments.length - 1];
var t0 = performance.now();
PrimeFaces.ab({
    s: cfg.source, f: cfg.form, p: cfg.process, u: cfg.update,
    onco: function() { done(performance.now() - t0); }
});
"""


def time_browser(driver: Any, config: AbConfig, samples: int) -> list[float]:
    """Time the identical request as the browser performs it.

    Measured from issuing the PrimeFaces call to its ``oncomplete`` - so it
    covers the request, the response, and the DOM update the browser has to
    apply before the next step could run.
    """
    timings = []
    for i in range(samples):
        elapsed_ms = float(
            driver.execute_async_script(
                _BROWSER_TIMING_JS,
                {
                    "source": config.source,
                    "form": config.form,
                    "process": config.process,
                    "update": config.update,
                },
            )
        )
        timings.append(elapsed_ms)
        logger.info("  browser #%d: %6.1f ms", i + 1, elapsed_ms)
    return timings


def summarize(label: str, timings: list[float]) -> dict[str, float]:
    """Log and return min/median/mean/max for one transport's samples."""
    stats = {
        "min": min(timings),
        "median": statistics.median(timings),
        "mean": statistics.fmean(timings),
        "max": max(timings),
    }
    logger.info(
        "%-8s min=%.1fms median=%.1fms mean=%.1fms max=%.1fms",
        label,
        stats["min"],
        stats["median"],
        stats["mean"],
        stats["max"],
    )
    return stats


def main() -> int:
    """Run the probe end to end; returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples", type=int, default=5, help="round trips per transport (minimum 1)"
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=settings.days_in_advance,
        help="tee sheet date to load (default: the booking horizon)",
    )
    parser.add_argument("--json", type=Path, help="write the timing summary here")
    args = parser.parse_args()

    if args.samples < 1:
        parser.error("--samples must be at least 1")

    if not settings.walden_member_number or not settings.walden_password:
        logger.error("WALDEN_MEMBER_NUMBER / WALDEN_PASSWORD are not set")
        return 2

    provider = WaldenGolfProvider()
    driver = provider._create_driver()
    try:
        logger.info("Logging in...")
        if not provider._perform_login(driver):
            logger.error("Login failed")
            return 1

        logger.info("Loading the tee sheet...")
        driver.get(provider.TEE_TIME_URL)
        WebDriverWait(driver, 20).until(
            expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form"))
        )
        provider._select_course_sync(driver, provider.NORTHGATE_COURSE_NAME)
        provider._select_date_sync(driver, date.today() + timedelta(days=args.days_ahead))

        page_html = driver.page_source
        component_id, config = find_safe_component(page_html)
        logger.info("Probing read-only component: %s", component_id)

        logger.info("Adopting the browser session over HTTP...")
        session = PrimeFacesSession.from_selenium(driver)
        rtt_ms = session.warm_up()

        try:
            logger.info("Direct HTTP (%d samples):", args.samples)
            direct = time_direct_http(session, config, args.samples)
            logger.info("Browser (%d samples):", args.samples)
            browser = time_browser(driver, config, args.samples)
        finally:
            session.close()

        logger.info("--- results ---")
        summary = {
            "component": component_id,
            "warmup_rtt_ms": rtt_ms,
            "direct_http": summarize("direct", direct),
            "browser": summarize("browser", browser),
        }
        speedup = summary["browser"]["median"] / summary["direct_http"]["median"]
        summary["median_speedup"] = speedup
        logger.info("Direct HTTP is %.1fx faster per interaction (median)", speedup)
        logger.info(
            "A 4-player booking is 6 interactions: ~%.0fms direct vs ~%.0fms browser",
            summary["direct_http"]["median"] * 6,
            summary["browser"]["median"] * 6,
        )

        if args.json:
            args.json.write_text(json.dumps(summary, indent=2))
            logger.info("Wrote %s", args.json)
        return 0

    except DirectHttpError as exc:
        logger.error("Direct-HTTP probe failed: %s", exc)
        return 1
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
