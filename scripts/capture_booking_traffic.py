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


def fetch_post_bodies(driver: webdriver.Chrome, events: list[dict]) -> dict[str, int]:
    """Ask CDP for the body of each POST the log left without one (best effort).

    A body that cannot be fetched does not stop the capture, but it is counted,
    and the error's type is kept on that request as ``postDataError`` (#474). A
    reply with no body is counted the same way, as ``"empty"``. Which errors
    Chrome raises here for a body it has dropped, and for a lost session, has
    not been observed; the type name may not tell them apart.

    Returns:
        ``{"attempted": n, "fetched": n, "failed": n}``.
    """
    counts = {"attempted": 0, "fetched": 0, "failed": 0}
    for ev in events:
        if ev.get("method") != "Network.requestWillBeSent":
            continue
        req = ev.get("params", {}).get("request", {})
        if req.get("method") == "POST" and not req.get("postData"):
            request_id = ev["params"].get("requestId")
            counts["attempted"] += 1
            try:
                body = driver.execute_cdp_cmd(
                    "Network.getRequestPostData", {"requestId": request_id}
                )
            except Exception as exc:  # noqa: BLE001 - best effort; counted and reported
                counts["failed"] += 1
                req["postDataError"] = type(exc).__name__
                continue
            post_data = (body or {}).get("postData")
            if not post_data:
                counts["failed"] += 1
                req["postDataError"] = "empty"
                continue
            req["postData"] = post_data
            counts["fetched"] += 1
    return counts


def describe_post_body_fetch(counts: dict[str, int]) -> str:
    """One line for the operator: how many missing POST bodies were recovered."""
    if counts["attempted"] == 0:
        return "POST bodies: none were missing from the log."
    line = f"POST bodies: fetched {counts['fetched']} of {counts['attempted']} missing from the log"
    if counts["failed"]:
        line += (
            f"; {counts['failed']} could not be fetched and are absent from this "
            "capture (see postDataError on those requests)"
        )
    return line + "."


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

    provider = WaldenGolfProvider(settings.walden_member_number, settings.walden_password)
    driver = build_driver()
    try:
        print("Logging in ...")
        if not provider._perform_login(driver):  # reuse the battle-tested login flow
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
        post_body_fetch = fetch_post_bodies(driver, events)
        sanitize_events(events)

        snapshot = {
            "captured_at": datetime.now().isoformat(),
            # Keep cookie names/attributes (needed to design the replay);
            # drop the live session-token values.
            "cookies": [{**cookie, "value": "<redacted>"} for cookie in driver.get_cookies()],
            "current_url": driver.current_url,
            "viewstates_on_final_page": extract_viewstates(driver),
            "post_body_fetch": post_body_fetch,
            "network_events": events,
        }
    finally:
        driver.quit()

    out = OUTPUT_DIR / f"traffic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    out.chmod(0o600)  # best effort; no-op semantics on Windows, effective on POSIX
    print(f"\nWrote {len(events)} network events to {out}")
    print(describe_post_body_fetch(snapshot["post_body_fetch"]))
    print("Grep for 'reserve_button' and 'javax.faces.ViewState' to find the booking POSTs.")


if __name__ == "__main__":
    main()
