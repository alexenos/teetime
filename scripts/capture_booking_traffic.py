"""
Capture the network traffic of a Walden Golf booking flow via Chrome DevTools.

Purpose: map the JSF/PrimeFaces request sequence (login -> tee sheet ->
Reserve click -> player info -> Book Now) so we can replay it over direct
HTTP at 6:30:00 without a browser on the critical path.

Usage (requires WALDEN_MEMBER_NUMBER / WALDEN_PASSWORD in env or .env):

    poetry run python scripts/capture_booking_traffic.py --duration 300

The script logs in, navigates to the tee time page, then keeps recording for
--duration seconds while you interact with the page manually (click Reserve
on a throwaway slot, walk through player selection, and CANCEL before
confirming unless you actually want the booking). Every request/response --
URL, method, headers, POST body, status -- is written to a JSON file, along
with cookies and all javax.faces.ViewState values seen.

Output: scripts/captures/traffic_<timestamp>.json
"""

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

load_dotenv()

from app.config import settings  # noqa: E402
from app.providers.walden_provider import WaldenGolfProvider  # noqa: E402

OUTPUT_DIR = Path(__file__).parent / "captures"


def build_driver() -> webdriver.Chrome:
    options = Options()
    # Headful so you can drive the booking flow manually while we record.
    options.add_argument("--window-size=1600,1000")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    # Capture request POST data too.
    options.add_experimental_option("perfLoggingPrefs", {"enableNetwork": True})
    return webdriver.Chrome(options=options)


def drain_performance_log(driver: webdriver.Chrome, events: list[dict]) -> None:
    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except (KeyError, json.JSONDecodeError):
            continue
        if msg.get("method", "").startswith("Network."):
            msg["_wallTimeMs"] = entry.get("timestamp")
            events.append(msg)


_SENSITIVE_PARAM_RE = re.compile(r"([^&=]*(?:password|passwd|pwd)[^&=]*)=[^&]*", re.IGNORECASE)
_SENSITIVE_HEADERS = {"cookie", "authorization", "set-cookie"}


def redact_body(url: str, body: str | None) -> str | None:
    """Strip credentials from a captured request body before persistence."""
    if body is None:
        return None
    if "login" in url.lower():
        # Never persist the login POST body (contains the plaintext password).
        return "<redacted: login request>"
    return _SENSITIVE_PARAM_RE.sub(r"\1=<redacted>", body)


def fetch_post_bodies(driver: webdriver.Chrome, events: list[dict]) -> None:
    """Ask CDP for request POST data of interesting requests (best effort)."""
    for ev in events:
        if ev.get("method") != "Network.requestWillBeSent":
            continue
        req = ev.get("params", {}).get("request", {})
        if req.get("method") == "POST" and not req.get("postData"):
            request_id = ev["params"].get("requestId")
            try:
                body = driver.execute_cdp_cmd(
                    "Network.getRequestPostData", {"requestId": request_id}
                )
                req["postData"] = body.get("postData")
            except Exception:  # noqa: BLE001 - body may be gone; fine
                pass


def sanitize_events(events: list[dict]) -> None:
    """Redact credentials and session tokens from captured network events."""
    for ev in events:
        req = ev.get("params", {}).get("request")
        if not req:
            continue
        req["postData"] = redact_body(req.get("url", ""), req.get("postData"))
        headers = req.get("headers")
        if headers:
            for name in list(headers):
                if name.lower() in _SENSITIVE_HEADERS:
                    headers[name] = "<redacted>"


def extract_viewstates(driver: webdriver.Chrome) -> list[str]:
    return driver.execute_script(
        "return Array.from(document.querySelectorAll("
        "\"input[name='javax.faces.ViewState']\")).map(i => i.value);"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Walden booking-flow network traffic")
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Seconds to keep recording after login for manual interaction",
    )
    args = parser.parse_args()

    if not settings.walden_member_number or not settings.walden_password:
        raise SystemExit("Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD (e.g. in .env) first.")

    OUTPUT_DIR.mkdir(mode=0o700, exist_ok=True)
    events: list[dict] = []

    provider = WaldenGolfProvider()
    driver = build_driver()
    try:
        print("Logging in ...")
        if not provider._perform_login(driver):  # noqa: SLF001 - reuse the battle-tested login flow
            raise SystemExit("Login failed - check credentials.")
        drain_performance_log(driver, events)
        print("Navigating to tee time page ...")
        driver.get(WaldenGolfProvider.TEE_TIME_URL)
        time.sleep(3)
        drain_performance_log(driver, events)

        print(
            f"\nRecording for {args.duration}s - drive the booking flow manually now.\n"
            "Click Reserve on a slot, step through player selection, then CANCEL\n"
            "(unless you want to keep the booking). Ctrl+C to stop early.\n"
        )
        deadline = time.time() + args.duration
        try:
            while time.time() < deadline:
                time.sleep(2)
                drain_performance_log(driver, events)
        except KeyboardInterrupt:
            print("Stopping early.")
        drain_performance_log(driver, events)
        fetch_post_bodies(driver, events)
        sanitize_events(events)

        snapshot = {
            "captured_at": datetime.now().isoformat(),
            # Keep cookie names/attributes (needed to design the replay);
            # drop the live session-token values.
            "cookies": [{**cookie, "value": "<redacted>"} for cookie in driver.get_cookies()],
            "current_url": driver.current_url,
            "viewstates_on_final_page": extract_viewstates(driver),
            "network_events": events,
        }
    finally:
        driver.quit()

    out = OUTPUT_DIR / f"traffic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    out.chmod(0o600)  # best effort; no-op semantics on Windows, effective on POSIX
    print(f"\nWrote {len(events)} network events to {out}")
    print("Grep for 'reserve_button' and 'javax.faces.ViewState' to find the booking POSTs.")


if __name__ == "__main__":
    main()
