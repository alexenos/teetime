"""
Prove the direct-HTTP Walden login check against the live site (issue #241).

The check in app/providers/walden_http_login.py has only ever run against
saved pages. This runs it for real, two ways:

compare (default)
    Runs the HTTP check, then the proven Selenium login, with the same
    credentials, and prints both outcomes and how long each took. They should
    agree: ACCEPTED from HTTP where Selenium returns True.

capture-rejected
    Makes ONE deliberate failed login - the real login with a random wrong
    password - and saves the page Walden returns, sanitized, to
    tests/fixtures/walden_login_rejected.html. That is the only way to pin the
    REJECTED classification to real markup: no failed-login page has been seen.
    Walden's lockout policy is unknown, so this makes exactly one attempt and
    should not be repeated casually. Read the saved file before committing it.

Credentials come from WALDEN_MEMBER_NUMBER / WALDEN_PASSWORD in the environment
or .env, the same as scripts/probe_direct_http.py. Neither is printed.

Usage:

    poetry run python scripts/probe_http_login.py
    poetry run python scripts/probe_http_login.py capture-rejected
"""

import argparse
import asyncio
import re
import secrets
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.log_safety import silence_wire_loggers  # noqa: E402
from app.providers.walden_http_login import (  # noqa: E402
    LoginOutcome,
    check_login,
    check_login_with_page,
    signed_in_state,
)

REJECTED_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "walden_login_rejected.html"
)


def _credentials() -> tuple[str, str]:
    login, password = settings.walden_member_number, settings.walden_password
    if not login or not password:
        raise SystemExit("Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD (env or .env).")
    return login, password


async def _compare() -> int:
    login, password = _credentials()

    started = time.perf_counter()
    check = await check_login(login, password)
    http_s = time.perf_counter() - started
    print(f"HTTP check:     {check.outcome.value:8}  {http_s:5.1f}s  ({check.reason})")

    # Imported here: it pulls in Selenium, which the capture mode does not need.
    from app.providers.walden_provider import WaldenGolfProvider

    started = time.perf_counter()
    accepted = await WaldenGolfProvider(login, password).login()
    selenium_s = time.perf_counter() - started
    print(f"Selenium login: {'accepted' if accepted else 'failed':8}  {selenium_s:5.1f}s")

    agree = (check.outcome is LoginOutcome.ACCEPTED) == accepted
    print("AGREE" if agree else "DISAGREE - do not ship the HTTP check until this is understood")
    return 0 if agree else 1


def sanitize(html: str, login: str, password: str) -> str:
    """Strip what identifies the account or the session from a captured page."""
    out = html.replace(password, "REDACTED-PASSWORD").replace(login, "REDACTED-LOGIN")
    out = re.sub(r"p_auth=[A-Za-z0-9]+", "p_auth=REDACTEDTOKEN", out)
    out = re.sub(r"authToken\s*=\s*'[^']*'", "authToken = 'REDACTEDTOKEN'", out)
    out = re.sub(r"(jsessionid=)[A-Za-z0-9.]+", r"\1REDACTED", out, flags=re.IGNORECASE)
    # The page footer names club staff by email; people's identifiers stay out
    # of the repository (CLAUDE.md).
    out = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "redacted@example.com", out)
    return out


async def _capture_rejected() -> int:
    login, password = _credentials()
    wrong = f"definitely-wrong-{secrets.token_hex(8)}"

    check, page = await check_login_with_page(login, wrong)
    print(f"HTTP check with a wrong password: {check.outcome.value} ({check.reason})")
    if page is None:
        print("No page came back to save.")
        return 1
    if signed_in_state(page) is True:
        print("Walden reported a signed-in session for a wrong password. Saving nothing.")
        return 1

    saved = sanitize(page, login, wrong)
    for secret in (login, wrong, password):
        if secret in saved:
            print("Sanitizing left a credential in the page. Saving nothing.")
            return 1
    REJECTED_FIXTURE.write_text(saved, encoding="utf-8")
    print(f"Saved {len(saved):,} bytes to {REJECTED_FIXTURE}")
    print(
        "Read it before committing. If the outcome above is 'unknown', the error markup "
        "differs from stock Liferay - update _ERROR_CLASSES in walden_http_login.py to match."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "mode", nargs="?", choices=("compare", "capture-rejected"), default="compare"
    )
    args = parser.parse_args()
    silence_wire_loggers()
    return asyncio.run(_capture_rejected() if args.mode == "capture-rejected" else _compare())


if __name__ == "__main__":
    sys.exit(main())
