"""Tests for the direct-HTTP Walden login check (issue #241).

All three pages are real captures in tests/fixtures: the signed-out login page
and the signed-in home page (2026-02-01), and the page Walden returned for a
deliberately wrong password (walden_login_rejected.html, 2026-10-03, captured
with scripts/probe_http_login.py capture-rejected and sanitized).
"""

import asyncio
import logging
import re
import urllib.parse
from pathlib import Path

import httpx
import pytest

from app.providers import walden_http_login
from app.providers.walden_http_login import (
    LoginOutcome,
    check_login,
    classify_response,
    parse_login_form,
    signed_in_state,
)

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_PAGE = (FIXTURES / "walden_login_page.html").read_text(encoding="utf-8")
SIGNED_IN_PAGE = (FIXTURES / "walden_post_login.html").read_text(encoding="utf-8")
BASE = "https://www.waldengolf.com"

LOGIN = "M-unit-test-login"
PASSWORD = "unit-test-password-42"

REJECTED_PAGE = (FIXTURES / "walden_login_rejected.html").read_text(encoding="utf-8")


def test_rejected_capture_is_what_it_claims() -> None:
    """Signed out, the login form again, Walden's own error, nobody's address."""
    assert signed_in_state(REJECTED_PAGE) is False
    assert parse_login_form(REJECTED_PAGE, BASE) is not None
    assert "Authentication failed. Please try again." in REJECTED_PAGE
    emails = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", REJECTED_PAGE))
    assert emails <= {"redacted@example.com"}


class TestSignedInState:
    def test_login_page_is_signed_out(self) -> None:
        assert signed_in_state(LOGIN_PAGE) is False

    def test_home_page_is_signed_in(self) -> None:
        assert signed_in_state(SIGNED_IN_PAGE) is True

    def test_page_without_the_marker(self) -> None:
        assert signed_in_state("<html><body>Maintenance</body></html>") is None


class TestParseLoginForm:
    def test_reads_action_and_hidden_fields(self) -> None:
        form = parse_login_form(LOGIN_PAGE, f"{BASE}/web/pages/login")
        assert form is not None
        assert form.action.startswith(f"{BASE}/web/pages/login?")
        assert "p_auth=e9Gaodto" in form.action
        # &amp; in the markup is decoded, not sent literally.
        assert "&amp;" not in form.action
        names = [name for name, _ in form.fields]
        prefix = "_com_liferay_login_web_portlet_LoginPortlet_"
        assert f"{prefix}formDate" in names
        assert f"{prefix}checkboxNames" in names
        # Unticked checkboxes are not submitted, as in a browser.
        assert f"{prefix}rememberMe" not in names
        assert form.login_field == f"{prefix}login"
        assert form.password_field == f"{prefix}password"

    def test_no_form(self) -> None:
        assert parse_login_form(SIGNED_IN_PAGE, BASE) is None

    def test_form_without_csrf_token_is_not_usable(self) -> None:
        assert parse_login_form(LOGIN_PAGE.replace("p_auth=e9Gaodto", ""), BASE) is None


class TestClassifyResponse:
    def test_signed_in_is_accepted(self) -> None:
        assert classify_response(200, SIGNED_IN_PAGE, BASE).outcome is LoginOutcome.ACCEPTED

    def test_login_form_with_error_is_rejected(self) -> None:
        assert classify_response(200, REJECTED_PAGE, BASE).outcome is LoginOutcome.REJECTED

    def test_login_form_without_error_is_unknown(self) -> None:
        """Never say "wrong password" without seeing Walden say so."""
        assert classify_response(200, LOGIN_PAGE, BASE).outcome is LoginOutcome.UNKNOWN

    def test_unrecognised_page_is_unknown(self) -> None:
        assert classify_response(200, "<html>Down</html>", BASE).outcome is LoginOutcome.UNKNOWN

    def test_server_error_is_unknown_even_with_an_error_alert(self) -> None:
        assert classify_response(503, REJECTED_PAGE, BASE).outcome is LoginOutcome.UNKNOWN

    def test_client_error_is_unknown(self) -> None:
        assert classify_response(403, REJECTED_PAGE, BASE).outcome is LoginOutcome.UNKNOWN

    def test_echoed_input_cannot_fake_an_error(self) -> None:
        """Error text typed by the member, echoed into the page, is not an alert."""
        echoed = LOGIN_PAGE.replace(
            "</form>", "<p>alert-danger Authentication failed</p></form>", 1
        )
        assert classify_response(200, echoed, BASE).outcome is LoginOutcome.UNKNOWN


class FakeWalden:
    """A MockTransport handler playing Walden, recording what it was sent."""

    def __init__(self, post_status: int = 302, post_page: str | None = None) -> None:
        self.post_status = post_status
        self.post_page = post_page
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/web/pages/login":
            return httpx.Response(200, text=LOGIN_PAGE)
        if request.method == "POST" and path == "/web/pages/login":
            if self.post_status == 302:
                return httpx.Response(302, headers={"Location": f"{BASE}/group/pages/home"})
            return httpx.Response(self.post_status, text=self.post_page or "")
        if request.method == "GET" and path == "/group/pages/home":
            return httpx.Response(200, text=SIGNED_IN_PAGE)
        if request.method == "GET" and path == "/c/portal/logout":
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


class TestCheckLogin:
    async def test_accepted_login(self) -> None:
        walden = FakeWalden()

        check = await check_login(LOGIN, PASSWORD, base_url=BASE, transport=walden.transport())

        assert check.outcome is LoginOutcome.ACCEPTED
        [post] = walden.posts
        assert "p_auth=e9Gaodto" in str(post.url)
        body = dict(urllib.parse.parse_qsl(post.content.decode()))
        prefix = "_com_liferay_login_web_portlet_LoginPortlet_"
        assert body[f"{prefix}login"] == LOGIN
        assert body[f"{prefix}password"] == PASSWORD
        assert body[f"{prefix}formDate"] == "1769984867797"
        assert "COOKIE_SUPPORT=true" in post.headers.get("cookie", "")
        # Signed out afterwards, leaving no session behind.
        assert walden.requests[-1].url.path == "/c/portal/logout"

    async def test_rejected_login(self) -> None:
        walden = FakeWalden(post_status=200, post_page=REJECTED_PAGE)

        check = await check_login(LOGIN, PASSWORD, base_url=BASE, transport=walden.transport())

        assert check.outcome is LoginOutcome.REJECTED
        assert not any(r.url.path == "/c/portal/logout" for r in walden.requests)

    async def test_server_error_on_login(self) -> None:
        walden = FakeWalden(post_status=503, post_page="Service Unavailable")
        check = await check_login(LOGIN, PASSWORD, base_url=BASE, transport=walden.transport())
        assert check.outcome is LoginOutcome.UNKNOWN

    async def test_login_page_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502)

        check = await check_login(
            LOGIN, PASSWORD, base_url=BASE, transport=httpx.MockTransport(handler)
        )
        assert check.outcome is LoginOutcome.UNKNOWN
        assert "502" in check.reason

    async def test_login_page_changed_shape(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>New site!</body></html>")

        check = await check_login(
            LOGIN, PASSWORD, base_url=BASE, transport=httpx.MockTransport(handler)
        )
        assert check.outcome is LoginOutcome.UNKNOWN

    async def test_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        check = await check_login(
            LOGIN, PASSWORD, base_url=BASE, transport=httpx.MockTransport(handler)
        )
        assert check.outcome is LoginOutcome.UNKNOWN

    async def test_slow_walden_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(walden_http_login, "TOTAL_TIMEOUT_S", 0.05)

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200, text=LOGIN_PAGE)

        check = await check_login(
            LOGIN, PASSWORD, base_url=BASE, transport=httpx.MockTransport(handler)
        )
        assert check.outcome is LoginOutcome.UNKNOWN
        assert "in time" in check.reason

    async def test_failed_sign_out_does_not_change_the_answer(self) -> None:
        walden = FakeWalden()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/c/portal/logout":
                raise httpx.ReadTimeout("slow")
            return walden(request)

        check = await check_login(
            LOGIN, PASSWORD, base_url=BASE, transport=httpx.MockTransport(handler)
        )
        assert check.outcome is LoginOutcome.ACCEPTED

    @pytest.mark.parametrize("login,password", [("", PASSWORD), (LOGIN, "")])
    async def test_empty_input_sends_nothing(self, login: str, password: str) -> None:
        walden = FakeWalden()
        check = await check_login(login, password, base_url=BASE, transport=walden.transport())
        assert check.outcome is LoginOutcome.REJECTED
        assert walden.requests == []


class TestLogSafety:
    """The password must never reach a log line, whatever the outcome."""

    @pytest.mark.parametrize(
        "walden",
        [
            FakeWalden(),
            FakeWalden(post_status=200, post_page=REJECTED_PAGE),
            FakeWalden(post_status=500, post_page=f"<pre>echo: {PASSWORD}</pre>"),
        ],
        ids=["accepted", "rejected", "server-error-echoing-input"],
    )
    async def test_password_not_logged(
        self, walden: FakeWalden, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        check = await check_login(LOGIN, PASSWORD, base_url=BASE, transport=walden.transport())

        assert PASSWORD not in caplog.text
        assert PASSWORD not in check.reason
        assert LOGIN not in check.reason
