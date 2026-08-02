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


@dataclass
class DirectBookingResult:
    """Outcome of a direct-HTTP booking attempt.

    Shaped to match the JS chain's result dict so the provider can log and
    branch on both identically.
    """

    success: bool = False
    blocked: bool = False
    phase: str = "init"
    error: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)

    def as_chain_result(self) -> dict[str, Any]:
        """Render as the dict shape ``_run_booking_chain_js`` returns."""
        return {
            "success": self.success,
            "blocked": self.blocked,
            "phase": self.phase,
            "error": self.error,
            "timing": self.timing,
        }


class DirectHttpBooker:
    """Runs one booking over HTTP against an adopted browser session."""

    def __init__(self, session: PrimeFacesSession) -> None:
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
        if self._reserve_config is None or self._reserve_body is None:
            raise DirectHttpError("prepare() must be called before book()")
        if not 1 <= num_players <= _MAX_PLAYERS:
            raise DirectHttpError(f"num_players must be 1-{_MAX_PLAYERS}, got {num_players}")

        result = DirectBookingResult()
        try:
            return self._run_chain(num_players, target_timestamp_ms, result)
        except DirectHttpError as exc:
            result.error = str(exc)
            logger.warning("DIRECT_HTTP: Chain failed in phase %s: %s", result.phase, exc)
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
        assert self._reserve_config is not None and self._reserve_body is not None

        if target_timestamp_ms is not None:
            result.phase = "precision_wait"
            result.timing["msUntilTarget"] = target_timestamp_ms - int(time_module.time() * 1000)
            result.timing["clickDriftMs"] = sleep_until(target_timestamp_ms)

        start = time_module.perf_counter()

        def elapsed_ms() -> int:
            return int((time_module.perf_counter() - start) * 1000)

        result.phase = "reserve_click"
        response = self.session.post(self._reserve_config, body=self._reserve_body)
        result.timing["reserveMs"] = elapsed_ms()

        blocked_reason = _find_blocked_message(response)
        if blocked_reason is not None:
            result.blocked = True
            result.error = blocked_reason
            result.timing["blockedDetectedMs"] = elapsed_ms()
            return result

        result.phase = "player_count"
        response = self._select_player_count(response, num_players)
        result.timing["playerCountMs"] = elapsed_ms()

        blocked_reason = _find_blocked_message(response)
        if blocked_reason is not None:
            result.blocked = True
            result.error = blocked_reason
            return result

        if num_players > 1:
            result.phase = "tbd_guests"
            response = self._add_tbd_guests(response, num_players)
            result.timing["tbdGuestsMs"] = elapsed_ms()

        result.phase = "book_now"
        response = self._click_book_now(response)
        result.timing["bookNowMs"] = elapsed_ms()

        result.phase = "complete"
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
    return [n for n in document.find_all("tr") if _has_ancestor_id_containing(n, "player")]


def _has_ancestor_id_containing(node: Node, fragment: str) -> bool:
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


def _find_blocked_message(response: PartialResponse) -> str | None:
    """Detect the 'slot blocked by another user' validation popup.

    Returns the matched reason, or None when the response carries no blocked
    indication. Uses the same patterns as the Selenium path so both report the
    condition identically.
    """
    text = response.markup.lower()
    for pattern in DOM.SLOT_BLOCKED.blocked_text_patterns:
        if pattern.lower() in text:
            return f"Slot blocked by another user ({pattern})"
    return None
