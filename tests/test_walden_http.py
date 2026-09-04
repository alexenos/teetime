"""
Tests for the direct-HTTP (browser-free) booking path.

The protocol tests run against the real captured tee sheet in
tests/fixtures/, so the request this builds is the request the site actually
expects - not one derived from a hand-written mock.
"""

import email.utils
import logging
import threading
import time as time_module
import urllib.parse
from collections.abc import Callable
from datetime import time
from pathlib import Path

import httpx
import pytest

from app.providers.walden_http import (
    AbConfig,
    DirectHttpError,
    DirectHttpStatusError,
    FormState,
    PrimeFacesSession,
    ViewExpiredError,
    _SuppressProbeRequestLog,
    find_ab_for_element,
    parse_ab_call,
    parse_html,
    parse_partial_response,
    sleep_until,
    visible_text,
)
from app.providers.walden_http_booker import (
    _BURST_WARMUP_MS,
    OPENING_MODE_BURST,
    PHASE_BOOK_NOW,
    PHASE_RESERVE_SENT,
    PHASE_RESERVE_STAGED,
    PHASE_VIEW_REFRESH,
    PRE_SUBMIT_PHASES,
    RESERVE_ACCEPTED,
    RESERVE_ERRORED,
    RESERVE_TIMEDOUT,
    DirectHttpBooker,
    _first_real_option_value,
    _parse_slot_time,
    _relocate_reserve,
    _relocate_reserve_in,
    find_response_message,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEE_SHEET = (FIXTURES / "walden_tee_time_loaded.html").read_text(encoding="utf-8", errors="replace")
# The refusal the site returned for a second same-day booking on 08/08/2026,
# lifted verbatim from the captured Book Now response with the member's name
# replaced. This is what a routine refusal looks like: no error class anywhere.
RESTRICTION_POPUP = (FIXTURES / "walden_restriction_popup.html").read_text(encoding="utf-8")

FORM_ID = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm"
RESERVE_ID = f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:reserve_button"
# The captured sheet's selected day tab, and the tee time of the slot RESERVE_ID
# sits in - both read off the fixture, not invented.
DAY_TAB_ID = f"{FORM_ID}:j_idt125:0:j_idt127"
RESERVE_SLOT_TIME = time(16, 34)


@pytest.fixture(scope="module")
def tee_sheet_form() -> FormState:
    """The captured tee sheet's form state, parsed once per module."""
    return FormState.from_html(TEE_SHEET)


def make_session(
    form_state: FormState,
    handler: Callable[[httpx.Request], httpx.Response],
) -> PrimeFacesSession:
    """Build a session whose transport is a MockTransport handler.

    No headers are set here on purpose: the session must apply its own protocol
    headers to an injected client, so what these tests exercise is what
    production sends.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PrimeFacesSession(
        form_state,
        {},
        base_url="https://www.waldengolf.com/group/pages/book-a-tee-time",
        client=client,
    )


def partial_response(body: str, view_state: str = "new-view-state") -> str:
    """Wrap markup in the partial-response envelope JSF returns."""
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<partial-response><changes>"
        f'<update id="{FORM_ID}"><![CDATA[{body}]]></update>'
        f'<update id="_teeTimePortlet_WAR_northstarportlet_:javax.faces.ViewState:0">'
        f"<![CDATA[{view_state}]]></update>"
        "</changes></partial-response>"
    )


class TestParseAbCall:
    """The PrimeFaces.ab parser, exercised against the real handlers."""

    def test_parses_reserve_button_handler(self) -> None:
        """The Reserve link's handler yields source, form and update targets."""
        config = parse_ab_call('PrimeFaces.ab({s:"btn-id",f:"form-id",u:"form-id"});return false;')
        assert config == AbConfig(source="btn-id", form="form-id", update="form-id")

    def test_resolves_s_this_to_element_id(self) -> None:
        """``s:this`` resolves to the id of the element carrying the handler."""
        config = parse_ab_call(
            'PrimeFaces.ab({s:this,e:"mousedown",f:"form-id",p:"link-id",u:"area-id"});',
            element_id="link-id",
        )
        assert config is not None
        assert config.source == "link-id"
        assert config.event == "mousedown"
        assert config.process == "link-id"

    def test_s_this_without_element_id_is_unresolvable(self) -> None:
        """``s:this`` cannot be resolved without knowing the element."""
        assert parse_ab_call('PrimeFaces.ab({s:this,f:"form-id"});') is None

    def test_ignores_snippet_without_source_and_form(self) -> None:
        """A call missing source or form is not a usable request."""
        assert parse_ab_call('PrimeFaces.ab({u:"only-update"});') is None

    def test_ignores_non_ab_javascript(self) -> None:
        """JavaScript with no PrimeFaces.ab call yields nothing."""
        assert parse_ab_call("showLoader(); return false;") is None

    def test_does_not_mistake_callback_keys_for_config_keys(self) -> None:
        """onst/onco callbacks contain 'f' and 's' inside identifiers."""
        config = parse_ab_call(
            'PrimeFaces.ab({s:"btn",f:"form",u:"form",'
            "onst:function(cfg){showLoader();},"
            "onco:function(xhr,status,args){hideLoader();}});"
        )
        assert config == AbConfig(source="btn", form="form", update="form")

    def test_parses_every_handler_in_the_real_tee_sheet(self) -> None:
        """No handler on the captured page defeats the parser."""
        document = parse_html(TEE_SHEET)
        handlers = [
            node
            for node in document.descendants()
            if "PrimeFaces.ab(" in node.attrs.get("onclick", "")
        ]
        assert len(handlers) > 20, "fixture should contain many ab handlers"
        for node in handlers:
            config = parse_ab_call(node.attrs["onclick"], element_id=node.id)
            assert config is not None, f"failed to parse handler on {node.id}"
            assert config.form == FORM_ID


class TestFormState:
    """Form serialization against the captured tee sheet."""

    def test_finds_the_tee_time_form(self, tee_sheet_form: FormState) -> None:
        """The form carrying a ViewState is the one selected."""
        assert tee_sheet_form.form_id == FORM_ID

    def test_posts_to_the_encoded_url_not_the_form_action(self, tee_sheet_form: FormState) -> None:
        """Liferay's bridge routes AJAX to encodedURL (lifecycle=2)."""
        assert "p_p_lifecycle=2" in tee_sheet_form.action_url
        assert "_jsfBridgeAjax=true" in tee_sheet_form.action_url

    def test_extracts_view_state(self, tee_sheet_form: FormState) -> None:
        """The ViewState is read from the form's hidden input."""
        assert tee_sheet_form.view_state == "3597409561974113966:-1460199378475720306"

    def test_serializes_the_state_the_browser_built_up(self, tee_sheet_form: FormState) -> None:
        """Course selection and date come across as form fields."""
        fields = dict(tee_sheet_form.fields)
        assert fields[f"{FORM_ID}:j_idt104_input"] == "02/01/2026"  # selected date
        assert fields[f"{FORM_ID}:maxDateInputField"] == "02/01/2027"
        # Course checkboxes: only the checked ones are submitted.
        assert (f"{FORM_ID}:j_idt101", "2") in tee_sheet_form.fields

    def test_skips_unchecked_and_disabled_controls(self) -> None:
        """Only successful controls are submitted, as a browser would."""
        state = FormState.from_html(
            '<form id="f" action="/post">'
            '<input type="hidden" name="javax.faces.ViewState" value="v">'
            '<input type="checkbox" name="on" value="1" checked>'
            '<input type="checkbox" name="off" value="2">'
            '<input type="text" name="dis" value="3" disabled>'
            '<input type="submit" name="btn" value="Go">'
            "</form>"
        )
        names = [name for name, _ in state.fields]
        assert "on" in names
        assert "off" not in names
        assert "dis" not in names
        assert "btn" not in names

    def test_serializes_selected_options_and_textareas(self) -> None:
        """Selects submit their chosen option; textareas submit their body."""
        state = FormState.from_html(
            '<form id="f" action="/post">'
            '<input type="hidden" name="javax.faces.ViewState" value="v">'
            '<select name="s"><option value="a">A</option>'
            '<option value="b" selected>B</option></select>'
            '<textarea name="t">hello</textarea>'
            "</form>"
        )
        assert ("s", "b") in state.fields
        assert ("t", "hello") in state.fields

    def test_rejects_page_without_a_jsf_form(self) -> None:
        """A page with no ViewState-bearing form cannot be posted to."""
        with pytest.raises(DirectHttpError, match="No JSF form"):
            FormState.from_html("<html><body><form id='x'></form></body></html>")

    def test_set_field_replaces_in_place(self, tee_sheet_form: FormState) -> None:
        """Replacing a field keeps the surrounding field order intact."""
        state = FormState(
            form_id="f", action_url="/post", fields=[("a", "1"), ("b", "2"), ("c", "3")]
        )
        state.set_field("b", "changed")
        assert state.fields == [("a", "1"), ("b", "changed"), ("c", "3")]

    def test_set_field_appends_when_absent(self) -> None:
        """A field the form did not carry is appended."""
        state = FormState(form_id="f", action_url="/post", fields=[("a", "1")])
        state.set_field("new", "v")
        assert state.fields == [("a", "1"), ("new", "v")]


class TestBuildBody:
    """The POST body is the one PrimeFaces would have sent."""

    def test_reserve_body_carries_the_partial_request_params(
        self, tee_sheet_form: FormState
    ) -> None:
        """The body carries the partial-request params and the form's state."""
        document = parse_html(TEE_SHEET)
        button = document.find_by_id(RESERVE_ID)
        assert button is not None
        config = find_ab_for_element(button, TEE_SHEET)
        assert config is not None

        session = make_session(tee_sheet_form, lambda request: httpx.Response(200))
        params = urllib.parse.parse_qsl(session.build_body(config).decode(), keep_blank_values=True)
        as_dict = dict(params)

        assert as_dict["javax.faces.partial.ajax"] == "true"
        assert as_dict["javax.faces.source"] == RESERVE_ID
        assert as_dict["javax.faces.partial.execute"] == "@all"
        assert as_dict["javax.faces.partial.render"] == FORM_ID
        assert as_dict["javax.faces.ViewState"] == tee_sheet_form.view_state
        # The activating control submits its own name so JSF decodes the action.
        assert as_dict[RESERVE_ID] == RESERVE_ID
        # And the form's own state rides along.
        assert as_dict[f"{FORM_ID}:j_idt104_input"] == "02/01/2026"

    def test_behavior_event_is_sent_for_change_handlers(self, tee_sheet_form: FormState) -> None:
        """A behavior-driven control sends its event name."""
        session = make_session(tee_sheet_form, lambda request: httpx.Response(200))
        config = AbConfig(source="s", form=FORM_ID, event="change", process="s")
        as_dict = dict(urllib.parse.parse_qsl(session.build_body(config).decode()))
        assert as_dict["javax.faces.behavior.event"] == "change"
        assert as_dict["javax.faces.partial.event"] == "change"
        assert as_dict["javax.faces.partial.execute"] == "s"

    def test_render_is_omitted_when_the_handler_declares_none(
        self, tee_sheet_form: FormState
    ) -> None:
        """No update target means no render parameter."""
        session = make_session(tee_sheet_form, lambda request: httpx.Response(200))
        body = session.build_body(AbConfig(source="s", form=FORM_ID)).decode()
        assert "javax.faces.partial.render" not in body

    def test_reserve_body_is_small(self, tee_sheet_form: FormState) -> None:
        """The race-critical payload is ~2KB, not a 700KB page render."""
        document = parse_html(TEE_SHEET)
        button = document.find_by_id(RESERVE_ID)
        assert button is not None
        config = find_ab_for_element(button, TEE_SHEET)
        assert config is not None
        session = make_session(tee_sheet_form, lambda request: httpx.Response(200))
        assert len(session.build_body(config)) < 4096


class TestPartialResponse:
    """Parsing the server's XML replies."""

    def test_extracts_updates_and_view_state(self) -> None:
        """Updates and the refreshed ViewState are both read out."""
        response = parse_partial_response(partial_response("<div>hi</div>", "vs-2"))
        assert response.view_state == "vs-2"
        assert "<div>hi</div>" in response.markup

    def test_view_state_update_is_not_part_of_markup(self) -> None:
        """The ViewState update is state, not renderable markup."""
        response = parse_partial_response(partial_response("<div>hi</div>", "vs-2"))
        assert "vs-2" not in response.markup

    def test_view_expired_raises_a_distinct_error(self) -> None:
        """An expired view is distinguishable from other server errors."""
        xml = (
            "<partial-response><error>"
            "<error-name>class javax.faces.application.ViewExpiredException</error-name>"
            "<error-message>View could not be restored</error-message>"
            "</error></partial-response>"
        )
        with pytest.raises(ViewExpiredError, match="ViewExpired"):
            parse_partial_response(xml)

    def test_other_server_errors_raise_direct_http_error(self) -> None:
        """Non-ViewState server errors surface with their Java class name."""
        xml = (
            "<partial-response><error>"
            "<error-name>java.lang.NullPointerException</error-name>"
            "<error-message>boom</error-message>"
            "</error></partial-response>"
        )
        with pytest.raises(DirectHttpError, match="NullPointerException"):
            parse_partial_response(xml)

    def test_html_login_page_is_reported_clearly(self) -> None:
        """A dead session returns HTML, not XML - say so rather than crash."""
        with pytest.raises(DirectHttpError, match="not a JSF partial-response"):
            parse_partial_response("<html><body>Please log in</body></html>")

    def test_captures_redirect(self) -> None:
        """A redirect element is captured rather than silently dropped."""
        xml = '<partial-response><redirect url="/login"/></partial-response>'
        assert parse_partial_response(xml).redirect_url == "/login"


class TestSessionState:
    """State handling across a request/response round trip."""

    def test_view_state_is_refreshed_from_the_response(self, tee_sheet_form: FormState) -> None:
        """Each response rolls the ViewState the next request must carry."""
        state = FormState.from_html(TEE_SHEET)
        session = make_session(
            state, lambda request: httpx.Response(200, text=partial_response("<p>ok</p>", "vs-9"))
        )
        session.post(AbConfig(source="s", form=FORM_ID))
        assert session.form_state.view_state == "vs-9"

    def test_form_fields_are_rebuilt_when_the_form_re_renders(self) -> None:
        """The tee sheet re-renders the whole form; stale fields must not persist."""
        state = FormState.from_html(TEE_SHEET)
        assert dict(state.fields)[f"{FORM_ID}:j_idt104_input"] == "02/01/2026"

        rerendered = (
            f'<form id="{FORM_ID}" action="/post">'
            f'<input type="hidden" name="{FORM_ID}:newField" value="fresh">'
            "</form>"
        )
        session = make_session(
            state,
            lambda request: httpx.Response(200, text=partial_response(rerendered, "vs-3")),
        )
        session.post(AbConfig(source="s", form=FORM_ID))

        fields = dict(session.form_state.fields)
        assert fields[f"{FORM_ID}:newField"] == "fresh"
        assert f"{FORM_ID}:j_idt104_input" not in fields
        assert fields["javax.faces.ViewState"] == "vs-3"

    def test_non_200_is_an_error(self, tee_sheet_form: FormState) -> None:
        """A non-200 status is a failed step, not an empty response."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(503))
        with pytest.raises(DirectHttpError, match="HTTP 503"):
            session.post(AbConfig(source="s", form=FORM_ID))

    def test_pre_built_body_is_sent_verbatim(self) -> None:
        """The staged body must not be re-serialized on the critical path."""
        sent: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            sent.append(request.content)
            return httpx.Response(200, text=partial_response("<p>ok</p>"))

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        session.post(AbConfig(source="s", form=FORM_ID), body=b"staged=body")
        assert sent == [b"staged=body"]


class StubDriver:
    """Minimal stand-in for a logged-in WebDriver on the tee sheet."""

    def __init__(self, cookies: list[dict[str, str]], page_source: str = TEE_SHEET) -> None:
        """Queue the pages this recorder will serve, in order."""
        self._cookies = cookies
        self.page_source = page_source
        self.current_url = "https://www.waldengolf.com/group/pages/book-a-tee-time"

    def get_cookies(self) -> list[dict[str, str]]:
        """Return the browser's cookie jar in Selenium's shape."""
        return self._cookies

    def execute_script(self, script: str) -> str:
        """Answer the user-agent lookup from_selenium performs."""
        return "Mozilla/5.0 (StubDriver)"


class TestFromSelenium:
    """Adopting a live browser session - the production entry point."""

    def test_adopts_cookies_user_agent_and_form_state(self) -> None:
        """The session inherits everything the browser established."""
        driver = StubDriver([{"name": "JSESSIONID", "value": "abc123"}])
        session = PrimeFacesSession.from_selenium(driver)

        assert session.form_state.form_id == FORM_ID
        assert session.form_state.view_state == "3597409561974113966:-1460199378475720306"
        assert session.base_url == driver.current_url
        assert session._client.headers["User-Agent"] == "Mozilla/5.0 (StubDriver)"
        assert session._client.cookies["JSESSIONID"] == "abc123"
        session.close()

    def test_rejects_a_session_with_no_cookies(self) -> None:
        """Without the session cookie every POST would come back unauthenticated."""
        with pytest.raises(DirectHttpError, match="no cookies"):
            PrimeFacesSession.from_selenium(StubDriver([]))


class TestProtocolHeaders:
    """The headers the JSF bridge routes on must reach the wire."""

    def test_posted_request_carries_the_primefaces_headers(self) -> None:
        """The bridge routes on these headers, so they must reach the wire."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            seen.append(request)
            return httpx.Response(200, text=partial_response("<p>ok</p>"))

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        session.post(AbConfig(source="s", form=FORM_ID))

        assert seen[0].headers["Faces-Request"] == "partial/ajax"
        assert seen[0].headers["X-Requested-With"] == "XMLHttpRequest"
        assert seen[0].headers["Content-Type"].startswith("application/x-www-form-urlencoded")


class TestWarmUp:
    """Opening the connection before the window."""

    def test_reports_round_trip_and_prefers_head(self) -> None:
        """Warm-up measures RTT and avoids a full page render."""
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            methods.append(request.method)
            return httpx.Response(200)

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        assert session.warm_up() >= 0
        # HEAD avoids making the server render the whole tee sheet for a body
        # we discard.
        assert methods == ["HEAD"]

    def test_falls_back_to_get_when_head_is_unsupported(self) -> None:
        """A server refusing HEAD still gets its connection warmed."""
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            methods.append(request.method)
            return httpx.Response(405) if request.method == "HEAD" else httpx.Response(200)

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        session.warm_up()
        assert methods == ["HEAD", "GET"]

    def test_unauthenticated_warm_up_is_an_error(self) -> None:
        """Better to find out now than as a confusing Reserve failure at 6:30."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(302))
        with pytest.raises(DirectHttpError, match="may not be authenticated"):
            session.warm_up()

    def test_transport_error_becomes_direct_http_error(self) -> None:
        """A connection failure surfaces as a recoverable error."""

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            raise httpx.ConnectError("no route to host")

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        with pytest.raises(DirectHttpError, match="Failed to warm up"):
            session.warm_up()


class TestExpiredSession:
    """A session that died between staging and the race."""

    def test_post_propagates_view_expired(self) -> None:
        """An expired view reaches the caller as ViewExpiredError."""
        xml = (
            "<partial-response><error>"
            "<error-name>class javax.faces.application.ViewExpiredException</error-name>"
            "<error-message>View could not be restored</error-message>"
            "</error></partial-response>"
        )
        session = make_session(
            FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200, text=xml)
        )
        with pytest.raises(ViewExpiredError):
            session.post(AbConfig(source="s", form=FORM_ID))

    def test_redirect_response_is_treated_as_expired(self) -> None:
        """JSF answers a dead session with a redirect, not an error element."""
        xml = '<partial-response><redirect url="/web/pages/login"/></partial-response>'
        session = make_session(
            FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200, text=xml)
        )
        with pytest.raises(ViewExpiredError, match="no longer valid"):
            session.post(AbConfig(source="s", form=FORM_ID))


class TestVisibleText:
    """Text extraction used to verify a booking from a partial response."""

    def test_reads_element_text(self) -> None:
        """Element text is extracted from markup."""
        assert "Booking confirmed" in visible_text("<div><p>Booking confirmed</p></div>")

    def test_ignores_script_and_style_bodies(self) -> None:
        """A JS string must not be mistaken for page copy."""
        html = '<div><script>var m = "booking failed";</script><p>Confirmed</p></div>'
        text = visible_text(html)
        assert "Confirmed" in text
        assert "booking failed" not in text


class TestSleepUntil:
    """The Python-side precision wait."""

    def test_returns_immediately_when_the_target_is_past(self) -> None:
        """A target already in the past does not block."""
        import time as time_module

        past = int(time_module.time() * 1000) - 5000
        assert sleep_until(past) >= 0

    def test_waits_until_the_target(self) -> None:
        """The wait never returns before the target instant."""
        import time as time_module

        target = int(time_module.time() * 1000) + 40
        drift = sleep_until(target)

        # The invariant is that it never returns early - that one is ours to
        # keep. The upper bound is not: past the coarse sleep it is OS scheduler
        # latency, and a shared CI runner overshot the old 250ms bound by 64ms
        # on a green build. Kept only wide enough to catch a real regression
        # (waiting on the wrong clock, or not waking at all), which would be off
        # by seconds, not by scheduler jitter.
        assert int(time_module.time() * 1000) >= target
        assert 0 <= drift < 2000


# ---------------------------------------------------------------------------
# The booking chain
# ---------------------------------------------------------------------------

# Shaped like a real PrimeFaces selectOneButton: the radios carry no inline
# handler, and the change behavior lives in the widget-init script keyed on the
# group's component id. The time-period filter (values 0-3) sits alongside it,
# which is the collision issue #105 was about.
PLAYER_PAGE = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <div class="ui-selectonebutton" id="timeFilter">
    <div class="ui-button"><input type="radio" name="filter" value="0" checked></div>
    <div class="ui-button"><input type="radio" name="filter" value="1"></div>
    <div class="ui-button"><input type="radio" name="filter" value="2"></div>
  </div>
  <div class="ui-selectonebutton" id="playerGroup">
    <div class="ui-button"><input type="radio" name="players" value="1"></div>
    <div class="ui-button"><input type="radio" name="players" value="2"></div>
    <div class="ui-button"><input type="radio" name="players" value="4"></div>
  </div>
  <script type="text/javascript">
    PrimeFaces.cw("SelectOneButton","widget_players",{{id:"playerGroup",
      behaviors:{{change:function(ext,event){{PrimeFaces.ab({{s:"playerGroup",
      e:"change",f:"{FORM_ID}",p:"playerGroup",u:"{FORM_ID}"}},ext);}}}}}});
  </script>
</form>
"""

# The alternative shape: an inline onclick straight on the control.
PLAYER_PAGE_INLINE_HANDLER = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <div class="ui-selectonebutton" id="playerGroup">
    <div class="ui-button"><input type="radio" name="players" value="4" id="playerRadio4"
      onclick='PrimeFaces.ab({{s:"playerRadio4",f:"{FORM_ID}",e:"change",u:"{FORM_ID}"}});'></div>
  </div>
</form>
"""

ROWS_PAGE = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <table id="playersTable"><tbody>
    <tr data-ri="0"><td>Member</td></tr>
    <tr data-ri="1"><td><a id="tbd1"
      onclick='PrimeFaces.ab({{s:"tbd1",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
    <tr data-ri="2"><td><a id="tbd2"
      onclick='PrimeFaces.ab({{s:"tbd2",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
    <tr data-ri="3"><td><a id="tbd3"
      onclick='PrimeFaces.ab({{s:"tbd3",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
  </tbody></table>
  <a id="bookTeeTimeAction"
     onclick='PrimeFaces.ab({{s:"bookTeeTimeAction",f:"{FORM_ID}",u:"{FORM_ID}"}});'>Book Now</a>
</form>
"""

BOOKED_PAGE = f'<form id="{FORM_ID}" action="/post"><p>Booking confirmed</p></form>'

BLOCKED_PAGE = (
    f'<form id="{FORM_ID}" action="/post">'
    '<div id="teeSheetValidationErrorPopup" aria-hidden="false">'
    "This slot is blocked by another user</div></form>"
)

# What a refusal we have no phrase for looks like: the response says no in a
# message container, using none of the words the phrase check knows.
REFUSED_PAGE = (
    f'<form id="{FORM_ID}" action="/post">'
    '<div class="ui-messages-error">'
    "Members are limited to one tee time per day.</div></form>"
)

# The same refusal raised through the blocked popup at Book Now rather than at
# Reserve - the step whose response nothing used to examine.
BLOCKED_AT_BOOK_NOW = (
    f'<form id="{FORM_ID}" action="/post">'
    '<div id="teeSheetValidationErrorPopup" aria-hidden="false">'
    "That time is already reserved</div></form>"
)

# 2026-08-22: the club refused Book Now over an unset per-player Resource
# (cart/walk) dropdown, in a "Reservation Alert:" dialog no message container
# this chain recognized - so the response looked like ordinary progress and
# the chain walked straight to COMPLETE. This is the shape of that response:
# Book Now is still present, and so is the row's select, still on its own
# placeholder (no option carries `selected`, and the first option's label
# starts with "--", exactly like the real "--Select Transport--").
ROWS_PAGE_WITH_UNSET_SELECT = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <table id="playersTable"><tbody>
    <tr data-ri="0"><td>
      <select id="resourceCol_input" name="resourceCol_input"
        onchange='PrimeFaces.ab({{s:"resourceCol",f:"{FORM_ID}",e:"change",u:"{FORM_ID}"}});'>
        <option value="placeholder-id">--Select Transport--</option>
        <option value="cart-id">Cart</option>
        <option value="walk-id">Walk</option>
      </select>
    </td></tr>
    <tr data-ri="1"><td><a id="tbd1"
      onclick='PrimeFaces.ab({{s:"tbd1",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
    <tr data-ri="2"><td><a id="tbd2"
      onclick='PrimeFaces.ab({{s:"tbd2",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
    <tr data-ri="3"><td><a id="tbd3"
      onclick='PrimeFaces.ab({{s:"tbd3",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
  </tbody></table>
  <a id="bookTeeTimeAction"
     onclick='PrimeFaces.ab({{s:"bookTeeTimeAction",f:"{FORM_ID}",u:"{FORM_ID}"}});'>Book Now</a>
</form>
"""

# Two unset selects on one Book Now response - row 0's is resolvable, row 1's
# is not. Regression coverage for filling row 0 alone being mistaken for the
# form being ready to resubmit.
TWO_UNSET_SELECTS_PAGE = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <table id="playersTable"><tbody>
    <tr data-ri="0"><td>
      <select id="resourceCol0_input" name="resourceCol0_input"
        onchange='PrimeFaces.ab({{s:"resourceCol0",f:"{FORM_ID}",e:"change",u:"{FORM_ID}"}});'>
        <option value="placeholder-id">--Select Transport--</option>
        <option value="cart-id">Cart</option>
      </select>
    </td></tr>
    <tr data-ri="1"><td>
      <select>
        <option value="placeholder-id">--Select Transport--</option>
        <option value="cart-id">Cart</option>
      </select>
    </td></tr>
  </tbody></table>
  <a id="bookTeeTimeAction"
     onclick='PrimeFaces.ab({{s:"bookTeeTimeAction",f:"{FORM_ID}",u:"{FORM_ID}"}});'>Book Now</a>
</form>
"""

# Same shape after row 0's field is set - only the unresolvable row 1 select
# is still unset.
ROW_0_FIXED_ROW_1_STILL_UNRESOLVABLE_PAGE = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <table id="playersTable"><tbody>
    <tr data-ri="0"><td>
      <select id="resourceCol0_input" name="resourceCol0_input">
        <option value="placeholder-id">--Select Transport--</option>
        <option value="cart-id" selected>Cart</option>
      </select>
    </td></tr>
    <tr data-ri="1"><td>
      <select>
        <option value="placeholder-id">--Select Transport--</option>
        <option value="cart-id">Cart</option>
      </select>
    </td></tr>
  </tbody></table>
  <a id="bookTeeTimeAction"
     onclick='PrimeFaces.ab({{s:"bookTeeTimeAction",f:"{FORM_ID}",u:"{FORM_ID}"}});'>Book Now</a>
</form>
"""

# Same shape, but the select carries no onchange/id PrimeFaces could replay -
# an unset field the chain has no way to act on.
ROWS_PAGE_WITH_UNRESOLVABLE_SELECT = f"""
<form id="{FORM_ID}" action="/post">
  <input type="hidden" name="javax.faces.ViewState" value="vs">
  <table id="playersTable"><tbody>
    <tr data-ri="0"><td>
      <select>
        <option value="placeholder-id">--Select Transport--</option>
        <option value="cart-id">Cart</option>
      </select>
    </td></tr>
    <tr data-ri="1"><td><a id="tbd1"
      onclick='PrimeFaces.ab({{s:"tbd1",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
    <tr data-ri="2"><td><a id="tbd2"
      onclick='PrimeFaces.ab({{s:"tbd2",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
    <tr data-ri="3"><td><a id="tbd3"
      onclick='PrimeFaces.ab({{s:"tbd3",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
  </tbody></table>
  <a id="bookTeeTimeAction"
     onclick='PrimeFaces.ab({{s:"bookTeeTimeAction",f:"{FORM_ID}",u:"{FORM_ID}"}});'>Book Now</a>
</form>
"""


class TestFindResponseMessage:
    """Reading the site's own message containers out of a partial response."""

    def test_reads_a_primefaces_error_message(self) -> None:
        """The container class the site uses for validation errors."""
        message = find_response_message(
            '<div class="ui-messages-error">Members are limited to one per day.</div>'
        )

        assert message == "Members are limited to one per day."

    def test_reads_role_alert_and_aria_live(self) -> None:
        """Not every message container is spelled with a class."""
        assert find_response_message('<div role="alert">Slot unavailable</div>') == (
            "Slot unavailable"
        )
        assert find_response_message('<span aria-live="assertive">Try again</span>') == (
            "Try again"
        )

    def test_skips_hidden_template_containers(self) -> None:
        """PrimeFaces renders empty message templates on every page."""
        markup = (
            '<div class="ui-messages-error" aria-hidden="true">Stale template text</div>'
            '<div class="ui-messages-error">Real message</div>'
        )

        assert find_response_message(markup) == "Real message"

    def test_hidden_child_template_inside_a_visible_wrapper_is_pruned(self) -> None:
        """A visible wrapper must not carry its hidden template's text.

        The hidden text would be reported as the site's message, and because a
        message already contained in a collected one is dropped as nested, it
        would take the real child message down with it.
        """
        markup = (
            '<div class="ui-messages">'
            '<div class="ui-message-error" aria-hidden="true">Stale template text</div>'
            '<div class="ui-message-error">Members are limited to one per day.</div>'
            "</div>"
        )

        assert find_response_message(markup) == "Members are limited to one per day."

    def test_container_inside_a_hidden_dialog_is_skipped(self) -> None:
        """A visible container the member cannot see is still not a message."""
        markup = (
            '<div class="ui-dialog" aria-hidden="true">'
            '<div class="ui-messages-error">Hidden dialog text</div></div>'
        )

        assert find_response_message(markup) is None

    def test_empty_containers_report_nothing(self) -> None:
        """An unfilled message container is not a message."""
        assert find_response_message('<div class="ui-messages-error"></div>') is None

    def test_nested_containers_are_not_repeated(self) -> None:
        """A wrapper and its child would otherwise both carry the same text."""
        markup = (
            '<div class="ui-messages"><div class="ui-messages-error">'
            "Only one per day</div></div>"
        )

        assert find_response_message(markup) == "Only one per day"

    def test_long_text_is_truncated(self) -> None:
        """A re-rendered tee sheet must not be pasted into an SMS reply."""
        message = find_response_message(f'<div role="alert">{"x" * 900}</div>')

        assert message is not None
        assert len(message) <= 503
        assert message.endswith("...")

    def test_a_clean_tee_sheet_yields_nothing(self) -> None:
        """The real page carries no message text to mistake for a refusal.

        This is what makes the extraction safe to report alongside every
        failure: the routine response has nothing in it.
        """
        assert find_response_message(TEE_SHEET) is None

    def test_reads_the_restriction_popup(self) -> None:
        """The club's refusal, which carries no error class at all.

        Booking 08/08 at 5:00 and 5:08 PM as one batch got the first and was
        refused the second; the response said so in a ui-dialog the class
        markers could not see, so the member was told only that the reservation
        was not confirmed.
        """
        assert find_response_message(RESTRICTION_POPUP) == (
            "Restriction: Member: Sample, Member is restricted for 1 round(s) "
            "on Northgate per Day"
        )

    def test_popup_buttons_and_widget_scripts_are_not_message_text(self) -> None:
        """The sentence is the message; the dialog's own furniture is not."""
        message = find_response_message(RESTRICTION_POPUP)

        assert message is not None
        assert "Ok" not in message
        assert "PrimeFaces.cw" not in message

    def test_an_unfilled_popup_wrapper_is_not_a_message(self) -> None:
        """The site renders these wrappers empty until it has something to say.

        The same response that carried the restriction had these two beside it,
        empty - which is why matching the wrapper by id is safe.
        """
        markup = (
            '<span id="teeTimeForm:warningPopup"></span>'
            '<span id="teeTimeForm:resourceNotAvailablePopup"></span>'
        )

        assert find_response_message(markup) is None


class ChainRecorder:
    """Serves canned responses in order, recording each request's source."""

    def __init__(self, pages: list[str]) -> None:
        """Queue the pages this recorder will serve, in order."""
        self.pages = pages
        self.sources: list[str] = []
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Serve the next canned page and record the request's source."""
        params = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.requests.append(params)
        self.sources.append(params.get("javax.faces.source", ""))
        page = self.pages[min(len(self.sources) - 1, len(self.pages) - 1)]
        return httpx.Response(200, text=partial_response(page, f"vs-{len(self.sources)}"))


def make_booker(recorder: ChainRecorder) -> DirectHttpBooker:
    """A booker with the Reserve request pre-staged against a recorder."""
    session = make_session(FormState.from_html(TEE_SHEET), recorder)
    booker = DirectHttpBooker(session)
    config = AbConfig(source=RESERVE_ID, form=FORM_ID, update=FORM_ID)
    booker._reserve_config = config
    booker._reserve_body = session.build_body(config)
    return booker


class TestDirectHttpBooker:
    """The Reserve -> players -> TBD -> Book Now chain."""

    def test_full_four_player_chain(self) -> None:
        """Reserve, player count, three TBD guests, then Book Now."""
        recorder = ChainRecorder(
            [PLAYER_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = make_booker(recorder).book(4)

        assert result.success, result.error
        assert result.phase == "complete"
        # Reserve, player count, three TBD guests, Book Now.
        assert recorder.sources == [
            RESERVE_ID,
            "playerGroup",
            "tbd1",
            "tbd2",
            "tbd3",
            "bookTeeTimeAction",
        ]

    def test_single_player_skips_tbd_guests(self) -> None:
        """A solo booking has no guest rows to fill."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = make_booker(recorder).book(1)

        assert result.success, result.error
        assert "tbd1" not in recorder.sources
        assert recorder.sources == [RESERVE_ID, "playerGroup", "bookTeeTimeAction"]

    def test_inline_handler_on_the_control_is_used_directly(self) -> None:
        """Not every control puts its behavior in a widget script."""
        recorder = ChainRecorder(
            [PLAYER_PAGE_INLINE_HANDLER, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = make_booker(recorder).book(4)

        assert result.success, result.error
        # The radio's own handler is used, not the enclosing group's.
        assert recorder.sources[1] == "playerRadio4"

    def test_blocked_slot_is_detected_after_reserve(self) -> None:
        """Losing the slot stops the chain instead of pressing on."""
        recorder = ChainRecorder([BLOCKED_PAGE])
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.blocked
        assert result.phase == PHASE_RESERVE_SENT
        assert result.error is not None and "blocked by another user" in result.error
        # It must stop, not push on into the player-count step.
        assert recorder.sources == [RESERVE_ID]

    def test_ignores_the_time_filter_button_group(self) -> None:
        """Issue #105: both groups are .ui-selectonebutton."""
        recorder = ChainRecorder(
            [PLAYER_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = make_booker(recorder).book(4)

        assert result.success, result.error
        assert recorder.sources[1] == "playerGroup"

    def test_player_count_radio_value_is_submitted(self) -> None:
        """Selecting a radio means posting its value, not just its behavior."""
        bodies: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            params = dict(urllib.parse.parse_qsl(request.content.decode()))
            bodies.append(params)
            page = PLAYER_PAGE if len(bodies) == 1 else ROWS_PAGE
            return httpx.Response(200, text=partial_response(page))

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        booker = DirectHttpBooker(session)
        booker._reserve_config = AbConfig(source=RESERVE_ID, form=FORM_ID)
        booker._reserve_body = b"staged=reserve"
        result = booker.book(1)

        assert result.success, result.error
        assert bodies[1]["players"] == "1"

    def test_blocked_slot_is_detected_at_book_now(self) -> None:
        """The submit step's response is examined like every other step's.

        Regression: the chain went straight from Book Now to COMPLETE, so a
        booking refused at the last step was reported the same as one accepted.
        """
        recorder = ChainRecorder(
            [PLAYER_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, BLOCKED_AT_BOOK_NOW]
        )
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.blocked
        assert result.phase == PHASE_BOOK_NOW
        assert result.error is not None and "already reserved" in result.error

    def test_unset_required_field_is_filled_and_book_now_retried(self) -> None:
        """2026-08-22: a lost slot from an unset dropdown gets a second try.

        The club refused Book Now over an unset per-player Resource field, in
        a dialog no message container recognized - so the response looked
        like ordinary progress. The fix does not know or care that the field
        is called "Resource": it notices Book Now's own response still shows
        its action and a player row on its own placeholder, best-guesses the
        first real option, and asks again.
        """
        recorder = ChainRecorder(
            [
                PLAYER_PAGE,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,  # Book Now refused; form re-rendered unresolved
                ROWS_PAGE,  # the field is set now - no select left to fill
                BOOKED_PAGE,  # Book Now retried and accepted
            ]
        )
        result = make_booker(recorder).book(4)

        assert result.success, result.error
        assert result.phase == "complete"
        assert recorder.sources == [
            RESERVE_ID,
            "playerGroup",
            "tbd1",
            "tbd2",
            "tbd3",
            "bookTeeTimeAction",
            "resourceCol",
            "bookTeeTimeAction",
        ]
        # The guess is the dropdown's own first real option, never the placeholder.
        assert recorder.requests[6]["resourceCol_input"] == "cart-id"

    def test_still_refused_after_the_best_guess_is_reported_blocked(self) -> None:
        """A best guess is not a guarantee - a real refusal is still reported."""
        recorder = ChainRecorder(
            [
                PLAYER_PAGE,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,
                ROWS_PAGE_WITH_UNSET_SELECT,  # Book Now refused
                ROWS_PAGE,  # field filled
                BLOCKED_AT_BOOK_NOW,  # retried and refused for a different reason
            ]
        )
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.blocked
        assert result.phase == PHASE_BOOK_NOW
        assert result.error is not None and "already reserved" in result.error
        assert recorder.sources[-1] == "bookTeeTimeAction"

    def test_unresolvable_unset_field_does_not_loop_or_crash(self) -> None:
        """A field the chain cannot act on is left alone, not spun on forever."""
        recorder = ChainRecorder(
            [
                PLAYER_PAGE,
                ROWS_PAGE_WITH_UNRESOLVABLE_SELECT,
                ROWS_PAGE_WITH_UNRESOLVABLE_SELECT,
                ROWS_PAGE_WITH_UNRESOLVABLE_SELECT,
                ROWS_PAGE_WITH_UNRESOLVABLE_SELECT,
                ROWS_PAGE_WITH_UNRESOLVABLE_SELECT,
            ]
        )
        result = make_booker(recorder).book(4)

        # No handler to replay means no retry was attempted; the chain falls
        # through to its ordinary (unresolved) outcome rather than raising.
        assert recorder.sources == [
            RESERVE_ID,
            "playerGroup",
            "tbd1",
            "tbd2",
            "tbd3",
            "bookTeeTimeAction",
        ]
        assert result.phase == "complete"

    def test_partial_fill_with_a_second_unresolvable_field_does_not_retry(self) -> None:
        """Fixing one of two unset fields is not the same as being ready.

        Regression: filling row 0's select used to be enough to hand the
        response back as "ready to resubmit" even though row 1's was still
        unset and unresolvable - so Book Now would have been retried with a
        required field still missing, spending the one retry on a request
        that could only be refused again.
        """
        recorder = ChainRecorder(
            [
                PLAYER_PAGE,
                ROWS_PAGE,
                ROWS_PAGE,
                ROWS_PAGE,
                ROWS_PAGE,
                TWO_UNSET_SELECTS_PAGE,  # Book Now refused; two fields unset
                ROW_0_FIXED_ROW_1_STILL_UNRESOLVABLE_PAGE,
            ]
        )
        result = make_booker(recorder).book(4)

        assert result.phase == "complete"
        # Row 0's field was set once; Book Now was never retried, because
        # row 1 was still unresolved after it.
        assert recorder.sources == [
            RESERVE_ID,
            "playerGroup",
            "tbd1",
            "tbd2",
            "tbd3",
            "bookTeeTimeAction",
            "resourceCol0",
        ]

    def test_first_real_option_skips_a_disabled_choice(self) -> None:
        """A disabled option is the club saying it can't be chosen right now.

        Not a value to submit as a best guess - the retry would just trade
        one refusal for another.
        """
        document = parse_html(
            '<select><option value="ph">--Select--</option>'
            '<option value="sold-out" disabled>Cart</option>'
            '<option value="ok">Walk</option></select>'
        )
        select = document.find_all("select")[0]

        assert _first_real_option_value(select) == "ok"

    def test_book_now_refusal_text_is_carried_off_the_chain(self) -> None:
        """A refusal with no known phrase still reaches the caller as words.

        The chain completes - every step found what it needed - but the site
        said no in a message container, and that is the only readable record of
        why no reservation exists.
        """
        recorder = ChainRecorder(
            [PLAYER_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, REFUSED_PAGE]
        )
        result = make_booker(recorder).book(4)

        assert result.phase == "complete"
        assert result.response_message is not None
        assert "one tee time per day" in result.response_message
        assert result.as_chain_result()["responseMessage"] == result.response_message

    def test_confirmed_response_carries_no_message(self) -> None:
        """A clean confirmation has nothing to report."""
        recorder = ChainRecorder(
            [PLAYER_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = make_booker(recorder).book(4)

        assert result.success, result.error
        assert result.response_message is None

    def test_failed_step_carries_the_message_it_was_reading(self) -> None:
        """A step that cannot find its element reports what the site said."""
        recorder = ChainRecorder([REFUSED_PAGE])
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.phase == "player_count"
        assert result.response_message is not None
        assert "one tee time per day" in result.response_message

    def test_missing_player_selector_fails_in_that_phase(self) -> None:
        """A response without the selector fails in player_count."""
        recorder = ChainRecorder([BOOKED_PAGE])
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.phase == "player_count"
        assert result.error is not None and "Player count selector" in result.error

    def test_disabled_player_count_is_reported(self) -> None:
        """A disabled count is reported rather than clicked anyway."""
        disabled = PLAYER_PAGE.replace(
            '<div class="ui-button"><input type="radio" name="players" value="4">',
            '<div class="ui-button ui-state-disabled">'
            '<input type="radio" name="players" value="4">',
        )
        result = make_booker(ChainRecorder([disabled])).book(4)

        assert not result.success
        assert result.phase == "player_count"
        assert result.error is not None and "disabled" in result.error

    def test_too_few_player_rows_fails_in_the_tbd_phase(self) -> None:
        """Fewer rows than players is a tbd_guests failure."""
        short_rows = ROWS_PAGE.replace('<tr data-ri="3"', '<tr data-ignored="3"').replace(
            '<tr data-ri="2"', '<tr data-ignored="2"'
        )
        recorder = ChainRecorder([PLAYER_PAGE, short_rows])
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.phase == "tbd_guests"
        assert result.error is not None and "player row" in result.error

    def test_missing_book_now_fails_in_that_phase(self) -> None:
        """A response without Book Now fails in book_now."""
        no_button = ROWS_PAGE.replace('id="bookTeeTimeAction"', 'id="somethingElse"').replace(
            ">Book Now<", ">Other<"
        )
        recorder = ChainRecorder([PLAYER_PAGE, no_button, no_button, no_button, no_button])
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.phase == "book_now"
        assert result.error is not None and "Book Now" in result.error

    def test_transport_failure_is_reported_not_raised(self) -> None:
        """A connection that never opened becomes a result, not an exception.

        The phase is pre-submit on purpose. httpx raises ConnectError only from
        establishing the connection, so no byte of the Reserve was written and a
        Selenium retry cannot race a booking that does not exist. This used to
        assert PHASE_RESERVE_SENT, which was safe but pessimistic: past
        PRE_SUBMIT_PHASES the provider reports the failure instead of retrying,
        so a 6:30 that could not open a socket was given up on rather than
        handed to the browser chain that could still have won it.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            raise httpx.ConnectError("connection refused")

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        booker = DirectHttpBooker(session)
        booker._reserve_config = AbConfig(source=RESERVE_ID, form=FORM_ID)
        booker._reserve_body = b"staged=reserve"
        result = booker.book(4)

        assert not result.success
        assert result.phase == PHASE_RESERVE_STAGED
        assert result.phase in PRE_SUBMIT_PHASES
        assert result.error is not None and "connection refused" in result.error

    def test_quiet_signal_is_raised_once_the_first_reserve_answers(self) -> None:
        """The provider's browser-quieting thread waits on exactly this event."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        booker = make_booker(recorder)
        booker.quiet_signal = threading.Event()
        result = booker.book(1)

        assert result.success, result.error
        assert booker.quiet_signal.is_set()

    def test_quiet_signal_stays_down_when_nothing_was_submitted(self) -> None:
        """A connection that never opened leaves the browser retry live, and the
        page it needs must not be taken apart under it."""

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            raise httpx.ConnectError("connection refused")

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        booker = DirectHttpBooker(session)
        booker._reserve_config = AbConfig(source=RESERVE_ID, form=FORM_ID)
        booker._reserve_body = b"staged=reserve"
        booker.quiet_signal = threading.Event()
        result = booker.book(4)

        assert not result.success
        assert result.phase in PRE_SUBMIT_PHASES
        assert not booker.quiet_signal.is_set()

    def test_book_without_prepare_is_rejected(self) -> None:
        """Misuse is a pre-submit result, so the caller falls back rather than
        losing the booking to a bug in an opt-in path."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        result = DirectHttpBooker(session).book(4)

        assert not result.success
        assert result.phase in PRE_SUBMIT_PHASES
        assert result.error is not None and "prepare" in result.error

    def test_invalid_player_count_is_rejected(self) -> None:
        """Player counts outside 1-4 are refused before anything is sent."""
        recorder = ChainRecorder([BOOKED_PAGE])
        result = make_booker(recorder).book(5)

        assert not result.success
        assert result.phase in PRE_SUBMIT_PHASES
        assert result.error is not None and "num_players" in result.error
        assert recorder.sources == []

    def test_timed_mode_waits_for_the_target(self) -> None:
        """Timed mode holds the POST until the target instant."""
        import time as time_module

        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        target = int(time_module.time() * 1000) + 60
        result = make_booker(recorder).book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert int(time_module.time() * 1000) >= target
        assert "clickDriftMs" in result.timing
        # Loose for the same reason as test_waits_until_the_target: this bound
        # measures the CI runner's scheduler, not the code under test.
        assert 0 <= result.timing["clickDriftMs"] < 2000


class TestBlockedDetectionScope:
    """Blocked detection must key on the popup, not on any matching text."""

    def test_hidden_popup_template_does_not_block(self) -> None:
        """A popup rendered but hidden is a template, not a verdict."""
        hidden = (
            f'<form id="{FORM_ID}" action="/post">'
            '<div id="teeSheetValidationErrorPopup" aria-hidden="true">'
            "This slot is blocked by another user</div>" + PLAYER_PAGE + "</form>"
        )
        recorder = ChainRecorder([hidden, ROWS_PAGE, BOOKED_PAGE])
        result = make_booker(recorder).book(1)

        assert not result.blocked
        assert result.success, result.error

    def test_matching_text_outside_the_popup_does_not_block(self) -> None:
        """Copy elsewhere on the page must not abort a slot we hold."""
        noisy = (
            f'<form id="{FORM_ID}" action="/post">'
            "<p>Times already reserved are shown in grey.</p>" + PLAYER_PAGE + "</form>"
        )
        recorder = ChainRecorder([noisy, ROWS_PAGE, BOOKED_PAGE])
        result = make_booker(recorder).book(1)

        assert not result.blocked
        assert result.success, result.error

    def test_visible_popup_still_blocks(self) -> None:
        """A shown popup is honored as a lost slot."""
        result = make_booker(ChainRecorder([BLOCKED_PAGE])).book(4)

        assert result.blocked
        assert result.error is not None and "blocked by another user" in result.error


class TestPlayerRowFallback:
    """The non-playersTable fallback must not count header rows."""

    def test_header_row_is_not_mistaken_for_the_member_row(self) -> None:
        """A header counted as row 0 shifts every guest index by one."""
        with_header = f"""
        <form id="{FORM_ID}" action="/post">
          <input type="hidden" name="javax.faces.ViewState" value="vs">
          <table id="playerPanel">
            <thead><tr><th>Player</th></tr></thead>
            <tbody>
              <tr><td>Member</td></tr>
              <tr><td><a id="tbd1"
                onclick='PrimeFaces.ab({{s:"tbd1",f:"{FORM_ID}",u:"{FORM_ID}"}});'>TBD</a></td></tr>
            </tbody>
          </table>
          <a id="bookTeeTimeAction"
             onclick='PrimeFaces.ab({{s:"bookTeeTimeAction",f:"{FORM_ID}",u:"{FORM_ID}"}});'
             >Book Now</a>
        </form>
        """
        recorder = ChainRecorder([PLAYER_PAGE, with_header, with_header, BOOKED_PAGE])
        result = make_booker(recorder).book(2)

        assert result.success, result.error
        # The guest row's TBD, not something shifted off the header row.
        assert "tbd1" in recorder.sources


class TestFinalMarkup:
    """The last response is the only record of a direct booking's outcome."""

    def test_success_carries_the_final_response_markup(self) -> None:
        """The final response is retained as the record of the outcome."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = make_booker(recorder).book(1)

        assert result.success, result.error
        assert "Booking confirmed" in result.final_markup
        assert "Booking confirmed" in visible_text(result.final_markup)

    def test_chain_result_exposes_markup_to_the_provider(self) -> None:
        """The provider receives the markup it needs to verify the booking."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        chain_result = make_booker(recorder).book(1).as_chain_result()

        assert "Booking confirmed" in chain_result["finalMarkup"]

    def test_chain_result_identifies_the_path_it_came_from(self) -> None:
        """The provider resolves only this path's outcomes against the site.

        A JS-chain result leaves the browser sitting on the booking's own
        response, so the DOM answers for it. Nothing in the shape of the dict
        says which chain produced it, so the path is named in it.
        """
        from app.providers.walden_http_booker import DIRECT_HTTP_PATH

        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        chain_result = make_booker(recorder).book(1).as_chain_result()

        assert chain_result["path"] == DIRECT_HTTP_PATH


class TestPrepare:
    """Staging the Reserve request off the real tee sheet."""

    def test_stages_the_reserve_request_from_the_page(self) -> None:
        """Staging resolves the button's handler and serializes its body."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        booker = DirectHttpBooker(session)
        booker.prepare(RESERVE_ID, TEE_SHEET)

        assert booker._reserve_config is not None
        assert booker._reserve_config.source == RESERVE_ID
        assert booker._reserve_body is not None
        params = dict(urllib.parse.parse_qsl(booker._reserve_body.decode()))
        assert params["javax.faces.source"] == RESERVE_ID

    def test_unknown_button_id_is_rejected(self) -> None:
        """A button id absent from the page cannot be staged."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        with pytest.raises(DirectHttpError, match="not found"):
            DirectHttpBooker(session).prepare("no-such-button", TEE_SHEET)

    def test_button_without_a_handler_is_rejected(self) -> None:
        """A plain link with no PrimeFaces handler cannot be replayed."""
        html = (
            f'<form id="{FORM_ID}" action="/post">'
            '<input type="hidden" name="javax.faces.ViewState" value="v">'
            '<a id="plain">Reserve</a></form>'
        )
        session = make_session(FormState.from_html(html), lambda request: httpx.Response(200))
        with pytest.raises(DirectHttpError, match="no PrimeFaces.ab handler"):
            DirectHttpBooker(session).prepare("plain", html)


def slot_block(button_id: str, label: str) -> str:
    """One tee-sheet slot, shaped like the real one.

    The time label and the Reserve link sit in *sibling* cells, which is the
    whole reason the time lookup walks up and back down rather than reading an
    ancestor's text.
    """
    handler = f'PrimeFaces.ab({{s:"{button_id}",f:"{FORM_ID}",u:"{FORM_ID}"}})'
    return (
        '<li class="ui-datascroller-item"><div class="Empty"><div class="ui-grid-a">'
        '<div class="time-show"><div class="time-div">'
        f'<label class="custom-time-label">{label}</label></div></div>'
        f'<div class="slot-area"><a id="{button_id}" onmousedown="{handler}">Reserve</a></div>'
        "</div></div></li>"
    )


def refreshed_sheet(*slots: str) -> str:
    """A re-rendered tee sheet carrying the given slots."""
    return (
        f'<form id="{FORM_ID}" action="/post">'
        '<input type="hidden" name="javax.faces.encodedURL" value="/post">'
        f"{''.join(slots)}</form>"
    )


# The refresh re-renders the sheet and the slot lands on a different row - the
# case that firing the pre-staged component id blind gets wrong.
MOVED_RESERVE_ID = f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:12:slotTee:0:reserve_button"
MOVED_SHEET = refreshed_sheet(
    slot_block(f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:11:slotTee:0:reserve_button", "04:26 PM"),
    slot_block(MOVED_RESERVE_ID, "04:34 PM"),
)


def countdown_sheet(remaining: str) -> str:
    """A re-rendered sheet still carrying the club's pre-window counter."""
    return refreshed_sheet(
        f'<div class="booking-starts-in">Booking Starts In : {remaining}</div>',
        slot_block(MOVED_RESERVE_ID, "04:34 PM"),
    )


def booker_over(handler: Callable[[httpx.Request], httpx.Response]) -> DirectHttpBooker:
    """A booker with Reserve pre-staged against a hand-written handler."""
    session = make_session(FormState.from_html(TEE_SHEET), handler)
    booker = DirectHttpBooker(session)
    config = AbConfig(source=RESERVE_ID, form=FORM_ID, update=FORM_ID)
    booker._reserve_config = config
    booker._reserve_body = session.build_body(config)
    return booker


def stage_refresh(booker: DirectHttpBooker) -> DirectHttpBooker:
    """Add a staged day-tab refresh to a booker from make_booker."""
    booker._refresh_config = AbConfig(source=DAY_TAB_ID, form=FORM_ID, update=FORM_ID)
    booker._slot_time = RESERVE_SLOT_TIME
    return booker


def just_past() -> int:
    """A target timestamp already gone, so the precision wait returns at once."""
    return int(time_module.time() * 1000) - 1000


class TestSlotTimeParsing:
    """Reading a tee time off a slot's label."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("08:00 AM", time(8, 0)),
            ("07:53 AM", time(7, 53)),
            ("04:34 PM", time(16, 34)),
            # Midnight and noon are where a plain "+12 if PM" goes wrong.
            ("12:00 AM", time(0, 0)),
            ("12:30 PM", time(12, 30)),
            ("Available", None),
            ("", None),
        ],
    )
    def test_labels_parse(self, text: str, expected: time | None) -> None:
        """Each label form the sheet uses reads back as the right time."""
        assert _parse_slot_time(text) == expected


class TestRelocateReserve:
    """Finding a slot again in a freshly rendered sheet."""

    def test_finds_the_slot_by_time_in_the_real_sheet(self) -> None:
        """The captured sheet's 04:34 PM slot resolves to its Reserve request."""
        config = _relocate_reserve(TEE_SHEET, RESERVE_SLOT_TIME)

        assert config is not None
        assert config.source == RESERVE_ID

    def test_follows_the_slot_when_the_row_index_moves(self) -> None:
        """Matching is by tee time, not by the id staged earlier."""
        config = _relocate_reserve(MOVED_SHEET, RESERVE_SLOT_TIME)

        assert config is not None
        assert config.source == MOVED_RESERVE_ID

    def test_absent_slot_is_reported_as_missing(self) -> None:
        """A time the refreshed sheet no longer offers resolves to nothing."""
        assert _relocate_reserve(MOVED_SHEET, time(9, 15)) is None


class TestViewRefresh:
    """Re-rendering the tee sheet at the window before firing Reserve."""

    def test_timed_booking_refreshes_then_reserves_the_moved_slot(self) -> None:
        """The day tab goes first, and Reserve follows the slot to its new row."""
        recorder = ChainRecorder([MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        assert recorder.sources == [
            DAY_TAB_ID,
            MOVED_RESERVE_ID,
            "playerGroup",
            "bookTeeTimeAction",
        ]

    def test_reserve_carries_the_view_state_the_refresh_returned(self) -> None:
        """The staged body's ViewState is dead once the refresh lands."""
        recorder = ChainRecorder([MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        # The recorder hands out vs-1 for the refresh, so Reserve must post that
        # rather than the ViewState baked in before the window.
        assert recorder.requests[1]["javax.faces.ViewState"] == "vs-1"

    def test_untimed_booking_does_not_refresh(self) -> None:
        """An immediate booking already holds a live view; refreshing wastes it."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_refresh(make_booker(recorder)).book(1)

        assert result.success, result.error
        assert recorder.sources[0] == RESERVE_ID

    def test_unstaged_refresh_leaves_the_chain_alone(self) -> None:
        """Without a staged refresh a timed booking fires straight away."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = make_booker(recorder).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        assert recorder.sources[0] == RESERVE_ID

    def test_transient_refresh_failure_is_retried(self) -> None:
        """One bad round trip is not a reason to reserve against a stale view."""
        pages = [MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]
        served: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(urllib.parse.parse_qsl(request.content.decode()))
            served.append(params.get("javax.faces.source", ""))
            if len(served) == 1:
                return httpx.Response(500)  # first refresh attempt
            page = pages[min(len(served) - 2, len(pages) - 1)]
            return httpx.Response(200, text=partial_response(page))

        result = stage_refresh(booker_over(handler)).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        # Refresh, refresh again, then Reserve at the relocated slot.
        assert served[:3] == [DAY_TAB_ID, DAY_TAB_ID, MOVED_RESERVE_ID]
        assert result.timing["viewRefreshAttempts"] == 2
        assert "viewRefreshFailed" not in result.timing

    def test_exhausted_refresh_falls_back_to_the_staged_request(self) -> None:
        """Once every attempt is spent, a stale Reserve beats no Reserve.

        The browser chain the caller would fall back to holds the same
        pre-window view, so declining to send loses the booking outright.
        """
        served: list[str] = []
        pages = [PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(urllib.parse.parse_qsl(request.content.decode()))
            source = params.get("javax.faces.source", "")
            served.append(source)
            if source == DAY_TAB_ID:
                return httpx.Response(500)
            page = pages[min(served.count(RESERVE_ID) + served.count("playerGroup") - 1, 2)]
            return httpx.Response(200, text=partial_response(page))

        result = stage_refresh(booker_over(handler)).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        assert served.count(DAY_TAB_ID) == 4  # _REFRESH_MAX_ATTEMPTS
        assert served[4] == RESERVE_ID
        assert result.timing["viewRefreshFailed"] == "no-response"

    def test_expired_session_sends_no_reserve_at_all(self) -> None:
        """The staged request carries the ViewState the server just rejected."""
        served: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(urllib.parse.parse_qsl(request.content.decode()))
            served.append(params.get("javax.faces.source", ""))
            return httpx.Response(
                200,
                text=(
                    "<?xml version='1.0'?><partial-response><error>"
                    "<error-name>class javax.faces.application.ViewExpiredException"
                    "</error-name><error-message>view expired</error-message>"
                    "</error></partial-response>"
                ),
            )

        result = stage_refresh(booker_over(handler)).book(1, target_timestamp_ms=just_past())

        assert not result.success
        assert served == [DAY_TAB_ID]  # nothing else went out
        assert result.timing["viewRefreshFailed"] == "session-expired"
        # Still a pre-submit phase, so the caller may hand this to the browser.
        assert result.phase in PRE_SUBMIT_PHASES

    def test_sheet_still_counting_down_is_retried(self) -> None:
        """The club's own counter outranks our clock on whether booking is open."""
        pages = [countdown_sheet("00:00:03"), MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]
        recorder = ChainRecorder(pages)
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        # Refreshed, was told 3s remained, refreshed again, then reserved.
        assert recorder.sources[:3] == [DAY_TAB_ID, DAY_TAB_ID, MOVED_RESERVE_ID]
        assert result.timing["viewRefreshAttempts"] == 2
        # What the countdown leaves behind once it clears is
        # test_countdown_that_clears_leaves_no_marker_behind's business.

    def test_slot_missing_from_the_refresh_replays_the_staged_id(self) -> None:
        """Losing the slot in the re-render is not a reason to send nothing."""
        recorder = ChainRecorder([refreshed_sheet(), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        assert recorder.sources[1] == RESERVE_ID
        # Still rebuilt against the view the refresh established.
        assert recorder.requests[1]["javax.faces.ViewState"] == "vs-1"

    def test_refreshed_sheet_is_kept_for_diagnosis(self) -> None:
        """A blocked verdict is only readable next to the view that produced it."""
        recorder = ChainRecorder([MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        assert MOVED_RESERVE_ID in result.refresh_markup
        # And it has to survive the hand-off to the provider, which is what
        # uploads it beside the Reserve response.
        assert result.as_chain_result()["refreshMarkup"] == result.refresh_markup

    def test_countdown_sheet_is_kept_even_though_it_was_rejected(self) -> None:
        """The sheet that was still counting down is the whole diagnosis."""
        recorder = ChainRecorder([countdown_sheet("00:01:05")] * 4 + [PLAYER_PAGE, ROWS_PAGE])
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        assert "Booking Starts In" in result.refresh_markup
        assert result.timing["viewRefreshCountdownS"] == 65
        # Every attempt was spent on the refresh, and the Reserve went out
        # against the counting-down sheet rather than being silently skipped.
        assert recorder.sources[:5] == [DAY_TAB_ID] * 4 + [MOVED_RESERVE_ID]
        # Named as a failed outcome, not just annotated with a countdown.
        assert result.timing["viewRefreshFailed"] == "still-counting-down"

    def test_countdown_that_clears_leaves_no_marker_behind(self) -> None:
        """A run that counts down once then comes back clean is a clean run.

        Reporting the stale countdown here would send a post-mortem after the
        wrong cause - the sheet reserved against was open.
        """
        recorder = ChainRecorder(
            [countdown_sheet("00:00:02"), MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        assert result.success, result.error
        assert result.timing["viewRefreshAttempts"] == 2
        assert "viewRefreshCountdownS" not in result.timing
        assert "viewRefreshFailed" not in result.timing

    def test_no_refreshed_sheet_when_none_landed(self) -> None:
        """Nothing to capture, and the provider says so rather than staying quiet."""

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(urllib.parse.parse_qsl(request.content.decode()))
            if params.get("javax.faces.source") == DAY_TAB_ID:
                return httpx.Response(500)
            return httpx.Response(200, text=partial_response(PLAYER_PAGE))

        result = stage_refresh(booker_over(handler)).book(1, target_timestamp_ms=just_past())

        assert result.refresh_markup == ""

    def test_reserve_records_how_late_it_went_out(self) -> None:
        """Drift plus refresh cost is what actually decides the race."""
        recorder = ChainRecorder([MOVED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_refresh(make_booker(recorder)).book(1, target_timestamp_ms=just_past())

        # just_past() backdates the target by a second, so the Reserve is at
        # least that late; the point is that the number is recorded at all.
        assert result.timing["reserveSentAtMs"] >= 1000

    def test_refresh_is_a_pre_submit_phase(self) -> None:
        """Nothing is reserved by a re-render, so a browser retry stays safe."""
        assert PHASE_VIEW_REFRESH in PRE_SUBMIT_PHASES


class TestStageViewRefresh:
    """Resolving the refresh off the real tee sheet."""

    def _booker(self) -> DirectHttpBooker:
        """A booker over the captured sheet whose requests all succeed."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        return DirectHttpBooker(session)

    def test_stages_the_selected_day_tab_and_the_slot_time(self) -> None:
        """Staging reads both off the captured sheet."""
        booker = self._booker()
        booker.prepare(RESERVE_ID, TEE_SHEET, refresh_at_window=True)

        assert booker._refresh_config is not None
        assert booker._refresh_config.source == DAY_TAB_ID
        # Re-rendering the whole form is what makes the tab usable as a refresh.
        assert booker._refresh_config.update == FORM_ID
        assert booker._slot_time == RESERVE_SLOT_TIME

    def test_not_staged_unless_asked_for(self) -> None:
        """The default leaves the path exactly as it was."""
        booker = self._booker()
        booker.prepare(RESERVE_ID, TEE_SHEET)

        assert booker._refresh_config is None

    def test_sheet_without_a_day_tab_stages_no_refresh(self) -> None:
        """A sheet that cannot be refreshed still stages a bookable Reserve."""
        html = (
            f'<form id="{FORM_ID}" action="/post">'
            '<input type="hidden" name="javax.faces.ViewState" value="v">'
            f"{slot_block(RESERVE_ID, '04:34 PM')}</form>"
        )
        session = make_session(FormState.from_html(html), lambda request: httpx.Response(200))
        booker = DirectHttpBooker(session)
        booker.prepare(RESERVE_ID, html, refresh_at_window=True)

        assert booker._refresh_config is None
        assert booker._reserve_config is not None


# ---------------------------------------------------------------------------
# Walking the fallback list when the club refuses a slot
# ---------------------------------------------------------------------------

# Three consecutive tee times on the same sheet. The first is the one Reserve is
# staged against; the others are what the chain may fall back to.
SLOT_B_ID = f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:68:slotTee:0:reserve_button"
SLOT_C_ID = f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:69:slotTee:0:reserve_button"
SLOT_B_TIME = time(16, 42)
SLOT_C_TIME = time(16, 50)


def blocked_sheet(*slots: str, countdown: str | None = None) -> str:
    """A refusal, carrying the whole re-rendered sheet the way the site does.

    This shape is why the fallback loop needs no extra round trip: the response
    that says "blocked" hands back every other row's Reserve handler with it.

    ``countdown`` reproduces what the club actually returns when the Reserve
    re-renders a view built before the window - a refusal and a counter in the
    same body, which is the pairing that used to be misread as "too early".
    """
    banner = (
        f'<div class="booking-starts-in">Booking Starts In : {countdown}</div>' if countdown else ""
    )
    return refreshed_sheet(
        banner,
        '<div id="teeSheetValidationErrorPopup" aria-hidden="false">'
        "This slot is blocked by another user</div>",
        *slots,
    )


ALL_THREE = (
    slot_block(RESERVE_ID, "04:34 PM"),
    slot_block(SLOT_B_ID, "04:42 PM"),
    slot_block(SLOT_C_ID, "04:50 PM"),
)


def stage_fallbacks(booker: DirectHttpBooker, *times: time) -> DirectHttpBooker:
    """Give a booker from make_booker a slot time and a ranked fallback list."""
    booker._slot_time = RESERVE_SLOT_TIME
    booker._fallback_times = times
    return booker


class TestReserveFallbacks:
    """A refused Reserve moves to the next tee time instead of ending the run.

    The club renders a slot another member is holding as Available, so the
    refusal is the only evidence the slot is gone. One Reserve is therefore one
    guess, and on 2026-08-07 the guess lost while 86 other rows stayed open.
    """

    def test_a_clean_reserve_spends_no_fallback(self) -> None:
        """Winning the slot asked for must not cost an extra request."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME).book(1)

        assert result.success, result.error
        assert recorder.sources == [RESERVE_ID, "playerGroup", "bookTeeTimeAction"]
        assert result.booked_slot_time == RESERVE_SLOT_TIME
        assert result.attempted_times == [RESERVE_SLOT_TIME]

    def test_a_blocked_slot_falls_back_to_the_next_tee_time(self) -> None:
        """The refusal's own markup is what the next Reserve is built from."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME).book(1)

        assert result.success, result.error
        assert recorder.sources == [RESERVE_ID, SLOT_B_ID, "playerGroup", "bookTeeTimeAction"]
        assert result.timing["reserveAttempts"] == 2

    def test_the_booked_time_is_the_one_actually_held(self) -> None:
        """The caller books by row index and would otherwise report the wrong slot."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME).book(1)

        assert result.booked_slot_time == SLOT_B_TIME
        assert result.attempted_times == [RESERVE_SLOT_TIME, SLOT_B_TIME]

    def test_a_fallback_the_sheet_no_longer_offers_is_skipped(self) -> None:
        """No Reserve button means the slot is gone, not that it is worth a request."""
        recorder = ChainRecorder(
            [
                blocked_sheet(
                    slot_block(RESERVE_ID, "04:34 PM"),
                    slot_block(SLOT_C_ID, "04:50 PM"),
                ),
                PLAYER_PAGE,
                ROWS_PAGE,
                BOOKED_PAGE,
            ]
        )
        # SLOT_B ranks higher but is absent from the sheet that came back.
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME).book(1)

        assert result.success, result.error
        assert recorder.sources[1] == SLOT_C_ID
        assert result.booked_slot_time == SLOT_C_TIME

    def test_every_candidate_blocked_is_reported_as_blocked(self) -> None:
        """Running out of tee times is still a lost race, not a crash."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE)])
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME).book(1)

        assert not result.success
        assert result.blocked
        assert result.phase == PHASE_RESERVE_SENT
        assert result.attempted_times == [RESERVE_SLOT_TIME, SLOT_B_TIME, SLOT_C_TIME]
        assert "blocked" in (result.error or "").lower()

    def test_attempts_are_capped(self) -> None:
        """A sheet being taken apart around us is not worth unbounded requests."""
        from app.providers.walden_http_booker import _RESERVE_MAX_ATTEMPTS

        spare = [
            slot_block(
                f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:{70 + i}:slotTee:0:reserve_button",
                f"05:{i:02d} PM",
            )
            for i in range(_RESERVE_MAX_ATTEMPTS + 4)
        ]
        recorder = ChainRecorder([blocked_sheet(slot_block(RESERVE_ID, "04:34 PM"), *spare)])
        result = stage_fallbacks(
            make_booker(recorder), *[time(17, i) for i in range(_RESERVE_MAX_ATTEMPTS + 4)]
        ).book(1)

        assert result.blocked
        assert result.timing["reserveAttempts"] == _RESERVE_MAX_ATTEMPTS
        assert len(recorder.sources) == _RESERVE_MAX_ATTEMPTS

    def test_no_fallbacks_behaves_as_a_single_shot(self) -> None:
        """The old behaviour, for a booking with nothing ranked behind it."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE)])
        result = stage_fallbacks(make_booker(recorder)).book(1)

        assert result.blocked
        assert recorder.sources == [RESERVE_ID]


def sweep_booker(recorder: ChainRecorder, *offsets: int) -> DirectHttpBooker:
    """A booker staged with a sweep ladder and the usual three-slot sheet."""
    booker = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME)
    booker._sweep_offsets_ms = offsets
    return booker


def window_about_to_open(in_ms: int = 40) -> int:
    """A target instant just ahead, so the ladder's rungs are still in the future.

    The rungs below are spaced far wider than a mocked request needs, because a
    rung whose instant has already passed is deliberately skipped and a tight
    spacing would make that a race with the test runner rather than a check of
    the behaviour.
    """
    return int(time_module.time() * 1000) + in_ms


def window_long_past() -> int:
    """A target a minute gone - a later booking in the same batch."""
    return int(time_module.time() * 1000) - 60_000


class TestReserveSweep:
    """An early refusal is answered by asking again, not by giving up the slot.

    The club refuses for roughly the first second past the window and words it
    "This slot is blocked by another user" whatever the cause. On 2026-08-08 the
    same request was refused at 0ms and 812ms and accepted at 1291ms; 08-12
    repeated it at 0/817/1239ms with the whole sheet still open an hour later. So
    a refusal in the first second says nothing about who holds the slot, and
    walking down the fallback list on one gives away the tee time for free.
    """

    def test_an_early_refusal_asks_for_the_same_slot_again(self) -> None:
        """The second attempt is the slot we want, not the next one down."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, RESERVE_ID]
        assert result.booked_slot_time == RESERVE_SLOT_TIME

    def test_the_ladder_is_spent_before_any_fallback(self) -> None:
        """Only once asking again has stopped helping does the list get walked."""
        recorder = ChainRecorder(
            [
                blocked_sheet(*ALL_THREE),
                blocked_sheet(*ALL_THREE),
                PLAYER_PAGE,
                ROWS_PAGE,
                BOOKED_PAGE,
            ]
        )
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:3] == [RESERVE_ID, RESERVE_ID, SLOT_B_ID]
        assert result.booked_slot_time == SLOT_B_TIME

    def test_a_grant_mid_ladder_stops_the_sweep(self) -> None:
        """Rungs are a budget, not a quota - the slot is taken as soon as given."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = sweep_booker(recorder, 0, 300, 600, 900).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert result.success, result.error
        assert recorder.sources.count(RESERVE_ID) == 2

    def test_every_attempt_is_recorded(self) -> None:
        """The ledger is the whole point: refused attempts are the measurement."""
        recorder = ChainRecorder(
            [
                blocked_sheet(*ALL_THREE),
                blocked_sheet(*ALL_THREE),
                PLAYER_PAGE,
                ROWS_PAGE,
                BOOKED_PAGE,
            ]
        )
        result = sweep_booker(recorder, 0, 300, 600).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        verdicts = [observation.verdict for observation in result.attempt_log]
        assert verdicts == ["refused", "refused", "unknown"]
        assert [o.attempt for o in result.attempt_log] == [1, 2, 3]
        # Sent times are measured against the window, which is what makes a
        # losing morning still worth something.
        assert all(o.sent_ms_past_window is not None for o in result.attempt_log)

    def test_an_untimed_booking_does_not_sweep(self) -> None:
        """Ad-hoc bookings fire into a window that opened days ago.

        There is no boundary to find and no reason to ask twice, so the ladder
        must not apply - a refusal there goes straight to the fallback list.
        """
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = sweep_booker(recorder, 0, 300, 600).book(1)

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]

    def test_a_single_rung_ladder_is_the_old_behaviour(self) -> None:
        """The kill switch: WALDEN_RESERVE_SWEEP_OFFSETS_MS=0."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = sweep_booker(recorder, 0).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]

    def test_a_window_long_past_skips_the_ladder(self) -> None:
        """Later bookings in a batch must not fire the ladder as a burst.

        Every booking carries the same target timestamp, so for the second and
        later ones every rung is already behind us and each sleep would return
        instantly. Without skipping them the sweep becomes nine identical
        Reserves back to back - and by then the window is minutes old, so a
        refusal is far more likely to be a slot genuinely held.
        """
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = sweep_booker(recorder, 0, 300, 600).book(1, target_timestamp_ms=window_long_past())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]

    def test_the_tried_list_does_not_repeat_a_swept_slot(self) -> None:
        """One slot asked three times is one tee time, not three.

        The per-attempt list stays as it is - its length is the refusal count -
        but anything rendering it for a human reads the distinct one.
        """
        recorder = ChainRecorder(
            [
                blocked_sheet(*ALL_THREE),
                blocked_sheet(*ALL_THREE),
                blocked_sheet(*ALL_THREE),
                PLAYER_PAGE,
                ROWS_PAGE,
                BOOKED_PAGE,
            ]
        )
        result = sweep_booker(recorder, 0, 300, 600).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert result.attempted_times[:3] == [RESERVE_SLOT_TIME] * 3
        assert result.distinct_attempted_times() == [RESERVE_SLOT_TIME, SLOT_B_TIME]


class StallingRecorder(ChainRecorder):
    """A ChainRecorder where chosen attempts never answer.

    Models the failure of 2026-08-13 and 08-14 exactly: the Reserve is written
    to the socket and the club returns nothing before the budget runs out.
    """

    def __init__(
        self,
        pages: list[str],
        stall_on: set[int],
        *,
        stall_delay_s: float = 0.0,
        error: type[httpx.HTTPError] = httpx.ReadTimeout,
    ) -> None:
        """``stall_on`` holds 1-based attempt numbers that fail.

        ``stall_delay_s`` burns real time before failing. A stall that raises
        instantly leaves the ladder's later rungs still in the future, where
        _next_future_rung would hand one back and a test could not tell the
        rung-schedule fix from a plain catch-and-continue. ``error`` selects
        which httpx failure to raise, since the four timeout subclasses do not
        all mean the same thing.
        """
        super().__init__(pages)
        self.stall_on = stall_on
        self.stall_delay_s = stall_delay_s
        self.error = error
        # Answered requests only. The canned pages are the *replies* the club
        # makes, so a request it never answers must not consume one - indexing
        # on every request instead would silently hand the next attempt the page
        # meant for this one.
        self.answered = 0
        # Milliseconds past the window each request left at, so a test can show
        # a rung's instant had already gone by when its retry fired.
        self.sent_at_ms: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer as usual, unless this attempt is one of the stalled ones."""
        params = dict(urllib.parse.parse_qsl(request.content.decode()))
        # Recorded before any raise, so a stalled request still counts as sent -
        # which is the entire reason it cannot be treated as a no-op.
        self.requests.append(params)
        self.sources.append(params.get("javax.faces.source", ""))
        self.sent_at_ms.append(int(time_module.time() * 1000))
        if len(self.sources) in self.stall_on:
            if self.stall_delay_s:
                time_module.sleep(self.stall_delay_s)
            raise self.error("stalled by the test", request=request)
        page = self.pages[min(self.answered, len(self.pages) - 1)]
        self.answered += 1
        return httpx.Response(200, text=partial_response(page, f"vs-{len(self.sources)}"))


class TestReserveTimeoutKeepsTheLadderWalking:
    """A Reserve that never answers must not end the race.

    On 2026-08-13 and 08-14 attempt 2 fired at +1000ms, hit the 2s budget and
    raised, which ended the run at phase=reserve_sent with the ladder's 1250,
    1500 and 1750 rungs unfired - and ~1.24s is exactly where the club had
    granted the slot on 08-08 and 08-12.

    It stays contagious in one direction: the request may have reached the club,
    so the same slot may be asked for again, but no *other* tee time may be,
    now or later in the run. A second booking stacked on an invisible hold
    collides with the club's one-round-per-member-per-day rule.
    """

    def test_a_stalled_reserve_asks_the_same_slot_again(self) -> None:
        """The rung after a timeout is the slot we want, not the next one down."""
        recorder = StallingRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE], stall_on={1})
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, RESERVE_ID]
        assert result.booked_slot_time == RESERVE_SLOT_TIME

    def test_a_stalled_reserve_never_walks_to_a_fallback(self) -> None:
        """Even a later refusal must not move us onto a different tee time."""
        recorder = StallingRecorder(
            [blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE], stall_on={1}
        )
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert SLOT_B_ID not in recorder.sources
        assert SLOT_C_ID not in recorder.sources
        assert result.distinct_attempted_times() == [RESERVE_SLOT_TIME]

    def test_a_stall_with_the_ladder_spent_stops_without_claiming_blocked(self) -> None:
        """ "We never heard back" is not "another member took it".

        `blocked` picks the member-facing "someone else got it" message and gates
        the untimed retry, which must never re-fire at a slot the club may be
        holding.
        """
        recorder = StallingRecorder([blocked_sheet(*ALL_THREE)], stall_on={1})
        result = sweep_booker(recorder, 0).book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert not result.blocked
        assert recorder.sources == [RESERVE_ID]

    def test_the_stalled_attempt_is_recorded_in_the_ledger(self) -> None:
        """The attempt that decided both mornings left no row at all.

        The ledger read "1 attempt, every attempt refused" for a run whose second
        Reserve was the one that mattered.
        """
        from app.providers.walden_http_booker import RESERVE_TIMEDOUT

        recorder = StallingRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE], stall_on={1})
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert [o.verdict for o in result.attempt_log][:1] == [RESERVE_TIMEDOUT]
        assert result.attempt_log[0].slot_time == RESERVE_SLOT_TIME

    def test_consecutive_stalls_still_reach_a_later_rung(self) -> None:
        """The stall itself carries us past the remaining rungs' instants.

        This is the case a plain catch-and-continue does not survive. A real
        stall costs seconds, so by the time it raises every remaining rung is in
        the past, _next_future_rung discards all of them and the run ends with
        the ladder unwalked - which is exactly what 08-13 and 08-14 did. After a
        stall the ladder's length is still a budget; only its schedule is moot.

        The rungs are tight and the stall deliberately slower than all of them,
        so the retries provably fire after both instants have gone by rather
        than merely happening to be next in the list.
        """
        target = window_about_to_open()
        recorder = StallingRecorder(
            [PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE], stall_on={1, 2}, stall_delay_s=0.15
        )
        result = sweep_booker(recorder, 0, 60, 120).book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert recorder.sources[:3] == [RESERVE_ID, RESERVE_ID, RESERVE_ID]
        # Both retries left after the last rung's instant, so neither could have
        # come from _next_future_rung - it would have returned None for both.
        assert recorder.sent_at_ms[1] - target > 120
        assert recorder.sent_at_ms[2] - target > 120

    def test_a_reserve_that_never_connected_stays_pre_submit(self) -> None:
        """ConnectTimeout is not uncertainty - nothing was ever written.

        httpx.TimeoutException covers ConnectTimeout and PoolTimeout as well as
        ReadTimeout, but those two fire before any byte reaches the socket.
        Treating them as post-send would close the fallback list against a hold
        that cannot exist and, worse, leave the phase past PRE_SUBMIT_PHASES -
        which is what tells the provider a Selenium retry would race our own
        booking. A 6:30 that cannot open a socket can still be won by the
        browser chain.
        """
        recorder = StallingRecorder(
            [PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE],
            stall_on={1},
            error=httpx.ConnectTimeout,
        )
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert result.phase == PHASE_RESERVE_STAGED
        assert result.phase in PRE_SUBMIT_PHASES
        # Not swept and not blocked: there is no hold to protect, so the run is
        # simply handed back for the browser chain to retry.
        assert recorder.sources == [RESERVE_ID]
        assert not result.blocked

    def test_a_pool_timeout_is_also_pre_submit(self) -> None:
        """The other half of the never-sent pair, for the same reason."""
        recorder = StallingRecorder(
            [PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE], stall_on={1}, error=httpx.PoolTimeout
        )
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert result.phase in PRE_SUBMIT_PHASES

    def test_a_write_timeout_is_treated_as_uncertain(self) -> None:
        """A partly-written request may still have landed, so it sweeps on."""
        recorder = StallingRecorder(
            [PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE], stall_on={1}, error=httpx.WriteTimeout
        )
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, RESERVE_ID]


class PairRecorder(ChainRecorder):
    """Serves pages by *request* order, optionally answering some of them late.

    Request order rather than answer order: the opening pair is in flight
    together, so the sequence the club answers in is not the sequence it was
    asked in, and indexing on replies would hand a response to whichever request
    happened to be served first.
    """

    def __init__(
        self,
        pages: list[str],
        *,
        delay_on: set[int] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        """``delay_on`` holds 1-based request numbers answered after ``delay_s``."""
        super().__init__(pages)
        self.delay_on = delay_on or set()
        self.delay_s = delay_s
        self.sent_at_ms: list[int] = []
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer the ``index``-th request with the ``index``-th canned page."""
        params = dict(urllib.parse.parse_qsl(request.content.decode()))
        with self._lock:
            self.requests.append(params)
            self.sources.append(params.get("javax.faces.source", ""))
            index = len(self.sources)
            self.sent_at_ms.append(int(time_module.time() * 1000))
        if index in self.delay_on:
            time_module.sleep(self.delay_s)
        page = self.pages[min(index - 1, len(self.pages) - 1)]
        return httpx.Response(200, text=partial_response(page, f"vs-{index}"))


def paired_booker(recorder: ChainRecorder, *offsets: int) -> DirectHttpBooker:
    """A sweep booker whose first two rungs are fired together."""
    booker = sweep_booker(recorder, *offsets)
    booker._pipeline_opening_pair = True
    return booker


class TestOpeningPairIsFiredTogether:
    """The first two rungs overlap, so the ladder can ask twice in one round trip.

    Serially the ladder cannot: on 2026-08-15 the first Reserve went at -60ms and
    its refusal did not land until +940ms, by which point the +900 rung had gone
    and the next question could not go until +1240ms. That was tolerable while
    the first rung was 0 and expected to fail. It is not now that the first rung
    aims at the club's second tick - serially, a mis-measured tick would push the
    follow-up past the offset that has actually been winning.
    """

    def test_the_second_goes_before_the_first_is_answered(self) -> None:
        """The whole point: the pair overlaps rather than queueing."""
        recorder = PairRecorder(
            [blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE],
            delay_on={1},
            delay_s=0.4,
        )
        result = paired_booker(recorder, 0, 120).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        # Serialised, the second could not have left until the first was
        # answered 400ms later. The margin is wide because only the order of
        # magnitude is the claim.
        assert recorder.sent_at_ms[1] - recorder.sent_at_ms[0] < 300

    def test_the_pair_signals_the_quiet_worker(self) -> None:
        """The pipelined path answers the same contract as the single send.

        It never set the signal, so with the pair enabled the parked page kept
        its timers through the whole race - the exact CPU contention the signal
        exists to end, left running in the one mode built to be fastest.
        """
        recorder = PairRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        booker = paired_booker(recorder, 1030, 1250)
        booker.quiet_signal = threading.Event()
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert booker.quiet_signal.is_set()

    def test_a_pair_that_never_connected_leaves_the_page_alone(self) -> None:
        """Neither half reached the socket, so the JS chain still needs its page."""
        recorder = StallingRecorder([PLAYER_PAGE], stall_on={1, 2}, error=httpx.ConnectError)
        booker = paired_booker(recorder, 1030, 1250)
        booker.quiet_signal = threading.Event()
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert result.phase in PRE_SUBMIT_PHASES
        assert not booker.quiet_signal.is_set()

    def test_a_grant_on_the_later_rung_is_taken(self) -> None:
        """A refused aim point costs a rung, not the morning."""
        recorder = PairRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = paired_booker(recorder, 1030, 1250).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert result.success, result.error
        assert result.booked_slot_time == RESERVE_SLOT_TIME
        assert recorder.sources[:2] == [RESERVE_ID, RESERVE_ID]

    def test_a_grant_on_the_aim_point_is_taken(self) -> None:
        """The rung the change exists to win on."""
        recorder = PairRecorder([PLAYER_PAGE, blocked_sheet(*ALL_THREE), ROWS_PAGE, BOOKED_PAGE])
        result = paired_booker(recorder, 1030, 1250).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert result.success, result.error
        assert result.booked_slot_time == RESERVE_SLOT_TIME
        # The chain carried on from the granting response, not the refusal that
        # came back beside it - the player count is the step after Reserve.
        assert recorder.sources[2] == "playerGroup"

    def test_both_halves_ask_only_for_the_staged_slot(self) -> None:
        """Two requests, one tee time - the club cannot grant two holds."""
        recorder = PairRecorder([blocked_sheet(*ALL_THREE), blocked_sheet(*ALL_THREE)])
        paired_booker(recorder, 1030, 1250).book(1, target_timestamp_ms=window_about_to_open())

        assert recorder.sources[:2] == [RESERVE_ID, RESERVE_ID]

    def test_both_refused_still_reaches_the_fallback_list(self) -> None:
        """The pair replaces two rungs, it does not replace the ladder."""
        recorder = PairRecorder(
            [
                blocked_sheet(*ALL_THREE),
                blocked_sheet(
                    slot_block(RESERVE_ID, "04:34 PM"),
                    slot_block(SLOT_B_ID, "04:42 PM"),
                    slot_block(SLOT_C_ID, "04:50 PM"),
                ),
                PLAYER_PAGE,
                ROWS_PAGE,
                BOOKED_PAGE,
            ]
        )
        result = paired_booker(recorder, 0, 60).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        # Both halves asked for the staged slot, then the run moved on to the
        # best fallback the freshest sheet still offered.
        assert recorder.sources[:3] == [RESERVE_ID, RESERVE_ID, SLOT_B_ID]

    def test_both_refused_are_both_in_the_ledger(self) -> None:
        """Two questions asked is two rows, or a post-mortem reads one."""
        recorder = PairRecorder([blocked_sheet(*ALL_THREE), blocked_sheet(*ALL_THREE)])
        result = paired_booker(recorder, 1030, 1250).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert not result.success
        assert len(result.attempt_log) >= 2
        assert result.timing["reserveAttempts"] >= 2

    def test_neither_connecting_leaves_the_browser_chain_available(self) -> None:
        """Nothing was submitted, so a Selenium retry cannot race our own booking.

        The single-send path rolls the phase back for exactly this case, and the
        pair has to as well: past PRE_SUBMIT_PHASES the provider stops retrying,
        so a 6:30 that cannot open a socket would be lost outright rather than
        handed to the chain that can still win it.
        """
        recorder = StallingRecorder([PLAYER_PAGE], stall_on={1, 2}, error=httpx.ConnectError)
        booker = paired_booker(recorder, 1030, 1250)

        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert result.phase == PHASE_RESERVE_STAGED
        # Both halves were fired before the rollback. Serialised, the first
        # failure would have raised on its own and this would read 1 - which is
        # the reading that would let the pair path skip the rollback unnoticed.
        assert len(recorder.sources) == 2

    def test_one_half_landing_closes_the_fallback_list(self) -> None:
        """A read timeout may have reached the club, so no *other* slot is asked.

        The asymmetry that keeps the one-round-per-day rule: a request that never
        connected cannot be holding anything, but one that timed out mid-flight
        might be, and booking a second tee time on top of an invisible hold is
        worse than losing the morning.
        """
        recorder = StallingRecorder([PLAYER_PAGE], stall_on={1, 2}, error=httpx.ReadTimeout)
        booker = paired_booker(recorder, 1030, 1250)

        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        # Not "blocked": the club never said no, so the member is not told
        # another member took it, and the untimed retry stays shut.
        assert not result.blocked
        assert result.distinct_attempted_times() == [RESERVE_SLOT_TIME]


class TestReportingStaysInTheStatedWindowFrame:
    """The aim moved to 06:30:01; the scale everything is reported on did not.

    Ten mornings of evidence are recorded as milliseconds past the club's stated
    06:30:00 - refusals at -60, -14, -7, 0, 0, 812, 817 and grants at 1239, 1240,
    1291. If moving the aim also moved the frame, tomorrow's ledger would read
    "sent 0ms, accepted" and could not be compared with any of it.
    """

    def test_offsets_are_measured_from_the_window_not_the_aim(self) -> None:
        """A run aimed 1030ms late reports 1030, not 0."""
        recorder = PairRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        aim = window_about_to_open(20)
        result = sweep_booker(recorder, 0).book(
            1, target_timestamp_ms=aim, window_timestamp_ms=aim - 1030
        )

        assert result.success, result.error
        sent = result.attempt_log[0].sent_ms_past_window
        assert sent is not None
        # Wide, because the wait may overshoot; the point is the ~1030 offset is
        # present at all rather than having been zeroed out by the reframing.
        assert 950 < sent < 1500
        assert result.timing["reserveSentAtMs"] > 950

    def test_without_a_separate_window_the_frame_is_the_target(self) -> None:
        """The pre-existing behaviour, for every caller that passes one instant."""
        recorder = PairRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = sweep_booker(recorder, 0).book(1, target_timestamp_ms=window_about_to_open(20))

        assert result.success, result.error
        sent = result.attempt_log[0].sent_ms_past_window
        assert sent is not None
        assert -50 < sent < 500


class TestARungOurOwnLatencyOvershotStillFires:
    """A rung reached late is still asked, unless the whole window is stale.

    Rungs are reached only once the previous answer lands, so a round trip of
    593-828ms eats whatever rung falls inside it. With the opening pair resolving
    around aim+1000ms, the 1000 rung would fire on a fast morning and be dropped
    on a slow one - sending the run to the fallback list with an ask still owed
    to the tee time we actually wanted.

    The grace has to stay well short of the minutes by which a later booking in a
    batch overshoots its shared target, which is the case the discard exists for.
    """

    def test_a_rung_just_overshot_is_still_taken(self) -> None:
        """Our own round trip must not cost a retry."""
        from app.providers.walden_http_booker import _next_future_rung

        target = int(time_module.time() * 1000) - 1500

        assert _next_future_rung([1000], target) == 1000

    def test_a_rung_from_a_stale_window_is_dropped(self) -> None:
        """A later booking in a batch has no boundary left to find."""
        from app.providers.walden_http_booker import _next_future_rung

        target = int(time_module.time() * 1000) - 60_000

        assert _next_future_rung([0, 250, 1000], target) is None

    def test_a_rung_still_ahead_is_taken_unchanged(self) -> None:
        """The ordinary case the grace must not disturb."""
        from app.providers.walden_http_booker import _next_future_rung

        target = int(time_module.time() * 1000) + 500

        assert _next_future_rung([250, 1000], target) == 250


class TestTheAimPointIsTheFirstRung:
    """The precision wait sleeps to the first rung, not to the window.

    Every refusal on record arrived under +1000ms and every grant over +1200ms,
    with the club stamping its refusals inside the 06:30:00 second. Firing on the
    instant asks the one question that has never been answered yes.
    """

    def test_the_wait_targets_the_first_rung(self) -> None:
        """A first rung past the window delays the first request by that much."""
        recorder = PairRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        target = window_about_to_open(10)
        result = sweep_booker(recorder, 250, 400).book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert result.timing["openingOffsetMs"] == 250
        # Slack below the rung only: the wait may overshoot, never undershoot by
        # more than the spin window.
        assert recorder.sent_at_ms[0] - target > 200

    def test_a_zero_first_rung_still_fires_on_the_instant(self) -> None:
        """The historical behaviour stays reachable by configuration."""
        recorder = PairRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        target = window_about_to_open(10)
        result = sweep_booker(recorder, 0, 300).book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert result.timing["openingOffsetMs"] == 0
        # Generous, because this direction is bounded by the test runner rather
        # than by the code: sleep_until cannot fire early, but a loaded machine
        # can delay the send arbitrarily. Still far below the 1030ms default,
        # which is the value it has to be distinguishable from.
        assert recorder.sent_at_ms[0] - target < 500


class TestDetachedSendLeavesStateAlone:
    """Sending and adopting are separable, which is what makes the pair safe.

    ``post`` folds the response into the session's form state. Two in-flight
    requests doing that would race over the fields and ViewState the rest of the
    chain is built from, so the pair sends detached and adopts exactly one.
    """

    def test_a_detached_send_does_not_change_the_view_state(self) -> None:
        """The whole hazard, in one assertion."""
        recorder = ChainRecorder([PLAYER_PAGE])
        session = make_session(FormState.from_html(TEE_SHEET), recorder)
        config = AbConfig(source=RESERVE_ID, form=FORM_ID, update=FORM_ID)
        before = dict(session.form_state.fields)

        response = session.send_detached(config, body=session.build_body(config))

        assert response.markup
        assert dict(session.form_state.fields) == before

    def test_adopting_applies_what_the_detached_send_returned(self) -> None:
        """And the other half: nothing is lost by deferring it."""
        recorder = ChainRecorder([PLAYER_PAGE])
        session = make_session(FormState.from_html(TEE_SHEET), recorder)
        config = AbConfig(source=RESERVE_ID, form=FORM_ID, update=FORM_ID)
        response = session.send_detached(config, body=session.build_body(config))

        session.adopt(response)

        assert dict(session.form_state.fields) != {}


class TestStagingTheLadder:
    """The ladder as it arrives through the public staging path."""

    def test_the_staged_ladder_is_sorted_and_deduplicated(self) -> None:
        """An unordered rung would sleep backwards and fire a burst instead.

        _try_direct_http_booking passes the configured offsets straight through
        on every timed booking, so this normalization is the only thing standing
        between a mis-ordered setting and a collapsed sweep.
        """
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        booker = DirectHttpBooker(session)
        booker.prepare(RESERVE_ID, TEE_SHEET, sweep_offsets_ms=(300, 0, 300, 150))

        assert booker._sweep_offsets_ms == (0, 150, 300)

    def test_staging_without_a_ladder_is_a_single_shot(self) -> None:
        """The default stays one Reserve on the instant."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        booker = DirectHttpBooker(session)
        booker.prepare(RESERVE_ID, TEE_SHEET)

        assert booker._sweep_offsets_ms == (0,)


class TestCountdownIsNotAVerdict:
    """The countdown is logged and never branched on.

    It was briefly read as "this Reserve went out early", and it cannot answer
    that in either direction. Present tracks the age of the view being
    re-rendered, not the state of the window; absent can simply mean the
    response carried no tee sheet to hold one.
    """

    def test_a_countdown_beside_a_block_is_a_block(self) -> None:
        """The response the club actually sent on 2026-08-06: both at once.

        Reading the countdown as "too early" cost 1.3s of re-fires on 08-08
        while members were already booking. The block is the part that is about
        this slot, so it decides, and the fallback list gets walked.
        """
        recorder = ChainRecorder(
            [
                blocked_sheet(*ALL_THREE, countdown="00:01:05"),
                PLAYER_PAGE,
                ROWS_PAGE,
                BOOKED_PAGE,
            ]
        )
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME).book(1)

        assert result.success, result.error
        # Moved to the fallback rather than re-asking for the blocked slot.
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]
        assert result.booked_slot_time == SLOT_B_TIME
        assert "prematureRetries" not in result.timing

    def test_a_countdown_never_re_fires_the_same_slot(self) -> None:
        """No response makes the chain ask twice for one tee time.

        A countdown with no blocked popup is not a refusal at all, so the chain
        carries on into the rest of the booking and completes it. The accepted
        Reserve here answers with the player dialog *and* a stale counter beside
        it - 2026-08-08's shape, where the re-rendered fragment still carried the
        06:28:57 view's countdown. Asserting the booking completed is what keeps
        the count below meaningful: a chain that died at the Reserve would
        satisfy the count while proving nothing.
        """
        accepted_with_stale_countdown = PLAYER_PAGE.replace(
            '<div class="ui-selectonebutton" id="timeFilter">',
            '<div class="booking-starts-in">Booking Starts In : 00:00:02</div>\n'
            '  <div class="ui-selectonebutton" id="timeFilter">',
        )
        recorder = ChainRecorder([accepted_with_stale_countdown, ROWS_PAGE, BOOKED_PAGE])
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME).book(1)

        assert result.success, result.error
        assert recorder.sources.count(RESERVE_ID) == 1
        assert result.attempted_times == [RESERVE_SLOT_TIME]
        assert "prematureRetries" not in result.timing

    def test_a_countdown_only_response_is_not_a_booking(self) -> None:
        """Not classifying a countdown as a refusal is not calling it a booking.

        Dropping the countdown branch means a countdown-only sheet is no longer
        refused at the Reserve step - so the step after it has to be what stops
        it. It is: the chain needs the player selector to go on, and a tee sheet
        does not carry one, so the run fails in `player_count` and reports it.
        """
        recorder = ChainRecorder([countdown_sheet("00:01:05")])
        result = make_booker(recorder).book(4)

        assert not result.success
        assert result.phase == "player_count"
        assert result.error is not None and "Player count selector" in result.error

    def test_an_endless_countdown_still_walks_the_fallback_list(self) -> None:
        """A sheet that always counts down must not pin us to one slot.

        2026-08-08 stayed on 12:08 PM for three attempts on this reading, and
        the fallback list - the thing that survives a lost race - went unused.
        """
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE, countdown="00:01:05")])
        result = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME).book(1)

        assert not result.success
        assert result.blocked
        assert result.attempted_times == [RESERVE_SLOT_TIME, SLOT_B_TIME, SLOT_C_TIME]
        assert SLOT_B_ID in recorder.sources


# ---------------------------------------------------------------------------
# Timing the Reserve to arrive rather than to leave
# ---------------------------------------------------------------------------


def clock_handler(offset_s: float) -> Callable[[httpx.Request], httpx.Response]:
    """A site whose clock runs ``offset_s`` ahead of ours.

    The Date header is whole seconds, as a real one is - which is the point.
    The offset is not readable from any single response; it is readable from
    where the header ticks over relative to our clock.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Mock transport handler stamping the site's own clock."""
        server_now = time_module.time() + offset_s
        return httpx.Response(
            200, headers={"Date": email.utils.formatdate(server_now, usegmt=True)}
        )

    return handler


class TestClockProbeTarget:
    """Where the probes are sent, which is what decided 2026-08-08.

    A HEAD on the tee sheet is authenticated and makes Liferay render the whole
    portlet - 1.6s on three consecutive mornings, over the per-probe ceiling, so
    all 16 samples were dropped and the Reserve went out with no lead at all.
    """

    def test_a_cheap_static_asset_is_preferred_over_the_tee_sheet(self) -> None:
        """The probes must not land on the page that takes 1.6s to render."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(
                200, headers={"Date": email.utils.formatdate(time_module.time(), usegmt=True)}
            )

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        session.measure_clock_skew()

        assert seen, "no probe was sent"
        assert set(seen) == {"/o/frontend-css-web/main.css"}
        assert "/group/pages/book-a-tee-time" not in seen

    def test_an_unusable_asset_falls_through_to_the_next(self) -> None:
        """One missing asset must not put the probes back on the tee sheet."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            # The first candidate answers without a Date, which is unusable.
            if request.url.path == "/o/frontend-css-web/main.css":
                return httpx.Response(200)
            return httpx.Response(
                200, headers={"Date": email.utils.formatdate(time_module.time(), usegmt=True)}
            )

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        session.measure_clock_skew()

        # seen[0] pins the order: without it this passes even if resolution
        # skipped the first candidate and started at the theme asset.
        assert seen[0] == "/o/frontend-css-web/main.css"
        assert seen[1] == "/o/frontend-theme-font-awesome-web/css/main.css"
        assert set(seen[1:]) == {"/o/frontend-theme-font-awesome-web/css/main.css"}

    def test_no_usable_asset_falls_back_to_the_page(self) -> None:
        """A bad probe target still beats abandoning the measurement."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith(("/o/", "/favicon")):
                return httpx.Response(404)
            return httpx.Response(
                200, headers={"Date": email.utils.formatdate(time_module.time(), usegmt=True)}
            )

        session = make_session(FormState.from_html(TEE_SHEET), handler)

        assert session._resolve_probe_url().endswith("/group/pages/book-a-tee-time")


class TestSuppressProbeRequestLog:
    """The filter that replaces the earlier httpx-level mutation.

    Raising the "httpx" logger's level for the loop's duration was scoped to
    this call in intent but not in effect: the level is process-wide state,
    and WaldenGolfProvider's methods each run on their own thread via
    asyncio.to_thread(), so nothing prevented two bookings from racing to
    set and restore it. A filter instance is scoped to one probe_url and
    never mutates shared state, so it cannot suppress or corrupt another
    thread's unrelated httpx logging.
    """

    def test_matches_the_head_probe_line(self) -> None:
        probe_filter = _SuppressProbeRequestLog(
            "https://www.waldengolf.com/o/frontend-css-web/main.css"
        )
        record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: %s %s "%s %d %s"',
            args=(
                "HEAD",
                "https://www.waldengolf.com/o/frontend-css-web/main.css",
                "HTTP/1.1",
                200,
                "OK",
            ),
            exc_info=None,
        )

        assert probe_filter.filter(record) is False

    def test_does_not_match_a_different_url_or_method(self) -> None:
        probe_filter = _SuppressProbeRequestLog(
            "https://www.waldengolf.com/o/frontend-css-web/main.css"
        )

        other_url = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: %s %s "%s %d %s"',
            args=(
                "HEAD",
                "https://www.waldengolf.com/group/pages/book-a-tee-time",
                "HTTP/1.1",
                200,
                "OK",
            ),
            exc_info=None,
        )
        post_same_url = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: %s %s "%s %d %s"',
            args=(
                "POST",
                "https://www.waldengolf.com/o/frontend-css-web/main.css",
                "HTTP/1.1",
                200,
                "OK",
            ),
            exc_info=None,
        )

        assert probe_filter.filter(other_url) is True
        assert probe_filter.filter(post_same_url) is True


class TestClockProbeLogSuppression:
    """measure_clock_skew's use of the filter, end to end."""

    def test_probe_requests_are_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The ~100 identical HEAD probes must not each produce a log line.

        One "main.css" line is expected regardless: _resolve_probe_url()
        sends its own HEAD request to pick a target before the filter is
        installed, and that single resolution call is not part of what this
        suppresses. It is the probe *loop* - dozens of repeats of the same
        request - that must collapse to (at most) that one line.
        """
        session = make_session(FormState.from_html(TEE_SHEET), clock_handler(0.05))

        with caplog.at_level(logging.INFO, logger="httpx"):
            skew = session.measure_clock_skew()

        assert skew is not None
        httpx_main_css_lines = [
            record
            for record in caplog.records
            if record.name == "httpx" and "main.css" in record.getMessage()
        ]
        assert len(httpx_main_css_lines) <= 1

    def test_unrelated_httpx_logging_during_the_loop_survives(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A concurrent thread's httpx logging must not be caught in the blast radius.

        Stands in for a second booking's Reserve POST landing on the shared
        "httpx" logger while this thread's probe loop is in flight - exactly
        the scenario a level mutation could suppress.
        """
        httpx_logger = logging.getLogger("httpx")

        def handler(request: httpx.Request) -> httpx.Response:
            httpx_logger.info(
                'HTTP Request: %s %s "%s %d %s"',
                "POST",
                "https://other-thread/reserve",
                "HTTP/1.1",
                200,
                "OK",
            )
            return httpx.Response(
                200, headers={"Date": email.utils.formatdate(time_module.time(), usegmt=True)}
            )

        session = make_session(FormState.from_html(TEE_SHEET), handler)

        with caplog.at_level(logging.INFO, logger="httpx"):
            session.measure_clock_skew()

        assert any("other-thread/reserve" in record.getMessage() for record in caplog.records)

    def test_filter_is_removed_after_the_call(self) -> None:
        """No suppression must leak past the call that added it."""
        session = make_session(FormState.from_html(TEE_SHEET), clock_handler(0.05))
        httpx_logger = logging.getLogger("httpx")

        session.measure_clock_skew()

        assert not any(isinstance(f, _SuppressProbeRequestLog) for f in httpx_logger.filters)


class TestClockSkew:
    """Measuring how far ahead the club's clock is, and how far away it is."""

    def test_offset_is_read_from_where_the_date_header_ticks(self) -> None:
        """A one-second header still locates a boundary to within a probe gap."""
        session = make_session(FormState.from_html(TEE_SHEET), clock_handler(0.4))

        skew = session.measure_clock_skew()

        assert skew is not None
        # Loose enough for probe spacing and sleep jitter, tight enough that a
        # sign error or a whole-second misread would fail it.
        assert 250 < skew.offset_ms < 550
        assert skew.transitions >= 1
        assert session.clock_skew is skew

    def test_a_clock_behind_ours_reads_negative(self) -> None:
        """The lead must be able to come out smaller, not only larger."""
        session = make_session(FormState.from_html(TEE_SHEET), clock_handler(-0.4))

        skew = session.measure_clock_skew()

        assert skew is not None
        assert -550 < skew.offset_ms < -250

    def test_a_frozen_date_header_is_not_a_measurement(self) -> None:
        """A cache serving one Date says nothing about the origin's clock."""
        frozen = email.utils.formatdate(time_module.time(), usegmt=True)
        session = make_session(
            FormState.from_html(TEE_SHEET),
            lambda request: httpx.Response(200, headers={"Date": frozen}),
        )

        assert session.measure_clock_skew() is None

    def test_a_missing_date_header_is_not_a_measurement(self) -> None:
        """Nothing to read is reported as nothing, not as zero skew."""
        session = make_session(
            FormState.from_html(TEE_SHEET),
            lambda request: httpx.Response(200, headers={}),
        )

        assert session.measure_clock_skew() is None

    def test_the_lead_is_the_offset_plus_the_flight_out(self) -> None:
        """Both halves run against us, so both are in the lead."""
        from app.providers.walden_http import ClockSkew

        assert ClockSkew(offset_ms=250, one_way_ms=200, probes=9, transitions=2).lead_ms == 450

    def test_the_tick_is_pinned_tightly_enough_to_aim_at(self) -> None:
        """The bracket bounds the offset, and so any aim taken from it.

        The first sweep rung is now aimed 30ms past the club's second tick, so a
        measurement that only located the tick to +-105ms - which 16 probes at
        80ms did - would not be good enough to aim with. The bracket is reported
        so that a morning where it comes out wide is legible as one.
        """
        session = make_session(FormState.from_html(TEE_SHEET), clock_handler(0.4))

        skew = session.measure_clock_skew()

        assert skew is not None
        # Several transitions rather than the single one the old spacing yielded:
        # the median of them is what makes the estimate better than one bracket.
        assert skew.transitions >= 3
        assert 0 < skew.tick_bracket_ms < 60


class TestArrivalLead:
    """What the booker does with the measurement."""

    def _booker(self, handler: Callable[[httpx.Request], httpx.Response]) -> DirectHttpBooker:
        """A booker over a handler, with nothing staged - only the lead matters."""
        return DirectHttpBooker(make_session(FormState.from_html(TEE_SHEET), handler))

    def test_a_measured_skew_becomes_the_lead(self) -> None:
        """The whole point: send early by what the measurement says."""
        booker = self._booker(clock_handler(0.4))

        booker._stage_arrival_lead()

        assert 250 < booker._lead_ms < 560

    def test_an_unmeasurable_clock_sends_unled(self) -> None:
        """Being late is recoverable; guessing early is what is not."""
        booker = self._booker(lambda request: httpx.Response(200, headers={}))

        booker._stage_arrival_lead()

        assert booker._lead_ms == 0.0

    def test_a_probe_failure_does_not_break_staging(self) -> None:
        """Staging must never be what loses a booking."""

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler that refuses every probe."""
            raise httpx.ConnectError("no route")

        booker = self._booker(handler)

        booker._stage_arrival_lead()

        assert booker._lead_ms == 0.0

    def test_an_implausible_measurement_is_clamped(self) -> None:
        """A bad measurement is unbounded in the direction that hurts."""
        from app.providers.walden_http_booker import _MAX_ARRIVAL_LEAD_MS

        booker = self._booker(clock_handler(2.0))

        booker._stage_arrival_lead()

        assert booker._lead_ms == _MAX_ARRIVAL_LEAD_MS

    def test_the_reserve_goes_out_before_our_own_target(self) -> None:
        """The behaviour all of the above exists to produce."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        booker = stage_fallbacks(make_booker(recorder))
        booker._lead_ms = 400.0
        # Far enough out that the lead lands the send before it, close enough
        # that the test does not sit waiting.
        target = int(time_module.time() * 1000) + 250

        result = booker.book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert result.timing["arrivalLeadMs"] == 400
        # Negative: the request left before our clock reached the window.
        assert result.timing["reserveSentAtMs"] < 0

    def test_no_lead_still_fires_at_the_target(self) -> None:
        """An unmeasured clock leaves the old timing exactly as it was."""
        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        booker = stage_fallbacks(make_booker(recorder))
        target = int(time_module.time() * 1000) + 60

        result = booker.book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert result.timing["arrivalLeadMs"] == 0
        assert result.timing["reserveSentAtMs"] >= 0


class TestReviewFindings:
    """Cases raised in review of #140, each one a way to lose a morning quietly."""

    def test_a_naive_date_header_is_read_as_utc(self) -> None:
        """A ``-0000`` offset parses naive, and local time would put it hours out.

        The damage is not the wrong number; it is that a several-hour skew still
        produces an ordinary-looking clamped lead, so the run fires into a shut
        window every morning without anything in the log looking wrong.
        """
        from app.providers.walden_http import _parse_http_date

        # Same instant, three spellings the header is allowed to use.
        assert _parse_http_date("Mon, 01 Jan 2026 12:34:56 -0000") == _parse_http_date(
            "Mon, 01 Jan 2026 12:34:56 +0000"
        )
        assert _parse_http_date("Mon, 01 Jan 2026 12:34:56 GMT") == _parse_http_date(
            "Mon, 01 Jan 2026 12:34:56 -0000"
        )

    def test_an_unbelievable_offset_is_discarded_not_clamped(self) -> None:
        """Clamping a nonsense reading turns it into a confident wrong answer.

        Two internet-facing servers do not sit half a minute apart. Clamping
        that to 900ms would send every Reserve of the morning early and look
        entirely normal doing it; refusing to lead at all is recoverable.
        """
        booker = DirectHttpBooker(make_session(FormState.from_html(TEE_SHEET), clock_handler(30.0)))

        booker._stage_arrival_lead()

        assert booker._lead_ms == 0.0

    def test_a_stalled_reserve_does_not_spend_the_whole_race(self) -> None:
        """The attempt deadline cannot cancel a request already on the socket.

        Without a per-attempt budget one hung POST holds the session open past
        the window and the fallback list - which exists to survive exactly this
        - is never walked.
        """
        from app.providers.walden_http import DEFAULT_TIMEOUT_S
        from app.providers.walden_http_booker import _RESERVE_TIMEOUT_S

        assert _RESERVE_TIMEOUT_S < DEFAULT_TIMEOUT_S

        seen: list[float | None] = []
        session = make_session(
            FormState.from_html(TEE_SHEET),
            lambda request: httpx.Response(200, text=partial_response(PLAYER_PAGE)),
        )
        original = session.post

        def recording_post(config, *, body=None, timeout_s=None):
            """Record the budget each request was given."""
            seen.append(timeout_s)
            return original(config, body=body, timeout_s=timeout_s)

        booker = DirectHttpBooker(session)
        config = AbConfig(source=RESERVE_ID, form=FORM_ID, update=FORM_ID)
        booker._reserve_config = config
        booker._reserve_body = session.build_body(config)
        booker.session.post = recording_post  # type: ignore[method-assign]

        booker.book(1)

        assert seen[0] == _RESERVE_TIMEOUT_S

    def test_a_timed_out_reserve_is_terminal_not_a_fallback(self) -> None:
        """The request may have reached the club, and a second hold is worse.

        Walking to the next tee time here could leave the member holding two
        slots on a course that allows one round a day - trading a lost morning
        for a booking to unpick.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler standing in for a request that never answers."""
            raise httpx.ReadTimeout("timed out")

        booker = stage_fallbacks(booker_over(handler), SLOT_B_TIME, SLOT_C_TIME)

        result = booker.book(1)

        assert not result.success
        assert not result.blocked
        # Past the Reserve POST, so the caller must not retry it in the browser.
        assert result.phase == PHASE_RESERVE_SENT
        assert result.phase not in PRE_SUBMIT_PHASES
        assert result.attempted_times == [RESERVE_SLOT_TIME]


def gated_sheet(open_: bool, *slots: str) -> str:
    """A refusal sheet carrying the club's own open/closed marker.

    The datascroller's ``disable-div`` class is the live "this sheet is not
    open for booking" signal (established 2026-08-21, #160/#161); the popup
    rides along because every real refusal carries it whatever the cause.
    """
    scroller = (
        f'<div id="{FORM_ID}:teeTimeCourses:0:teeTimeSlots" '
        f'class="ui-datascroller ui-widget{"" if open_ else " disable-div"}">'
        + "".join(slots)
        + "</div>"
    )
    return refreshed_sheet(
        '<div id="teeSheetValidationErrorPopup" aria-hidden="false">'
        "This slot is blocked by another user</div>",
        scroller,
    )


CLOSED_SHEET = gated_sheet(False, *ALL_THREE)
OPEN_BLOCKED_SHEET = gated_sheet(True, *ALL_THREE)


def hold_booker(
    recorder: ChainRecorder, *, cap_ms: int = 8000, interleave: bool = True
) -> DirectHttpBooker:
    """A booker running the hold-until-open policy over the usual three slots."""
    booker = stage_fallbacks(make_booker(recorder), SLOT_B_TIME, SLOT_C_TIME)
    booker._sweep_offsets_ms = (0,)
    booker._hold_until_open = True
    booker._hold_cap_ms = cap_ms
    booker._target_interleave = interleave
    return booker


class TestHoldUntilOpen:
    """A closed sheet cannot lose a slot, so refusals on one spend nothing.

    Both Friday races on record (2026-08-21 and 08-28) spent every ask for the
    target into a sheet the club's own reply rendered closed, then walked off to
    the fallback list seconds before the club granted anything to anyone. The
    member who took the target both weeks was simply still asking when the
    sheet opened. This policy makes the bot the one still asking.
    """

    def test_closed_refusals_re_ask_the_target_and_spend_nothing(self) -> None:
        """Refusals on a closed sheet repeat the same slot, not the next one."""
        recorder = ChainRecorder(
            [CLOSED_SHEET, CLOSED_SHEET, CLOSED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = hold_booker(recorder).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:4] == [RESERVE_ID, RESERVE_ID, RESERVE_ID, RESERVE_ID]
        assert result.booked_slot_time == RESERVE_SLOT_TIME
        assert result.timing["reserveAttempts"] == 4

    def test_the_hold_outlasts_the_legacy_attempt_budget(self) -> None:
        """Ten closed refusals must not exhaust a budget sized for six asks."""
        from app.providers.walden_http_booker import _RESERVE_MAX_ATTEMPTS

        closed_streak = [CLOSED_SHEET] * (_RESERVE_MAX_ATTEMPTS + 4)
        recorder = ChainRecorder([*closed_streak, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = hold_booker(recorder).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources.count(RESERVE_ID) == len(closed_streak) + 1
        assert result.booked_slot_time == RESERVE_SLOT_TIME

    def test_an_open_sheet_refusal_starts_the_fallback_walk(self) -> None:
        """The first refusal that arrives on an open sheet is a real one."""
        recorder = ChainRecorder([OPEN_BLOCKED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = hold_booker(recorder).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]
        assert result.booked_slot_time == SLOT_B_TIME

    def test_the_target_is_re_asked_between_fallbacks(self) -> None:
        """The win the policy exists for: the target granted on a later re-ask."""
        recorder = ChainRecorder(
            [OPEN_BLOCKED_SHEET, OPEN_BLOCKED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = hold_booker(recorder).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:3] == [RESERVE_ID, SLOT_B_ID, RESERVE_ID]
        assert result.booked_slot_time == RESERVE_SLOT_TIME

    def test_interleave_off_walks_the_fallbacks_straight_through(self) -> None:
        """Without interleave the open-sheet walk is the caller's exact rule."""
        recorder = ChainRecorder(
            [OPEN_BLOCKED_SHEET, OPEN_BLOCKED_SHEET, PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE]
        )
        result = hold_booker(recorder, interleave=False).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert result.success, result.error
        assert recorder.sources[:3] == [RESERVE_ID, SLOT_B_ID, SLOT_C_ID]
        assert result.booked_slot_time == SLOT_C_TIME

    def test_the_hold_cap_ends_the_hold(self) -> None:
        """A sheet that never opens must not hold the race hostage."""
        recorder = ChainRecorder([CLOSED_SHEET])
        result = hold_booker(recorder, cap_ms=0).book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert result.blocked
        # Cap spent on the first refusal: the walk proceeds, target interleaved.
        assert recorder.sources == [RESERVE_ID, SLOT_B_ID, RESERVE_ID, SLOT_C_ID, RESERVE_ID]

    def test_a_sheet_with_no_marker_counts_as_open(self) -> None:
        """An unreadable sheet fails toward progress, not an unbounded hold."""
        recorder = ChainRecorder([blocked_sheet(*ALL_THREE), PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        result = hold_booker(recorder).book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]

    def test_untimed_bookings_run_the_same_policy(self) -> None:
        """One execution path: an ad-hoc refusal walks with the target interleaved.

        An ad-hoc booking's sheet opened days ago, so the hold itself no-ops -
        but the loop is the same one the race runs, which is what lets a
        Tuesday-afternoon booking exercise Friday's code.
        """
        recorder = ChainRecorder([OPEN_BLOCKED_SHEET])
        result = hold_booker(recorder).book(1)

        assert not result.success
        assert recorder.sources == [RESERVE_ID, SLOT_B_ID, RESERVE_ID, SLOT_C_ID, RESERVE_ID]

    def test_an_untimed_closed_sheet_is_still_capped(self) -> None:
        """No window to measure the cap from, so it runs from the first Reserve."""
        recorder = ChainRecorder([CLOSED_SHEET])
        result = hold_booker(recorder, cap_ms=0).book(1)

        assert not result.success
        assert recorder.sources == [RESERVE_ID, SLOT_B_ID, RESERVE_ID, SLOT_C_ID, RESERVE_ID]


# ---------------------------------------------------------------------------
# The opening burst
# ---------------------------------------------------------------------------

# What the club actually returns when it grants a slot: its booking form, with
# the populated "Reservation at" header the classifier keys on. PLAYER_PAGE
# alone classifies as neither grant nor refusal - the ladder treats that as
# progress, the burst does not, because a burst member that is a 200 error page
# must not be mistaken for a win and stop the burst.
ACCEPTED_PAGE = PLAYER_PAGE.replace(
    '<div class="ui-selectonebutton" id="playerGroup">',
    "<label>Reservation at</label><label>04:34 PM</label>"
    '<div class="ui-selectonebutton" id="playerGroup">',
)


class SourceRecorder(ChainRecorder):
    """Answers each Reserve button by its own page queue, thread-safely.

    Burst members are in flight together and ask for different slots, so the
    reply has to be chosen by *which* slot was asked, not by request order.
    Sources with no queue of their own - the chain steps after a grant - are
    served from ``chain`` in order. ``status_on`` answers the given 1-based
    request numbers with that HTTP status instead, ``stall_on`` raises a read
    timeout for them, and ``delay_on`` answers them late. Every 200 carries a
    Date header, because the gate summary is read off the club's clock.
    """

    def __init__(
        self,
        by_source: dict[str, list[str]],
        chain: list[str],
        *,
        status_on: dict[int, int] | None = None,
        status_body: str = "<html>Too Many Requests</html>",
        stall_on: set[int] | None = None,
        delay_on: set[int] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        """Queue the per-slot pages and the chain pages that follow a grant."""
        super().__init__(chain)
        self.by_source = by_source
        self.status_on = status_on or {}
        self.status_body = status_body
        self.stall_on = stall_on or set()
        self.delay_on = delay_on or set()
        self.delay_s = delay_s
        self.sent_at_ms: list[int] = []
        self._served: dict[str, int] = {}
        self._chain_served = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer by the requested source, or fail the request as configured."""
        params = dict(urllib.parse.parse_qsl(request.content.decode()))
        source = params.get("javax.faces.source", "")
        with self._lock:
            self.requests.append(params)
            self.sources.append(source)
            index = len(self.sources)
            self.sent_at_ms.append(int(time_module.time() * 1000))
        if index in self.stall_on:
            raise httpx.ReadTimeout("stalled by the test", request=request)
        if index in self.status_on:
            return httpx.Response(
                self.status_on[index],
                text=self.status_body,
                headers={"Retry-After": "2", "Date": email.utils.formatdate(usegmt=True)},
            )
        if index in self.delay_on:
            time_module.sleep(self.delay_s)
        with self._lock:
            queue = self.by_source.get(source)
            if queue is None:
                page = self.pages[min(self._chain_served, len(self.pages) - 1)]
                self._chain_served += 1
            else:
                served = self._served.get(source, 0)
                self._served[source] = served + 1
                page = queue[min(served, len(queue) - 1)]
        return httpx.Response(
            200,
            text=partial_response(page, f"vs-{index}"),
            headers={"Date": email.utils.formatdate(usegmt=True)},
        )


BLOCKED_ALL = blocked_sheet(*ALL_THREE)
CHAIN_AFTER_GRANT = [ROWS_PAGE, BOOKED_PAGE]


def burst_booker(
    recorder: ChainRecorder,
    *offsets: int,
    target_only: int = 4,
    fallbacks: tuple[time, ...] = (SLOT_B_TIME, SLOT_C_TIME),
) -> DirectHttpBooker:
    """A booker in burst mode, its fallbacks staged the way prepare() stages them."""
    booker = stage_fallbacks(make_booker(recorder), *fallbacks)
    booker._opening_mode = OPENING_MODE_BURST
    booker._burst_offsets_ms = offsets
    booker._burst_target_only = target_only
    sheet = refreshed_sheet(*ALL_THREE)
    document = parse_html(sheet)
    booker._burst_fallback_requests = []
    for candidate in fallbacks:
        relocated = _relocate_reserve_in(document, sheet, candidate)
        assert relocated is not None
        booker._burst_fallback_requests.append(
            (candidate, relocated, booker.session.build_body(relocated))
        )
    return booker


class TestOpeningBurst:
    """The opening is asked as a pipelined burst, not one round trip at a time.

    Every Friday on record lost its target between the first ask and the second,
    700-1030ms later - one round trip plus a parse, the ladder's cadence. The
    burst keeps a request landing every hundred-odd milliseconds through the
    first seconds, target and top fallbacks alike, and the first grant wins.
    """

    def test_members_overlap_rather_than_queue(self) -> None:
        """Three members leave inside 300ms although the first takes 400ms to answer."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [BLOCKED_ALL], SLOT_C_ID: [ACCEPTED_PAGE]},
            CHAIN_AFTER_GRANT,
            delay_on={1},
            delay_s=0.4,
        )
        booker = burst_booker(recorder, 0, 60, 120, target_only=3)
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert recorder.sources[:3] == [RESERVE_ID, RESERVE_ID, RESERVE_ID]
        assert recorder.sent_at_ms[2] - recorder.sent_at_ms[0] < 300
        assert result.timing["burstMembers"] == 3
        assert result.timing["openingMode"] == "burst"

    def test_the_first_grant_wins_and_unsent_members_are_skipped(self) -> None:
        """A win on the first ask must not go on to take a second hold."""
        recorder = SourceRecorder({RESERVE_ID: [ACCEPTED_PAGE]}, CHAIN_AFTER_GRANT)
        booker = burst_booker(recorder, 0, 300, 600, target_only=1)
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert result.booked_slot_time == RESERVE_SLOT_TIME
        # The members at +300 and +600 - one of them a fallback - reached
        # their instants after the grant had landed and were not sent.
        assert recorder.sources == [RESERVE_ID, "playerGroup", "bookTeeTimeAction"]
        assert result.timing["burstSkipped"] == 2
        assert result.timing["burstAccepted"] == 0

    def test_a_fallback_in_the_burst_is_granted_when_the_target_is_gone(self) -> None:
        """Both Friday wins were fallback grants at +5s; the burst asks at +0.1s."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [ACCEPTED_PAGE]}, CHAIN_AFTER_GRANT
        )
        booker = burst_booker(recorder, 0, 30, 60, 90, target_only=2)
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert result.booked_slot_time == SLOT_B_TIME
        granted = [o for o in result.attempt_log if o.verdict == RESERVE_ACCEPTED]
        assert [o.slot_time for o in granted] == [SLOT_B_TIME]
        assert granted[0].burst_index == 2
        assert SLOT_B_TIME in result.attempted_times

    def test_the_targets_grant_outranks_a_fallbacks(self) -> None:
        """When both are granted, the tee time the member asked for is the one kept."""
        recorder = SourceRecorder(
            {RESERVE_ID: [ACCEPTED_PAGE], SLOT_B_ID: [ACCEPTED_PAGE]},
            CHAIN_AFTER_GRANT,
            delay_on={1},
            delay_s=0.2,
        )
        booker = burst_booker(recorder, 0, 30, target_only=1)
        with _CaptureLogs("app.providers.walden_http_booker") as records:
            result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert result.booked_slot_time == RESERVE_SLOT_TIME
        assert any("surplus hold" in r.getMessage() for r in records)

    def test_a_status_error_is_recorded_and_closes_nothing(self) -> None:
        """A 429 is the club declining to reserve; the walk goes on and the row says so."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [BLOCKED_ALL], SLOT_C_ID: [ACCEPTED_PAGE]},
            CHAIN_AFTER_GRANT,
            status_on={1: 429},
        )
        booker = burst_booker(recorder, 0, 30, 60, target_only=3)
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert result.booked_slot_time == SLOT_C_TIME
        errored = [o for o in result.attempt_log if o.verdict == RESERVE_ERRORED]
        assert len(errored) == 1
        assert errored[0].status_code == 429
        assert errored[0].response_headers["retry-after"] == "2"
        assert errored[0].error_body is not None and "Too Many" in errored[0].error_body
        row = errored[0].as_row()
        assert row["statusCode"] == 429 and row["burstIndex"] == 0

    def test_a_socket_timeout_without_a_grant_closes_the_fallback_walk(self) -> None:
        """A member that never answered may be holding its slot; nothing else is asked."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [ACCEPTED_PAGE]},
            CHAIN_AFTER_GRANT,
            stall_on={2},
        )
        booker = burst_booker(recorder, 0, 30, 60, target_only=3)
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        assert not result.blocked
        assert SLOT_B_ID not in recorder.sources
        assert [o.verdict for o in result.attempt_log].count(RESERVE_TIMEDOUT) == 1

    def test_the_walk_after_the_burst_covers_the_list_again(self) -> None:
        """Fallbacks refused inside the burst are asked again, later, from the target."""
        recorder = SourceRecorder(
            {
                RESERVE_ID: [BLOCKED_ALL],
                SLOT_B_ID: [BLOCKED_ALL],
                SLOT_C_ID: [BLOCKED_ALL, ACCEPTED_PAGE],
            },
            CHAIN_AFTER_GRANT,
        )
        booker = burst_booker(recorder, 0, 30, target_only=1)
        result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        assert result.booked_slot_time == SLOT_C_TIME
        # Burst: T, B. Walk: B (already asked in the burst, asked again), C
        # refused, list spent; second pass from the target: T, B, C granted.
        assert recorder.sources.count(SLOT_C_ID) == 2
        assert recorder.sources.count(RESERVE_ID) >= 2

    def test_the_quiet_signal_is_raised_on_a_grant_and_never_on_a_refusal(self) -> None:
        """The page's timers stay alive until a slot is held.

        2026-09-04: quieted at +1.6s on a refusal, and every later answer was
        the same frozen pre-window render.
        """
        refused = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [BLOCKED_ALL], SLOT_C_ID: [BLOCKED_ALL]},
            CHAIN_AFTER_GRANT,
        )
        booker = burst_booker(refused, 0, 30, target_only=2)
        booker.quiet_signal = threading.Event()
        result = booker.book(1, target_timestamp_ms=window_about_to_open())
        assert not result.success
        assert not booker.quiet_signal.is_set()

        granted = SourceRecorder({RESERVE_ID: [ACCEPTED_PAGE]}, CHAIN_AFTER_GRANT)
        booker = burst_booker(granted, 0, 30, target_only=2)
        booker.quiet_signal = threading.Event()
        result = booker.book(1, target_timestamp_ms=window_about_to_open())
        assert result.success, result.error
        assert booker.quiet_signal.is_set()

    def test_identical_refusals_are_flagged_as_a_frozen_view(self) -> None:
        """Fourteen identical bodies took a post-mortem to notice; now it is a field."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [BLOCKED_ALL], SLOT_C_ID: [BLOCKED_ALL]},
            CHAIN_AFTER_GRANT,
        )
        booker = burst_booker(recorder, 0, 30, 60, target_only=3)
        with _CaptureLogs("app.providers.walden_http_booker") as records:
            result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        burst_rows = [o for o in result.attempt_log if o.burst_index is not None]
        assert [o.identical_to_previous for o in burst_rows][1:] == [True, True]
        assert burst_rows[0].body_digest is not None
        assert any("frozen view" in r.getMessage() for r in records)

    def test_the_gate_summary_names_the_first_granted_second(self) -> None:
        """A grant for any slot in a club-second proves the gate was open in it."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [ACCEPTED_PAGE]}, CHAIN_AFTER_GRANT
        )
        booker = burst_booker(recorder, 0, 30, target_only=1)
        with _CaptureLogs("app.providers.walden_http_booker") as records:
            result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        gate = [r.getMessage() for r in records if r.getMessage().startswith("GATE:")]
        assert any("GRANTED 04:42 PM" in line for line in gate)
        assert any("gate was open by club" in line for line in gate)

    def test_member_zero_fires_after_the_pool_is_warm(self) -> None:
        """The pool spin-up is paid before the instant, not in front of the send.

        Submitting eleven members into a cold pool measured 3.5ms median and
        4.9ms worst, all of it otherwise landing on the one send the morning is
        timed around. _run_chain wakes a warm-up budget early and the burst
        redoes the wait precisely, so member 0's drift is small and positive
        rather than carrying the spin-up.
        """
        recorder = SourceRecorder({RESERVE_ID: [ACCEPTED_PAGE]}, CHAIN_AFTER_GRANT)
        booker = burst_booker(recorder, 0, 300, 600, 900, target_only=4)
        target = window_about_to_open(in_ms=300)
        result = booker.book(1, target_timestamp_ms=target)

        assert result.success, result.error
        # The warm-up wake is its own number, and the precise wait is the one
        # reported as the click drift.
        assert "burstWarmupDriftMs" in result.timing
        assert result.timing["clickDriftMs"] >= 0
        assert result.timing["clickDriftMs"] < _BURST_WARMUP_MS
        # And the send really did land on the instant, not before it.
        assert recorder.sent_at_ms[0] >= target - 1

    def test_unanswered_asks_are_counted_outside_the_club_seconds(self) -> None:
        """A 429 has no club clock, so it belongs to no second and is named apart.

        It used to be summed inside the per-second line, where it could never
        appear: those rows are built from answers, and an ask the club never
        answered carries no Date header to bucket it by.
        """
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [ACCEPTED_PAGE]},
            CHAIN_AFTER_GRANT,
            status_on={1: 429},
        )
        booker = burst_booker(recorder, 0, 30, target_only=1)
        with _CaptureLogs("app.providers.walden_http_booker") as records:
            result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert result.success, result.error
        gate = [r.getMessage() for r in records if r.getMessage().startswith("GATE:")]
        assert any("GRANTED" in line for line in gate)
        assert any("no readable answer" in line and "status 429" in line for line in gate)

    def test_a_wholly_throttled_burst_still_says_so(self) -> None:
        """Nothing answered is the case the summary most needs to report."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [BLOCKED_ALL]},
            CHAIN_AFTER_GRANT,
            status_on={1: 429, 2: 429, 3: 429},
        )
        booker = burst_booker(recorder, 0, 30, target_only=2, fallbacks=())
        with _CaptureLogs("app.providers.walden_http_booker") as records:
            result = booker.book(1, target_timestamp_ms=window_about_to_open())

        assert not result.success
        gate = [r.getMessage() for r in records if r.getMessage().startswith("GATE:")]
        assert any("no readable answer" in line and "status 429" in line for line in gate)

    def test_a_status_body_is_redacted_before_it_is_kept(self) -> None:
        """The error body is logged and uploaded, so session tokens never enter it."""
        session_body = (
            "<html><a href='/book;jsessionid=ABC123SECRET'>retry</a>"
            "<input name='javax.faces.ViewState' value='-8840302615059009897:138'>"
            "<a href='/x?p_auth=Ab3xQ9'>home</a> Too Many Requests</html>"
        )

        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL]},
            CHAIN_AFTER_GRANT,
            status_on={1: 429},
            status_body=session_body,
        )
        result = burst_booker(recorder, 0, 30, target_only=2, fallbacks=()).book(
            1, target_timestamp_ms=window_about_to_open()
        )

        assert not result.success
        errored = [o for o in result.attempt_log if o.verdict == RESERVE_ERRORED]
        assert errored, result.error
        body = errored[0].error_body or ""
        assert "ABC123SECRET" not in body
        assert "8840302615059009897" not in body
        assert "Ab3xQ9" not in body
        # The club's own words survive - that is the whole diagnostic value.
        assert "Too Many Requests" in body
        assert body.count("<redacted>") == 3

    def test_an_untimed_booking_sends_once_then_walks(self) -> None:
        """No instant to burst around: the ad-hoc untimed retry is one ask, then the walk."""
        recorder = SourceRecorder(
            {RESERVE_ID: [BLOCKED_ALL], SLOT_B_ID: [ACCEPTED_PAGE]}, CHAIN_AFTER_GRANT
        )
        booker = burst_booker(recorder, 0, 30, 60, target_only=3)
        result = booker.book(1)

        assert result.success, result.error
        assert recorder.sources[:2] == [RESERVE_ID, SLOT_B_ID]
        assert "burstMembers" not in result.timing


class TestNon200Responses:
    """A status other than 200 is an answer, and the answer is kept."""

    def test_a_non_200_carries_status_headers_and_body(self) -> None:
        """What a rate limit looks like from the ledger's side."""

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            return httpx.Response(
                429,
                text="<html>Slow down</html>",
                headers={"Retry-After": "3", "Set-Cookie": "JSESSIONID=secret"},
            )

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        with pytest.raises(DirectHttpStatusError) as raised:
            session.post(AbConfig(source="s", form=FORM_ID))

        assert raised.value.status_code == 429
        assert raised.value.headers["retry-after"] == "3"
        # The token itself never leaves the transport layer.
        assert raised.value.headers["set-cookie"] == "present"
        assert "Slow down" in raised.value.body_snippet
        assert isinstance(raised.value, DirectHttpError)


class _CaptureLogs:
    """Collect the records a logger emits inside a ``with`` block."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._handler = _ListHandler()

    def __enter__(self) -> list[logging.LogRecord]:
        self._logger.addHandler(self._handler)
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        return self._handler.records

    def __exit__(self, *exc: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)


class _ListHandler(logging.Handler):
    """A handler that keeps every record it is given."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record."""
        self.records.append(record)
