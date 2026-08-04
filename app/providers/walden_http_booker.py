"""
The Walden booking chain, executed over direct HTTP instead of a browser.

This is the browser-free equivalent of ``_JS_ASYNC_BOOKING_CHAIN`` in
:mod:`app.providers.walden_provider`: Reserve -> player count -> TBD guests ->
Book Now. Each step is one PrimeFaces AJAX POST (see
:mod:`app.providers.walden_http` for the protocol), and each step's request is
derived from the markup the previous response returned - the same
``PrimeFaces.ab({...})`` handler the browser would have run on click.

The critical-path design goal is that at the target timestamp there is nothing
left to do but write bytes to an already-open socket:

* The Reserve request body is serialized during :meth:`DirectHttpBooker.prepare`,
  before the window opens.
* The TLS connection is established during ``prepare`` too.
* :meth:`DirectHttpBooker.book` waits out the remaining time and posts.

Failures raise :class:`~app.providers.walden_http.DirectHttpError`, which the
caller treats as "fall back to the Selenium chain". Nothing here is trusted
enough to be the only path to a booking.
"""

import logging
import time as time_module
from dataclasses import dataclass, field
from typing import Any

from app.providers.walden_dom_schema import DOM
from app.providers.walden_http import (
    AbConfig,
    DirectHttpError,
    Node,
    PartialResponse,
    PrimeFacesSession,
    find_ab_for_element,
    parse_html,
    sleep_until,
)

logger = logging.getLogger(__name__)

# Guest rows settle server-side within one request/response, so unlike the JS
# chain there is no fixed inter-click delay to pay here.
_MAX_PLAYERS = 4

# Class-name substrings marking a message/alert container, the tree-matching
# equivalent of the CSS selectors in DOM.ERROR_MESSAGES.containers. "error"
# covers ui-messages-error, ui-message-error, ui-growl-message-error and plain
# .error/.errors; the PrimeFaces widget classes catch a validation message
# rendered without an -error suffix.
_MESSAGE_CLASS_MARKERS = ("error", "alert", "ui-messages", "ui-growl")

# Enough to carry a validation sentence or two into an SMS/Discord reply without
# pasting a re-rendered tee sheet into it.
_MAX_MESSAGE_CHARS = 500

# Marks a chain result as this path's. The provider handles both this and the JS
# chain's results through the same branches, but only this one leaves the browser
# DOM untouched, so only this one needs the outcome resolved against the site.
DIRECT_HTTP_PATH = "direct-http"

# Chain phases. These are a cross-module contract: the provider decides whether
# a Selenium retry is safe by looking at which phase the chain stopped in, so
# the names live here and are imported there rather than duplicated.
#
# The split around the Reserve POST is the point of the whole set. Once the
# request is on the wire we cannot know whether the server acted on it, so a
# browser retry from that point risks racing our own reservation.
PHASE_INIT = "init"
PHASE_PRECISION_WAIT = "precision_wait"
PHASE_RESERVE_STAGED = "reserve_staged"  # request built, nothing sent yet
PHASE_RESERVE_SENT = "reserve_sent"  # written to the socket, outcome unknown
PHASE_PLAYER_COUNT = "player_count"
PHASE_TBD_GUESTS = "tbd_guests"
PHASE_BOOK_NOW = "book_now"
PHASE_COMPLETE = "complete"

# The only phases in which nothing can have reached the server, and therefore
# the only ones after which a browser retry is safe.
PRE_SUBMIT_PHASES = frozenset({PHASE_INIT, PHASE_PRECISION_WAIT, PHASE_RESERVE_STAGED})


@dataclass
class DirectBookingResult:
    """Outcome of a direct-HTTP booking attempt.

    Shaped to match the JS chain's result dict so the provider can log and
    branch on both identically, plus ``final_markup`` - the last response body,
    which is the only record of the booking outcome, since the browser DOM is
    untouched by this path.

    ``success`` here means every step of the chain ran without erroring - the
    four POSTs went out and each response yielded the element the next step
    needed. It is not evidence that a reservation exists: a completed chain
    whose tee time never appeared on the member's reservations page is exactly
    what prompted this field's documentation. The provider verifies separately.
    """

    success: bool = False
    blocked: bool = False
    phase: str = PHASE_INIT
    error: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    final_markup: str = ""
    # Visible validation/message text found in the final response, if any. The
    # direct path's counterpart to _extract_booking_error_message, which reads
    # the browser DOM this path never touches.
    response_message: str | None = None

    def as_chain_result(self) -> dict[str, Any]:
        """Render as the dict shape ``_run_booking_chain_js`` returns."""
        return {
            "success": self.success,
            "blocked": self.blocked,
            "phase": self.phase,
            "error": self.error,
            "timing": self.timing,
            "finalMarkup": self.final_markup,
            "responseMessage": self.response_message,
            "path": DIRECT_HTTP_PATH,
        }


class DirectHttpBooker:
    """Runs one booking over HTTP against an adopted browser session."""

    def __init__(self, session: PrimeFacesSession) -> None:
        """Bind the booker to an adopted, authenticated session."""
        self.session = session
        self._reserve_config: AbConfig | None = None
        self._reserve_body: bytes | None = None

    # -- staging ----------------------------------------------------------

    def prepare(self, reserve_button_id: str, page_html: str) -> None:
        """Resolve and pre-serialize the Reserve request, and warm the socket.

        Everything expensive happens here, before the booking window opens:
        parsing the tee sheet, resolving the button's AJAX config, urlencoding
        the body, DNS/TCP/TLS. What remains for the target instant is a socket
        write.

        Args:
            reserve_button_id: Component id of the slot's Reserve link, e.g.
                ``..._:teeTimeForm:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:reserve_button``.
            page_html: Current tee sheet source, used to resolve the handler.
        """
        document = parse_html(page_html)
        button = document.find_by_id(reserve_button_id)
        if button is None:
            raise DirectHttpError(f"Reserve button {reserve_button_id!r} not found in page")

        config = find_ab_for_element(button, page_html)
        if config is None:
            raise DirectHttpError(
                f"Reserve button {reserve_button_id!r} has no PrimeFaces.ab handler to replay"
            )

        self._reserve_config = config
        self._reserve_body = self.session.build_body(config)
        logger.info(
            "DIRECT_HTTP: Reserve request staged - source=%s, %d body bytes",
            config.source,
            len(self._reserve_body),
        )
        self.session.warm_up()

    # -- the race ---------------------------------------------------------

    def book(
        self,
        num_players: int,
        *,
        target_timestamp_ms: int | None = None,
    ) -> DirectBookingResult:
        """Run the full chain, firing Reserve at ``target_timestamp_ms``.

        Args:
            num_players: 1-4. Guests beyond the member are added as TBD.
            target_timestamp_ms: Epoch ms to send the Reserve POST at. None
                fires immediately (later bookings in a batch, window already
                open).

        Returns:
            The outcome; never raises for an ordinary booking failure.
        """
        result = DirectBookingResult()

        # Misuse, not a booking outcome - but reported as a PHASE_INIT result
        # rather than raised, because nothing has been sent and the caller's
        # correct response is to fall back, not to lose the booking.
        if self._reserve_config is None or self._reserve_body is None:
            result.error = "prepare() must be called before book()"
            logger.error("DIRECT_HTTP: %s", result.error)
            return result
        if not 1 <= num_players <= _MAX_PLAYERS:
            result.error = f"num_players must be 1-{_MAX_PLAYERS}, got {num_players}"
            logger.error("DIRECT_HTTP: %s", result.error)
            return result
        try:
            return self._run_chain(num_players, target_timestamp_ms, result)
        except DirectHttpError as exc:
            result.error = str(exc)
            # A step that could not find the element it needed is often a step
            # the site refused; the reason, if it gave one, is in the response
            # that step was reading.
            result.response_message = find_response_message(result.final_markup)
            logger.warning(
                "DIRECT_HTTP: Chain failed in phase %s: %s%s",
                result.phase,
                exc,
                f" (site message: {result.response_message})" if result.response_message else "",
            )
            return result
        except Exception as exc:  # noqa: BLE001 - this path must never abort a booking run
            # An unexpected error here is a bug in this module, not a booking
            # outcome. Report it as a failed attempt so the caller can fall
            # back rather than losing the run entirely.
            result.error = f"Unexpected {type(exc).__name__} in phase {result.phase}: {exc}"
            logger.exception("DIRECT_HTTP: Unexpected error in phase %s", result.phase)
            return result

    def _run_chain(
        self,
        num_players: int,
        target_timestamp_ms: int | None,
        result: DirectBookingResult,
    ) -> DirectBookingResult:
        """Drive the request sequence, advancing ``result.phase`` as it goes."""
        assert self._reserve_config is not None and self._reserve_body is not None

        result.phase = PHASE_RESERVE_STAGED

        if target_timestamp_ms is not None:
            result.phase = PHASE_PRECISION_WAIT
            result.timing["msUntilTarget"] = target_timestamp_ms - int(time_module.time() * 1000)
            result.timing["clickDriftMs"] = sleep_until(target_timestamp_ms)

        start = time_module.perf_counter()

        def elapsed_ms() -> int:
            """Milliseconds since the Reserve request went out."""
            return int((time_module.perf_counter() - start) * 1000)

        # Advance before the call, not after. Once the request is handed to the
        # socket we cannot tell "never sent" from "sent, response lost", and a
        # browser retry in the second case races our own reservation.
        result.phase = PHASE_RESERVE_SENT
        response = self.session.post(self._reserve_config, body=self._reserve_body)
        result.final_markup = response.markup
        result.timing["reserveMs"] = elapsed_ms()

        blocked_reason = _find_blocked_message(response)
        if blocked_reason is not None:
            result.blocked = True
            result.error = blocked_reason
            result.timing["blockedDetectedMs"] = elapsed_ms()
            return result

        result.phase = PHASE_PLAYER_COUNT
        response = self._select_player_count(response, num_players)
        result.final_markup = response.markup
        result.timing["playerCountMs"] = elapsed_ms()

        blocked_reason = _find_blocked_message(response)
        if blocked_reason is not None:
            result.blocked = True
            result.error = blocked_reason
            return result

        if num_players > 1:
            result.phase = PHASE_TBD_GUESTS
            response = self._add_tbd_guests(response, num_players)
            result.final_markup = response.markup
            result.timing["tbdGuestsMs"] = elapsed_ms()

            blocked_reason = _find_blocked_message(response)
            if blocked_reason is not None:
                result.blocked = True
                result.error = blocked_reason
                return result

        result.phase = PHASE_BOOK_NOW
        response = self._click_book_now(response)
        result.final_markup = response.markup
        result.timing["bookNowMs"] = elapsed_ms()

        # The submit step can be refused like any other, and until now this was
        # the one step whose response nobody examined: the chain went straight
        # to COMPLETE. A booking the club turned down at Book Now was therefore
        # indistinguishable from one it accepted, because neither says anything
        # the phrase check recognizes.
        blocked_reason = _find_blocked_message(response)
        if blocked_reason is not None:
            result.blocked = True
            result.error = blocked_reason
            return result

        # Whatever the site rendered in its message containers rides along, so a
        # refusal for a reason we have no pattern for still reaches the member
        # instead of being reported as an unexplained non-confirmation.
        result.response_message = find_response_message(response.markup)

        result.phase = PHASE_COMPLETE
        result.success = True
        result.timing["totalMs"] = elapsed_ms()
        return result

    # -- individual steps -------------------------------------------------

    def _select_player_count(self, response: PartialResponse, num_players: int) -> PartialResponse:
        """Set the player-count radio and replay its change behavior.

        The group is identified the same way the JS chain does it: a
        ``.ui-selectonebutton`` carrying a radio for the requested count but no
        ``value="0"`` option, which is what distinguishes it from the
        ALL/MORNING/AFTERNOON time filter (issue #105).
        """
        markup = response.markup
        document = parse_html(markup)

        radio = _find_player_count_radio(document, num_players)
        if radio is None:
            raise DirectHttpError(
                f"Player count selector for {num_players} players not found in response"
            )
        if DOM.PLAYER_COUNT.disabled_class in _parent_classes(radio):
            raise DirectHttpError(f"Player count {num_players} is disabled for this slot")

        name = radio.attrs.get("name")
        if not name:
            raise DirectHttpError("Player count radio has no name to submit")

        # Selecting a radio *is* setting the form field; the change behavior
        # then tells the server to re-render the player rows.
        self.session.form_state.set_field(name, radio.attrs.get("value", str(num_players)))

        config = find_ab_for_element(radio, markup) or _group_ab_config(radio, markup)
        if config is None:
            raise DirectHttpError("Player count control has no PrimeFaces.ab handler to replay")

        logger.info("DIRECT_HTTP: Selecting %d players via %s", num_players, config.source)
        return self.session.post(config)

    def _add_tbd_guests(self, response: PartialResponse, num_players: int) -> PartialResponse:
        """Click the TBD link on each guest row (row 0 is the member)."""
        for guest_index in range(1, num_players):
            document = parse_html(response.markup)
            rows = _find_player_rows(document)
            if len(rows) <= guest_index:
                raise DirectHttpError(
                    f"Only {len(rows)} player row(s) rendered; need {num_players}"
                )

            tbd = _find_tbd_link(rows[guest_index])
            if tbd is None:
                raise DirectHttpError(f"TBD button not found on guest row {guest_index}")

            config = find_ab_for_element(tbd, response.markup)
            if config is None:
                raise DirectHttpError(
                    f"TBD button on guest row {guest_index} has no PrimeFaces.ab handler"
                )

            logger.info("DIRECT_HTTP: Adding TBD guest %d via %s", guest_index, config.source)
            response = self.session.post(config)
        return response

    def _click_book_now(self, response: PartialResponse) -> PartialResponse:
        """Submit the booking."""
        markup = response.markup
        document = parse_html(markup)

        book_now = _find_book_now(document)
        if book_now is None:
            raise DirectHttpError("Book Now button not found in response")

        config = find_ab_for_element(book_now, markup)
        if config is None:
            raise DirectHttpError("Book Now button has no PrimeFaces.ab handler to replay")

        logger.info("DIRECT_HTTP: Clicking Book Now via %s", config.source)
        return self.session.post(config)


# ---------------------------------------------------------------------------
# Element lookups - the HTTP-side counterparts of the chain's DOM queries
# ---------------------------------------------------------------------------


def _parent_classes(node: Node) -> frozenset[str]:
    """CSS classes of the node's parent, or an empty set at the root."""
    return node.parent.classes if node.parent is not None else frozenset()


def _find_player_count_radio(document: Node, num_players: int) -> Node | None:
    """Find the player-count radio, skipping the time-filter button group."""
    for group in document.find_with_class("ui-selectonebutton"):
        radios = [n for n in group.find_all("input") if n.attrs.get("type") == "radio"]
        if any(r.attrs.get("value") == "0" for r in radios):
            continue  # time period filter (ALL/MORNING/AFTERNOON/AVAILABLE)
        for radio in radios:
            if radio.attrs.get("value") == str(num_players):
                return radio
    return None


def _group_ab_config(radio: Node, markup: str) -> AbConfig | None:
    """Fall back to the enclosing widget's handler for a radio with none.

    PrimeFaces binds a ``selectOneButton``'s ``change`` behavior to the widget
    container, not to the individual radio inputs.
    """
    node: Node | None = radio.parent
    while node is not None:
        if node.id:
            config = find_ab_for_element(node, markup)
            if config is not None:
                return config
        node = node.parent
    return None


def _find_player_rows(document: Node) -> list[Node]:
    """Find the rendered player rows, mirroring the chain's row selectors."""
    rows = [
        n
        for n in document.find_all("tr")
        if "data-ri" in n.attrs and _has_ancestor_id_containing(n, "playersTable")
    ]
    if rows:
        return rows
    # Require data cells: a <thead> row counted as row 0 would be mistaken for
    # the member row and shift every guest index by one, clicking TBD on the
    # wrong rows with no error to show for it.
    return [
        n
        for n in document.find_all("tr")
        if _has_ancestor_id_containing(n, "player") and n.find_all("td")
    ]


def _has_ancestor_id_containing(node: Node, fragment: str) -> bool:
    """Report whether any ancestor's id contains the fragment, case-insensitively."""
    current: Node | None = node.parent
    while current is not None:
        if fragment.lower() in current.id.lower():
            return True
        current = current.parent
    return False


def _find_tbd_link(row: Node) -> Node | None:
    """Find a row's TBD link by id, then by link text."""
    for node in row.descendants():
        if node.tag in ("a", "span", "button") and "tbd" in node.id.lower():
            return node
    for node in row.find_all("a"):
        if "TBD" in node.text_content().upper():
            return node
    return None


def _find_book_now(document: Node) -> Node | None:
    """Find the Book Now action by id, then by exact link text.

    Exact text only: substring matching on 'Book' would hit navigation links
    like 'Book a Tee Time' - the same trap the JS chain guards against.
    """
    for node in document.find_all("a"):
        if "bookteetimeaction" in node.id.lower():
            return node
    for node in document.find_all("a"):
        if node.text_content().strip().lower() in ("book now", "book"):
            return node
    return None


def find_response_message(markup: str) -> str | None:
    """Extract visible validation/message text from a partial response.

    The direct-HTTP counterpart of ``_extract_booking_error_message``, which
    reads the same kind of containers off the browser DOM. This path has no DOM,
    so a refusal the phrase check has no pattern for used to reach the member as
    an unexplained "did not confirm the reservation" - true, but useless.

    This is diagnostic text only: nothing branches on it. That is deliberate.
    A full tee sheet re-render carries hidden message templates, so treating a
    match as a refusal would fail bookings that succeeded. Reporting the text
    alongside an outcome decided elsewhere costs nothing if it is noise.

    Returns:
        The collected message text, or None when the response carries none.
    """
    document = parse_html(markup)
    seen: set[str] = set()
    messages: list[str] = []

    for node in document.descendants():
        if not _is_message_container(node):
            continue
        # Hidden containers are templates PrimeFaces renders on every page, not
        # something the member was shown.
        if node.attrs.get("aria-hidden", "").lower() == "true":
            continue
        text = " ".join(node.text_content().split())
        if not text or text.lower() in seen:
            continue
        # A container nested inside one already collected would repeat its text.
        if any(text in collected for collected in messages):
            continue
        seen.add(text.lower())
        messages.append(text)

    if not messages:
        return None

    joined = "; ".join(messages)
    return joined[:_MAX_MESSAGE_CHARS] + "..." if len(joined) > _MAX_MESSAGE_CHARS else joined


def _is_message_container(node: Node) -> bool:
    """Report whether a node is one of the site's message/alert containers.

    Mirrors ``DOM.ERROR_MESSAGES.containers`` - which is a CSS selector list,
    and this tree matches by class substring rather than selectors.
    """
    if node.attrs.get("role", "").lower() == "alert":
        return True
    if node.attrs.get("aria-live", "").lower() in ("assertive", "polite"):
        return True
    return any(
        marker in css_class.lower()
        for css_class in node.classes
        for marker in _MESSAGE_CLASS_MARKERS
    )


def _find_blocked_message(response: PartialResponse) -> str | None:
    """Detect the 'slot blocked by another user' validation popup.

    Scoped to the popup element, and only when it is not explicitly hidden -
    the same test the Selenium path applies (``aria-hidden`` flips to false when
    the popup is shown). Matching the whole response instead would let a hidden
    popup template, or any markup quoting one of these phrases, abort the chain
    on a slot we actually hold.

    Returns the matched reason, or None when the response carries no blocked
    indication.
    """
    document = parse_html(response.markup)
    for node in document.descendants():
        if "teesheetvalidationerrorpopup" not in node.id.lower():
            continue
        if node.attrs.get("aria-hidden", "").lower() == "true":
            continue
        text = node.text_content().lower()
        for pattern in DOM.SLOT_BLOCKED.blocked_text_patterns:
            if pattern.lower() in text:
                return f"Slot blocked by another user ({pattern})"
    return None
