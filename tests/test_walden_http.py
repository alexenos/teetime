"""
Tests for the direct-HTTP (browser-free) booking path.

The protocol tests run against the real captured tee sheet in
tests/fixtures/, so the request this builds is the request the site actually
expects - not one derived from a hand-written mock.
"""

import urllib.parse
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.providers.walden_http import (
    AbConfig,
    DirectHttpError,
    FormState,
    PrimeFacesSession,
    ViewExpiredError,
    find_ab_for_element,
    parse_ab_call,
    parse_html,
    parse_partial_response,
    sleep_until,
    visible_text,
)
from app.providers.walden_http_booker import (
    PHASE_RESERVE_SENT,
    DirectHttpBooker,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEE_SHEET = (FIXTURES / "walden_tee_time_loaded.html").read_text(encoding="utf-8", errors="replace")

FORM_ID = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm"
RESERVE_ID = f"{FORM_ID}:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:reserve_button"


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

        # The invariant is that it never returns early. The drift bound is
        # deliberately loose: it is bounded by OS scheduler latency, so a tight
        # assertion just makes this flaky on a loaded CI runner.
        assert int(time_module.time() * 1000) >= target
        assert 0 <= drift < 250


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


class ChainRecorder:
    """Serves canned responses in order, recording each request's source."""

    def __init__(self, pages: list[str]) -> None:
        """Queue the pages this recorder will serve, in order."""
        self.pages = pages
        self.sources: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Serve the next canned page and record the request's source."""
        params = dict(urllib.parse.parse_qsl(request.content.decode()))
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
        """A dropped connection becomes a result, not an exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            """Mock transport handler for one test case."""
            raise httpx.ConnectError("connection reset")

        session = make_session(FormState.from_html(TEE_SHEET), handler)
        booker = DirectHttpBooker(session)
        booker._reserve_config = AbConfig(source=RESERVE_ID, form=FORM_ID)
        booker._reserve_body = b"staged=reserve"
        result = booker.book(4)

        assert not result.success
        assert result.phase == PHASE_RESERVE_SENT
        assert result.error is not None and "connection reset" in result.error

    def test_book_without_prepare_is_rejected(self) -> None:
        """The Reserve request must be staged before the race."""
        session = make_session(FormState.from_html(TEE_SHEET), lambda request: httpx.Response(200))
        with pytest.raises(DirectHttpError, match="prepare"):
            DirectHttpBooker(session).book(4)

    def test_invalid_player_count_is_rejected(self) -> None:
        """Player counts outside 1-4 are refused up front."""
        booker = make_booker(ChainRecorder([BOOKED_PAGE]))
        with pytest.raises(DirectHttpError, match="num_players"):
            booker.book(5)

    def test_timed_mode_waits_for_the_target(self) -> None:
        """Timed mode holds the POST until the target instant."""
        import time as time_module

        recorder = ChainRecorder([PLAYER_PAGE, ROWS_PAGE, BOOKED_PAGE])
        target = int(time_module.time() * 1000) + 60
        result = make_booker(recorder).book(1, target_timestamp_ms=target)

        assert result.success, result.error
        assert int(time_module.time() * 1000) >= target
        assert "clickDriftMs" in result.timing
        # Loose for the same reason as test_waits_until_the_target.
        assert 0 <= result.timing["clickDriftMs"] < 250


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
