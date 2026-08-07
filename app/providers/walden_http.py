"""
Direct-HTTP booking path for the Walden Golf / Northstar tee sheet.

Background
----------
The tee sheet is a JSF/PrimeFaces portlet running inside Liferay. Every
interaction the Selenium chain performs by clicking - Reserve, the player-count
button, each TBD guest, Book Now - is, underneath, a single
``application/x-www-form-urlencoded`` POST that PrimeFaces builds from the
element's ``PrimeFaces.ab({...})`` handler and sends as an XHR. The server
answers with a ``<partial-response>`` XML document containing the re-rendered
markup and a fresh ``javax.faces.ViewState``.

Nothing about that exchange requires a browser. This module speaks the same
protocol directly with ``httpx``:

    click Reserve  ==  POST encodedURL
                       javax.faces.partial.ajax=true
                       javax.faces.source=<reserve button id>
                       javax.faces.partial.execute=@all
                       javax.faces.partial.render=<form id>
                       <reserve button id>=<reserve button id>
                       javax.faces.ViewState=<current view state>
                       + the form's serialized fields (~1.2 KB total)

Why it is faster is not the click itself - the staged JS chain (#113, #116)
already fires with ~0 ms drift. It is everything the browser does *between*
clicks:

* Each response re-renders ``u:"...teeTimeForm"`` - the entire form. In Chrome
  that is a ~700 KB HTML parse plus style/layout before the next step's element
  exists. Over HTTP it is a regex over an XML payload.
* The JS chain can only discover that work finished by polling every 10 ms, and
  spaces the TBD clicks with fixed 200 ms sleeps so PrimeFaces can settle.
  Request/response is strictly ordered - the next POST goes out the moment the
  previous response lands.
* Between bookings in a batch the browser re-navigates and re-scrolls the tee
  sheet (~23 s, per #122). Over HTTP a second booking is just another POST on
  the same session.

Scope and safety
----------------
This module deliberately does *not* replace login, navigation, course/date
selection or slot discovery. Those all happen minutes before the booking window
opens, where latency is irrelevant and the Selenium path is proven. The HTTP
session is adopted *from* a live, already-navigated WebDriver
(:meth:`PrimeFacesSession.from_selenium`), inheriting its cookies and the exact
form state the browser had built up.

The booking window itself is unchanged: the reserve POST is fired at the same
target timestamp the JS chain would have clicked at, never earlier.

Every step after Reserve is derived from the markup the server just returned, by
parsing the same ``PrimeFaces.ab({...})`` handler the browser would have
executed. Nothing about the request sequence is hardcoded, so a component id
churning from ``j_idt1076`` to ``j_idt1082`` does not break the chain.
"""

import email.utils
import logging
import re
import time as time_module
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)


# Elements that never carry an end tag; the tree builder must not nest into them.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# Request budget for a single PrimeFaces POST. The whole chain is 3-6 of these,
# and a stalled one at 6:30 is worth abandoning to the Selenium fallback fast.
DEFAULT_TIMEOUT_S = 10.0

# Final busy-wait before the target timestamp, mirroring the JS chain's 25 ms
# spin. Python only needs a couple of ms since there is no DOM work left to do.
_SPIN_WINDOW_MS = 3

# Clock-skew probing. The `Date` header is the club's own clock, but at one
# second of resolution, so a single sample says nothing better than "somewhere
# in this second". What is readable is the *transition*: probe faster than the
# second ticks, and the local time at which the header increments is a server
# second boundary observed directly. Spacing bounds the error (+-40 ms here);
# the count has to span more than a second so at least one transition is
# guaranteed to fall inside the run.
_SKEW_PROBE_COUNT = 16
_SKEW_PROBE_SPACING_S = 0.08
# A probe is only usable if its own round trip is short enough that the header
# was stamped near the midpoint we ascribe to it. A stalled probe is dropped
# rather than allowed to drag the estimate.
_SKEW_MAX_PROBE_RTT_S = 1.0


@dataclass(frozen=True)
class ClockSkew:
    """How far the club's clock and the network sit between us and the window.

    ``offset_ms`` is the club's clock minus ours: positive means the club gets
    to 06:30:00 before we do. ``one_way_ms`` is the outbound flight time. Their
    sum is how early a request has to leave to arrive as the window opens.
    """

    offset_ms: float
    one_way_ms: float
    probes: int
    transitions: int

    @property
    def lead_ms(self) -> float:
        """How far ahead of our own target a request should be sent."""
        return self.offset_ms + self.one_way_ms


def _median(values: list[float]) -> float:
    """Median of a non-empty list, without pulling in a dependency for it."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _parse_http_date(header: str | None) -> int | None:
    """Read an HTTP ``Date`` header as whole epoch seconds, or None."""
    if not header:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return int(parsed.timestamp())


class DirectHttpError(RuntimeError):
    """Raised when the direct-HTTP path cannot complete a step.

    Always recoverable by the caller: the Selenium chain remains a valid way to
    perform the same booking, so callers catch this and fall back.
    """


class ViewExpiredError(DirectHttpError):
    """The server rejected our ViewState - the adopted session is stale."""


# ---------------------------------------------------------------------------
# Minimal HTML tree
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A single element in the lightweight DOM built by :func:`parse_html`."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None
    text: str = ""

    @property
    def id(self) -> str:
        """The element's ``id`` attribute, or an empty string."""
        return self.attrs.get("id", "")

    @property
    def classes(self) -> frozenset[str]:
        """The element's CSS classes as a set."""
        return frozenset(self.attrs.get("class", "").split())

    def descendants(self) -> "list[Node]":
        """All nodes beneath this one, in document order."""
        out: list[Node] = []
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(node.children))
        return out

    def text_content(self) -> str:
        """This element's text plus all descendant text, whitespace-collapsed."""
        parts = [self.text] + [d.text for d in self.descendants()]
        return " ".join(p for p in parts if p).strip()

    def find_by_id(self, element_id: str) -> "Node | None":
        """Find the first descendant with the given id."""
        for node in self.descendants():
            if node.id == element_id:
                return node
        return None

    def find_all(self, tag: str | None = None, **attr_match: str) -> "list[Node]":
        """Find descendants by tag name and exact attribute values."""
        found = []
        for node in self.descendants():
            if tag is not None and node.tag != tag:
                continue
            if any(node.attrs.get(k) != v for k, v in attr_match.items()):
                continue
            found.append(node)
        return found

    def find_with_class(self, css_class: str) -> "list[Node]":
        """Find descendants carrying a CSS class."""
        return [n for n in self.descendants() if css_class in n.classes]


class _TreeBuilder(HTMLParser):
    """Builds a :class:`Node` tree, tolerating the tee sheet's unclosed tags."""

    def __init__(self) -> None:
        """Start an empty document tree."""
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#document")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an element, descending into it unless it is a void tag."""
        node = Node(
            tag=tag,
            attrs={k: (v if v is not None else "") for k, v in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Add a self-closing element without descending into it."""
        node = Node(
            tag=tag,
            attrs={k: (v if v is not None else "") for k, v in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        """Close the matching open element, ignoring stray end tags."""
        # Unwind to the matching open tag. Stray end tags are ignored rather
        # than allowed to pop the stack past their opener.
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        """Attach non-whitespace text to the element currently open."""
        stripped = data.strip()
        if stripped:
            node = self._stack[-1]
            node.text = f"{node.text} {stripped}".strip() if node.text else stripped


def parse_html(html: str) -> Node:
    """Parse an HTML document or fragment into a :class:`Node` tree."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def visible_text(html: str) -> str:
    """Extract user-visible text from markup, ignoring script and style bodies.

    The HTTP-side counterpart of reading ``body.text`` from a WebDriver: it lets
    the same success/failure phrase checks run against a partial response as
    against a rendered page. Script contents are excluded so a JS string literal
    cannot be mistaken for page copy.
    """
    document = parse_html(html)
    parts = []
    for node in [document, *document.descendants()]:
        if node.text and not _has_ancestor_tag(node, ("script", "style")):
            parts.append(node.text)
    return " ".join(parts).strip()


def _has_ancestor_tag(node: Node, tags: tuple[str, ...]) -> bool:
    """Report whether the node sits inside any of the given tags."""
    current: Node | None = node
    while current is not None:
        if current.tag in tags:
            return True
        current = current.parent
    return False


# ---------------------------------------------------------------------------
# PrimeFaces.ab handler parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbConfig:
    """A parsed ``PrimeFaces.ab({...})`` call - one AJAX request's parameters.

    Mirrors the PrimeFaces shorthand keys: ``s`` source, ``f`` form, ``p``
    process (execute), ``u`` update (render), ``e`` behavior event.
    """

    source: str
    form: str
    process: str | None = None
    update: str | None = None
    event: str | None = None


# PrimeFaces.ab({s:"...",f:"...",p:"...",u:"..."}) - values may be quoted with
# either quote style, and `this` appears unquoted for inline handlers.
_AB_CALL_RE = re.compile(r"PrimeFaces\.ab\(\s*\{(?P<body>.*?)\}\s*[,)]", re.S)
_AB_KEY_RE = re.compile(r"(?<![\w$])(?P<key>[sfpue])\s*:\s*(?P<value>\"[^\"]*\"|'[^']*'|this\b)")


def parse_ab_call(js: str, *, element_id: str | None = None) -> AbConfig | None:
    """Parse the first ``PrimeFaces.ab({...})`` call in a JS snippet.

    Args:
        js: JavaScript source - typically an ``onclick`` attribute value.
        element_id: Id substituted for ``s:this``, which PrimeFaces emits for
            handlers bound inline to the element they act on.

    Returns:
        The parsed config, or None when the snippet has no parseable call or
        lacks the source/form pair every request needs.
    """
    call = _AB_CALL_RE.search(js)
    if not call:
        return None

    values: dict[str, str] = {}
    for match in _AB_KEY_RE.finditer(call.group("body")):
        raw = match.group("value")
        if raw == "this":
            if element_id is None:
                continue
            values[match.group("key")] = element_id
        else:
            values[match.group("key")] = raw[1:-1]

    source = values.get("s")
    form = values.get("f")
    if not source or not form:
        return None

    return AbConfig(
        source=source,
        form=form,
        process=values.get("p"),
        update=values.get("u"),
        event=values.get("e"),
    )


def find_ab_for_element(node: Node, document_html: str) -> AbConfig | None:
    """Resolve the AJAX request a click on ``node`` would have produced.

    Checks the element's own event attributes first, then - for PrimeFaces
    widgets such as ``selectOneButton``, whose behaviors live in a widget-init
    script rather than an inline handler - searches the document for a call
    whose source is this element's id.
    """
    for attr in ("onclick", "onchange", "onmousedown"):
        handler = node.attrs.get(attr)
        if handler:
            config = parse_ab_call(handler, element_id=node.id)
            if config:
                return config

    if not node.id:
        return None

    for call in _AB_CALL_RE.finditer(document_html):
        config = parse_ab_call(f"PrimeFaces.ab({{{call.group('body')}}})")
        if config and config.source == node.id:
            return config
    return None


# ---------------------------------------------------------------------------
# Form state
# ---------------------------------------------------------------------------

_VIEW_STATE_PARAM = "javax.faces.ViewState"
_ENCODED_URL_PARAM = "javax.faces.encodedURL"


@dataclass
class FormState:
    """The serialized state of a JSF form, as a browser would submit it.

    ``fields`` holds the form's successful controls in document order - exactly
    what ``$(form).serialize()`` would produce, which is what PrimeFaces posts
    for a non-``partialSubmit`` request.
    """

    form_id: str
    action_url: str
    fields: list[tuple[str, str]]

    @property
    def view_state(self) -> str | None:
        """The form's current ``javax.faces.ViewState``, if it carries one."""
        for name, value in self.fields:
            if name == _VIEW_STATE_PARAM:
                return value
        return None

    def set_field(self, name: str, value: str) -> None:
        """Replace (or append) a single field value, preserving field order."""
        for index, (existing, _) in enumerate(self.fields):
            if existing == name:
                self.fields[index] = (name, value)
                return
        self.fields.append((name, value))

    @classmethod
    def from_html(cls, html: str, *, form_id: str | None = None) -> "FormState":
        """Extract a form's submittable state from a rendered page.

        Args:
            html: Full page source.
            form_id: Id of the form to read. Defaults to the first form that
                carries a ViewState, which on the tee sheet is ``teeTimeForm``.
        """
        document = parse_html(html)
        forms = document.find_all("form")

        target: Node | None = None
        if form_id is not None:
            target = next((f for f in forms if f.id == form_id), None)
            if target is None:
                raise DirectHttpError(f"Form {form_id!r} not found in page")
        else:
            for candidate in forms:
                if any(
                    n.attrs.get("name") == _VIEW_STATE_PARAM for n in candidate.find_all("input")
                ):
                    target = candidate
                    break
            if target is None:
                raise DirectHttpError("No JSF form (none carrying a ViewState) found in page")

        fields = _serialize_form(target)

        # PrimeFaces posts to javax.faces.encodedURL when the portlet bridge
        # renders one (it routes to the resource phase), falling back to the
        # form's action. This mirrors PrimeFaces.ajax.Utils.getPostUrl.
        action_url = next(
            (value for name, value in fields if name == _ENCODED_URL_PARAM),
            target.attrs.get("action", ""),
        )
        if not action_url:
            raise DirectHttpError(f"Form {target.id!r} has no action or encodedURL to post to")

        return cls(form_id=target.id, action_url=action_url, fields=fields)


def _serialize_form(form: Node) -> list[tuple[str, str]]:
    """Serialize a form's successful controls the way a browser would.

    Skips unnamed, disabled, and unchecked controls, and buttons (which are
    only submitted when they are the activating control - PrimeFaces adds the
    source parameter explicitly for that).
    """
    serialized: list[tuple[str, str]] = []
    for node in form.descendants():
        if node.tag not in ("input", "select", "textarea"):
            continue
        name = node.attrs.get("name")
        if not name or "disabled" in node.attrs:
            continue

        if node.tag == "select":
            for option in node.find_all("option"):
                if "selected" in option.attrs:
                    serialized.append((name, option.attrs.get("value", option.text_content())))
            continue

        if node.tag == "textarea":
            serialized.append((name, node.text_content()))
            continue

        input_type = node.attrs.get("type", "text").lower()
        if input_type in ("submit", "button", "reset", "image", "file"):
            continue
        if input_type in ("radio", "checkbox") and "checked" not in node.attrs:
            continue
        serialized.append((name, node.attrs.get("value", "")))

    return serialized


# ---------------------------------------------------------------------------
# Partial response
# ---------------------------------------------------------------------------


@dataclass
class PartialResponse:
    """A parsed JSF ``<partial-response>`` document."""

    updates: dict[str, str] = field(default_factory=dict)
    view_state: str | None = None
    redirect_url: str | None = None
    error_name: str | None = None
    error_message: str | None = None

    @property
    def markup(self) -> str:
        """All re-rendered markup concatenated, for element lookups."""
        return "\n".join(
            value for key, value in self.updates.items() if _VIEW_STATE_PARAM not in key
        )


def parse_partial_response(xml: str) -> PartialResponse:
    """Parse a JSF partial-response payload.

    Raises:
        ViewExpiredError: if the server reports an expired/invalid view.
        DirectHttpError: if the payload is not a partial-response at all -
            typically an HTML error or login page, meaning the session died.
    """
    try:
        root = ElementTree.fromstring(xml.strip())
    except ElementTree.ParseError as exc:
        raise DirectHttpError(
            f"Response was not a JSF partial-response (parse failed: {exc}); "
            f"first 200 chars: {xml[:200]!r}"
        ) from exc

    if root.tag != "partial-response":
        raise DirectHttpError(
            f"Response was not a JSF partial-response (root element {root.tag!r}); "
            "the session has most likely expired"
        )

    response = PartialResponse()

    error = root.find("error")
    if error is not None:
        name_node = error.find("error-name")
        message_node = error.find("error-message")
        response.error_name = (name_node.text or "").strip() if name_node is not None else None
        response.error_message = (
            (message_node.text or "").strip() if message_node is not None else None
        )
        if response.error_name and "ViewExpired" in response.error_name:
            raise ViewExpiredError(
                f"Server rejected the view state: {response.error_name} "
                f"{response.error_message or ''}".strip()
            )
        raise DirectHttpError(
            f"Server returned {response.error_name}: {response.error_message or ''}".strip()
        )

    redirect = root.find("redirect")
    if redirect is not None:
        response.redirect_url = redirect.get("url")

    for update in root.iter("update"):
        update_id = update.get("id") or ""
        content = update.text or ""
        response.updates[update_id] = content
        # JSF namespaces the ViewState update id, e.g.
        # "_teeTimePortlet_WAR_northstarportlet_:javax.faces.ViewState:0".
        if _VIEW_STATE_PARAM in update_id:
            response.view_state = content.strip()

    return response


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class PrimeFacesSession:
    """Speaks the PrimeFaces AJAX protocol directly over HTTP.

    Holds the cookie jar and the current form state, refreshing the ViewState
    from every partial response the way the browser's JSF runtime does.
    """

    def __init__(
        self,
        form_state: FormState,
        cookies: dict[str, str],
        *,
        base_url: str,
        user_agent: str = "Mozilla/5.0",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        """Build a session around a form's state and a browser's cookies.

        Args:
            form_state: The form this session will submit.
            cookies: Cookie jar, normally adopted from a live WebDriver.
            base_url: URL used to warm the connection and resolve relative actions.
            user_agent: Sent on every request; match the browser's.
            timeout_s: Per-request budget.
            client: Optional pre-built client (tests inject a mock transport).
        """
        self.form_state = form_state
        self.base_url = base_url
        # Filled in by warm_up/measure_clock_skew during staging. None means not
        # measured, which the booker treats as "do not lead" rather than
        # guessing - a wrong lead fires into a window the club has not opened.
        self.warm_up_rtt_ms: float | None = None
        self.clock_skew: ClockSkew | None = None
        headers = {
            "User-Agent": user_agent,
            # PrimeFaces sets these on every AJAX request; the bridge uses
            # Faces-Request to route the call to the partial lifecycle.
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/xml, text/xml, */*; q=0.01",
        }
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        # Applied after construction so an injected client (tests, or a caller
        # supplying its own transport) gets the same protocol headers and cookie
        # jar as one we build - otherwise the request under test is not the
        # request production sends.
        self._client.headers.update(headers)
        self._client.cookies.update(cookies)

    @classmethod
    def from_selenium(
        cls,
        driver: Any,
        *,
        form_id: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> "PrimeFacesSession":
        """Adopt a live browser session: its cookies and its current form state.

        Everything the browser established - login, course selection, the
        selected date - is carried in the session cookies plus the ~12 form
        fields this reads, so the HTTP session resumes exactly where the
        browser is rather than rebuilding any of it.
        """
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        if not cookies:
            raise DirectHttpError("Browser session has no cookies to adopt")

        form_state = FormState.from_html(driver.page_source, form_id=form_id)
        user_agent = str(driver.execute_script("return navigator.userAgent;"))

        logger.info(
            "DIRECT_HTTP: Adopted browser session - form=%s, %d cookies, %d form fields, "
            "viewState=%s",
            form_state.form_id,
            len(cookies),
            len(form_state.fields),
            "present" if form_state.view_state else "MISSING",
        )
        return cls(
            form_state,
            cookies,
            base_url=str(driver.current_url),
            user_agent=user_agent,
            timeout_s=timeout_s,
        )

    def build_body(self, config: AbConfig) -> bytes:
        """Build the exact POST body PrimeFaces would have sent for ``config``.

        Ordered to match the browser: the form's serialized fields first, then
        the partial-request parameters PrimeFaces appends.
        """
        params: list[tuple[str, str]] = list(self.form_state.fields)
        params.append(("javax.faces.partial.ajax", "true"))
        params.append(("javax.faces.source", config.source))
        # PrimeFaces defaults execute to @all when a component declares no
        # process list - the same default JSF applies to command components.
        params.append(("javax.faces.partial.execute", config.process or "@all"))
        if config.update:
            params.append(("javax.faces.partial.render", config.update))
        if config.event:
            params.append(("javax.faces.behavior.event", config.event))
            params.append(("javax.faces.partial.event", config.event))
        # The activating control submits its own name - this is what makes JSF
        # decode the click as an action on that component. (The form's own
        # marker field is already among the serialized fields.)
        params.append((config.source, config.source))
        return urllib.parse.urlencode(params).encode("utf-8")

    def warm_up(self) -> float:
        """Open the TLS connection ahead of the race.

        Returns the round-trip time in ms. Without this the first POST pays DNS
        + TCP + TLS - easily 100-300 ms - at exactly the wrong moment.
        """
        started = time_module.perf_counter()
        try:
            # HEAD first: the base URL is the portlet page, and a GET makes the
            # server render the whole tee sheet seconds before the window for a
            # body we discard. Liferay may not serve HEAD, so fall back to GET.
            response = self._client.head(self.base_url)
            if not response.is_success:
                response = self._client.get(self.base_url)
        except httpx.HTTPError as exc:
            raise DirectHttpError(
                f"Failed to warm up connection to {self.base_url}: {exc}"
            ) from exc

        rtt_ms = (time_module.perf_counter() - started) * 1000
        if not response.is_success:
            # Anything but 2xx here means the adopted cookies did not carry the
            # session - a logged-out portal answers with a redirect to the login
            # page. Better to fail now, while falling back to the browser chain
            # is still free, than as a baffling Reserve failure at 6:30.
            raise DirectHttpError(
                f"Warm-up of {self.base_url} returned HTTP {response.status_code}; "
                "the adopted session may not be authenticated"
            )
        logger.info(
            "DIRECT_HTTP: Connection warm, HTTP %d, round trip %.0fms",
            response.status_code,
            rtt_ms,
        )
        self.warm_up_rtt_ms = rtt_ms
        return rtt_ms

    def measure_clock_skew(self) -> ClockSkew | None:
        """Measure the club's clock against ours, and how long a request takes.

        Both halves of the same question: at what local instant should a request
        leave so that it *arrives* as the window opens. Firing at our own
        06:30:00.000 answers a different question, and answers it late twice
        over - once for the offset between the two clocks, once for the flight
        time.

        Sampling is NTP-shaped but built around a one-second header. Each probe
        pairs a local midpoint with the second the server stamped; where two
        adjacent probes straddle an increment, the boundary between them is a
        server second boundary, and the local time of that boundary is the
        offset. Several transitions are usually visible, and the median of them
        is what survives one slow probe.

        Returns:
            The measurement, or None when it could not be taken - a header the
            site does not send, a clock frozen by a cache, every probe failing.
            None means "do not lead", not "lead by zero on purpose"; the caller
            distinguishes them.
        """
        samples: list[tuple[float, int]] = []
        round_trips: list[float] = []
        for probe in range(_SKEW_PROBE_COUNT):
            if probe:
                time_module.sleep(_SKEW_PROBE_SPACING_S)
            sent = time_module.time()
            try:
                response = self._client.head(self.base_url)
            except httpx.HTTPError as exc:
                # One failed probe is not worth abandoning the measurement, and
                # is not worth a warning each: the summary below reports how
                # many landed, which is the number that matters.
                logger.debug("DIRECT_HTTP: Clock probe %d failed (%s)", probe, exc)
                continue
            received = time_module.time()

            round_trip = received - sent
            if round_trip > _SKEW_MAX_PROBE_RTT_S:
                continue
            server_second = _parse_http_date(response.headers.get("Date"))
            if server_second is None:
                continue
            round_trips.append(round_trip)
            samples.append(((sent + received) / 2, server_second))

        if len(samples) < 2:
            logger.warning(
                "DIRECT_HTTP: Clock skew unmeasurable - %d usable probe(s) of %d",
                len(samples),
                _SKEW_PROBE_COUNT,
            )
            return None

        offsets = [
            # The increment happened between these two probes, so the server's
            # own second boundary sits at their midpoint by our clock. Its
            # distance from the whole second the server reported is the offset.
            later_second - (earlier_mid + later_mid) / 2
            for (earlier_mid, earlier_second), (later_mid, later_second) in zip(
                samples, samples[1:]
            )
            # Exactly one tick. A wider jump means the probes fell too far apart
            # to say where inside it the boundary was.
            if later_second - earlier_second == 1
        ]
        if not offsets:
            logger.warning(
                "DIRECT_HTTP: Clock skew unmeasurable - the Date header never advanced "
                "across %d probes spanning %.1fs (cached?)",
                len(samples),
                _SKEW_PROBE_COUNT * _SKEW_PROBE_SPACING_S,
            )
            return None

        skew = ClockSkew(
            offset_ms=_median(offsets) * 1000,
            # Halved on the usual assumption that the two legs are symmetric.
            # Only the outbound leg is on the critical path: the response's
            # journey back happens after the club has already acted.
            one_way_ms=_median(round_trips) * 1000 / 2,
            probes=len(samples),
            transitions=len(offsets),
        )
        logger.info(
            "DIRECT_HTTP: Clock skew measured - club is %+.0fms from us, one-way %.0fms "
            "(%d probes, %d transitions)",
            skew.offset_ms,
            skew.one_way_ms,
            skew.probes,
            skew.transitions,
        )
        self.clock_skew = skew
        return skew

    def post(
        self,
        config: AbConfig,
        *,
        body: bytes | None = None,
        timeout_s: float | None = None,
    ) -> PartialResponse:
        """Send one PrimeFaces AJAX request and fold the response into state.

        Args:
            config: The request to send.
            body: Pre-built body from :meth:`build_body`, for the timed path
                where serialization must not happen on the critical path.
            timeout_s: Per-request budget, overriding the session's. The
                session default is sized for a chain step that must not be
                abandoned; a request made *during* the race needs to give up
                far sooner than that and try something else.
        """
        payload = body if body is not None else self.build_body(config)
        # httpx distinguishes "no timeout argument" (use the client's) from any
        # explicit value via a sentinel, so the two calls cannot be collapsed
        # into one with a computed keyword.
        try:
            if timeout_s is None:
                http_response = self._client.post(self.form_state.action_url, content=payload)
            else:
                http_response = self._client.post(
                    self.form_state.action_url, content=payload, timeout=timeout_s
                )
        except httpx.HTTPError as exc:
            raise DirectHttpError(f"POST for {config.source} failed: {exc}") from exc

        if http_response.status_code != 200:
            raise DirectHttpError(
                f"POST for {config.source} returned HTTP {http_response.status_code}"
            )

        response = parse_partial_response(http_response.text)
        if response.redirect_url:
            # JSF answers a dead session with a redirect to the login page
            # rather than an error element. Without this the body is never
            # updated and the chain would carry on against an expired view.
            raise ViewExpiredError(
                f"Server redirected {config.source} to {response.redirect_url}; "
                "the adopted session is no longer valid"
            )
        self._apply(response)
        return response

    def _apply(self, response: PartialResponse) -> None:
        """Fold a partial response into the session state.

        The tee sheet re-renders the whole form on every step (``u`` is the form
        id), so the browser's next request would serialize the *new* fields.
        Rebuild from the returned markup when it contains our form, then apply
        the ViewState, which JSF always sends as its own update.
        """
        form_markup = self.form_state.form_id and response.updates.get(self.form_state.form_id)
        if form_markup and "<form" in form_markup:
            document = parse_html(form_markup)
            refreshed = next(
                (f for f in document.find_all("form") if f.id == self.form_state.form_id), None
            )
            if refreshed is None:
                logger.warning(
                    "DIRECT_HTTP: Update for %s carried no matching form; keeping current fields",
                    self.form_state.form_id,
                )
            else:
                # Field refresh and action resolution are independent: a
                # re-rendered fragment often omits the action, and keeping stale
                # fields in that case would submit the pre-click player count and
                # guest state on the next step.
                fields = _serialize_form(refreshed)
                self.form_state.fields = fields
                action_url = next(
                    (value for name, value in fields if name == _ENCODED_URL_PARAM),
                    refreshed.attrs.get("action", ""),
                )
                if action_url:
                    # The action may be relative; resolve against the URL we are
                    # already posting to.
                    self.form_state.action_url = urllib.parse.urljoin(
                        self.form_state.action_url, action_url
                    )

        if response.view_state:
            self.form_state.set_field(_VIEW_STATE_PARAM, response.view_state)

    def close(self) -> None:
        """Close the underlying HTTP client and its connection pool."""
        self._client.close()

    def __enter__(self) -> "PrimeFacesSession":
        """Enter a context manager that closes the client on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client when leaving the context."""
        self.close()


def sleep_until(target_timestamp_ms: int) -> int:
    """Block until ``target_timestamp_ms``, returning the drift in ms.

    Coarse ``sleep`` down to the last few ms, then a spin - the same shape as
    the JS chain's wait, minus a DOM to worry about. Returns immediately (with
    a positive drift) when the target is already past.
    """
    coarse_s = (target_timestamp_ms - _SPIN_WINDOW_MS - time_module.time() * 1000) / 1000
    if coarse_s > 0:
        time_module.sleep(coarse_s)
    target_s = target_timestamp_ms / 1000
    while time_module.time() < target_s:
        pass
    return int(time_module.time() * 1000) - target_timestamp_ms
