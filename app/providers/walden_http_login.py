"""
Check a Walden login over direct HTTP, without a browser (issue #241).

The setup form (#240) has to check a member's login before saving it, while
the member waits. The proven Selenium login (WaldenGolfProvider.login) starts
Chrome, loads pages, and queues behind the service's single browser slot, which
costs tens of seconds (15.1s measured on 2026-10-03). This does the same
check with plain HTTP requests, about 1.5s warm.

It also answers in three states, where the Selenium login answers in two.
_perform_login returns False both when Walden rejects the password and when a
page times out, but "wrong password" and "Walden didn't answer" ask different
things of the member, so they must not share an answer:

* ACCEPTED - the final page reports a signed-in session.
* REJECTED - Walden showed its login form again, signed out, with an error.
* UNKNOWN - anything else: a network error, a timeout, a 5xx, or a page that
  matches neither of the above. Never guessed to be REJECTED, because telling
  a member their correct password is wrong is worse than asking them to retry.

What the fixtures establish (tests/fixtures/walden_login_page.html and
walden_post_login.html, captured 2026-02-01): a standard Liferay login portlet.
The form posts to an action URL carrying a per-session CSRF token (p_auth),
with fields namespaced _com_liferay_login_web_portlet_LoginPortlet_. Neither
page has captcha markup. Liferay's theme script reports the session as
``isSignedIn: function() { return false }`` on the login page and ``true`` on
the signed-in home page - a success marker that holds however the redirects
behave. The form action and hidden fields are parsed from the page rather than
hardcoded, the same principle walden_http.py follows, so a changed token or an
added field does not break this.

A rejected login (tests/fixtures/walden_login_rejected.html, captured
2026-10-03 with one deliberate wrong password) comes back as the login page
again, signed out, with stock Liferay error markup: an ``alert-danger`` element
reading "Authentication failed. Please try again." REJECTED requires all of
those, so a page that is merely unfamiliar is UNKNOWN.

Log safety: the POST body carries the password, so nothing here logs a request
body, a response body, or an exception that could contain either. httpx's own
INFO line logs the request URL only, which carries p_auth (a CSRF token, not a
credential).
"""

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum

import httpx

from app.config import settings
from app.providers.walden_dom_schema import DOM
from app.providers.walden_http import Node, parse_html

logger = logging.getLogger(__name__)

LOGIN_PATH = "/web/pages/login"
LOGOUT_PATH = "/c/portal/logout"
LOGIN_FORM_ID = "_com_liferay_login_web_portlet_LoginPortlet_loginForm"

# Whole check, including the sign-out. The member is waiting on it. Measured
# against the live site on 2026-10-03: five requests (login page, POST, two
# redirects, sign-out), about 1.5s warm. One cold run from a workstation took
# 8.7s, unexplained, so the cap leaves room above that rather than cutting off
# a slow but valid login as "couldn't check".
TOTAL_TIMEOUT_S = 15.0
REQUEST_TIMEOUT_S = 5.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# themeDisplay.isSignedIn() as Liferay's theme script renders it.
_SIGNED_IN = re.compile(r"isSignedIn\s*:\s*function\s*\(\s*\)\s*\{\s*return\s+(true|false)")

# Error markup for a failed login. Walden uses stock Liferay's alert-danger
# (confirmed by the 2026-10-03 capture); the other two are older Liferay
# spellings. Matched as classes on an element, never as page text, so the
# member's own input echoed into the page cannot satisfy it.
_ERROR_CLASSES = ("alert-danger", "portlet-msg-error", "alert-error")


class LoginOutcome(str, Enum):
    """What a login check established."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoginCheck:
    """The result of one login check.

    ``reason`` is written by this module, never copied from the page, so it is
    always safe to log and to show the member.
    """

    outcome: LoginOutcome
    reason: str


@dataclass(frozen=True)
class _LoginForm:
    action: str
    fields: list[tuple[str, str]]
    login_field: str
    password_field: str


def signed_in_state(html: str) -> bool | None:
    """What the page's theme script says about the session, or None if it says nothing."""
    match = _SIGNED_IN.search(html)
    if match is None:
        return None
    return match.group(1) == "true"


def parse_login_form(html: str, base_url: str) -> _LoginForm | None:
    """The login form's action and hidden fields, or None if the page has no login form.

    Hidden inputs are submitted as the page set them (formDate, redirect,
    checkboxNames, ...). Checkboxes are left unticked, as a person would leave
    them. The login and password fields are located by the names the Selenium
    login uses (DOM.LOGIN), so both paths agree on what the form is.
    """
    form = parse_html(html).find_by_id(LOGIN_FORM_ID)
    if form is None or form.tag != "form":
        return None
    action = form.attrs.get("action", "")
    if "p_auth=" not in action:
        # Without its CSRF token the POST is refused by Liferay, and the
        # refusal would look like a rejected login.
        return None

    login_field = DOM.LOGIN.member_input_name
    password_field = DOM.LOGIN.password_input_name
    names: set[str] = set()
    fields: list[tuple[str, str]] = []
    for node in form.descendants():
        if node.tag != "input":
            continue
        name = node.attrs.get("name", "")
        names.add(name)
        if name and node.attrs.get("type", "text").lower() == "hidden":
            fields.append((name, node.attrs.get("value", "")))
    if login_field not in names or password_field not in names:
        return None

    return _LoginForm(
        action=urllib.parse.urljoin(base_url, action),
        fields=fields,
        login_field=login_field,
        password_field=password_field,
    )


def _has_error_alert(root: Node) -> bool:
    return any(root.find_with_class(css_class) for css_class in _ERROR_CLASSES)


def classify_response(status_code: int, html: str, base_url: str) -> LoginCheck:
    """Decide what the page the login POST ended on says about the login."""
    if status_code >= 500:
        return LoginCheck(LoginOutcome.UNKNOWN, f"Walden answered HTTP {status_code}")
    if status_code >= 400:
        return LoginCheck(LoginOutcome.UNKNOWN, f"Walden refused the request (HTTP {status_code})")

    signed_in = signed_in_state(html)
    if signed_in is True:
        return LoginCheck(LoginOutcome.ACCEPTED, "signed in")
    if (
        signed_in is False
        and parse_login_form(html, base_url) is not None
        and _has_error_alert(parse_html(html))
    ):
        return LoginCheck(LoginOutcome.REJECTED, "Walden showed its login form again with an error")
    return LoginCheck(LoginOutcome.UNKNOWN, "the page after login was not recognised")


async def check_login(
    login: str,
    password: str,
    *,
    base_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LoginCheck:
    """Log in to Walden as this member over plain HTTP and report what happened.

    Signs out afterwards when the login was accepted, so no session is left
    behind. That is tidiness rather than necessity: one Walden account can
    hold several sessions at once (confirmed by the maintainer, 2026-10-02).

    Never raises for an outcome; a failure to reach Walden is UNKNOWN.
    """
    check, _ = await check_login_with_page(login, password, base_url=base_url, transport=transport)
    return check


async def check_login_with_page(
    login: str,
    password: str,
    *,
    base_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[LoginCheck, str | None]:
    """check_login, plus the HTML of the page the login POST ended on.

    For scripts/probe_http_login.py, which saves that page as a fixture so the
    REJECTED classification can be pinned to real markup. The page may echo
    the submitted login, so the caller must sanitize it before keeping it, and
    nothing in the application should call this.
    """
    if not login or not password:
        return LoginCheck(LoginOutcome.REJECTED, "the login or password was empty"), None
    base = (base_url or settings.walden_base_url).rstrip("/")
    try:
        return await asyncio.wait_for(
            _check(login, password, base, transport), timeout=TOTAL_TIMEOUT_S
        )
    except TimeoutError:
        return LoginCheck(LoginOutcome.UNKNOWN, "Walden did not answer in time"), None


async def _check(
    login: str, password: str, base: str, transport: httpx.AsyncBaseTransport | None
) -> tuple[LoginCheck, str | None]:
    async with httpx.AsyncClient(
        base_url=base,
        timeout=REQUEST_TIMEOUT_S,
        follow_redirects=True,
        transport=transport,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ) as client:
        # Liferay sets COOKIE_SUPPORT from a script, and refuses a login from a
        # client that has not set it. A browser would have run that script.
        client.cookies.set(
            "COOKIE_SUPPORT", "true", domain=urllib.parse.urlsplit(base).hostname or ""
        )

        try:
            page = await client.get(LOGIN_PATH)
        except httpx.HTTPError as exc:
            reason = f"could not reach Walden ({type(exc).__name__})"
            return LoginCheck(LoginOutcome.UNKNOWN, reason), None
        if page.status_code != 200:
            reason = f"the login page answered HTTP {page.status_code}"
            return LoginCheck(LoginOutcome.UNKNOWN, reason), None
        if signed_in_state(page.text) is True:
            # A fresh client has no session, so this would mean the page is not
            # what it was when the fixtures were captured.
            reason = "the login page already reported a session"
            return LoginCheck(LoginOutcome.UNKNOWN, reason), None

        form = parse_login_form(page.text, str(page.url))
        if form is None:
            reason = "the login page had no recognisable form"
            return LoginCheck(LoginOutcome.UNKNOWN, reason), None

        body = [*form.fields, (form.login_field, login), (form.password_field, password)]
        try:
            result = await client.post(
                form.action,
                content=urllib.parse.urlencode(body),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": base,
                    "Referer": str(page.url),
                },
            )
        except httpx.HTTPError as exc:
            # The exception names the request, never its body; still, only its
            # type is kept.
            reason = f"the login request failed ({type(exc).__name__})"
            return LoginCheck(LoginOutcome.UNKNOWN, reason), None

        check = classify_response(result.status_code, result.text, str(result.url))
        if check.outcome is LoginOutcome.ACCEPTED:
            try:
                await client.get(LOGOUT_PATH)
            except httpx.HTTPError:
                # The answer is already known; a session left behind is harmless.
                logger.info("Walden sign-out after a login check failed; ignoring")
        return check, result.text
