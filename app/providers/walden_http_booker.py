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

Two things sit on top of that, both from mornings this path lost.

**It is timed to arrive, not to leave.** The club's clock runs ahead of ours and
the request still has to fly there, so sending at our own 06:30:00.000 puts the
Reserve on the club's desk something like half a second into a window members
have been clicking into since it opened. :meth:`DirectHttpBooker.prepare`
measures both quantities against the site and sends that much early. See
:meth:`DirectHttpBooker._stage_arrival_lead`.

**One Reserve is one guess.** A hold is not a booking: the club refuses a
Reserve on a slot another member is holding while the tee sheet goes on
rendering that slot as Available, so the refusal is the *only* evidence a slot
is gone. Firing once and reporting "blocked" therefore threw away a morning on
one contested tee time while eighty-odd others sat open. Reserve now walks a
ranked fallback list until one is accepted, and because a refusal comes back
with the whole re-rendered sheet, each step down that list costs a parse rather
than a round trip. See :meth:`DirectHttpBooker._reserve_with_fallbacks`.

The refusals are told apart before either applies:
:func:`_classify_reserve_response` separates a held slot from a window the club
has not opened yet, because the recovery for one is the opposite of the other.

Failures raise :class:`~app.providers.walden_http.DirectHttpError`, which the
caller treats as "fall back to the Selenium chain". Nothing here is trusted
enough to be the only path to a booking.
"""

import hashlib
import logging
import re
import time as time_module
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from app.providers.walden_dom_schema import DOM
from app.providers.walden_http import (
    AbConfig,
    DirectHttpError,
    Node,
    PartialResponse,
    PrimeFacesSession,
    ViewExpiredError,
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

# Id substrings marking one of the site's own popup wrappers. These carry no
# error class at all - a refused booking comes back as a `ui-dialog` headed
# "Restriction:" - so the class markers above never saw them, and a real refusal
# reached the member as an unexplained "did not confirm the reservation".
#
# Matching by id is safe because the site renders these wrappers empty until it
# has something to put in them: the same response that carried the restriction
# had `warningPopup` and `resourceNotAvailablePopup` beside it as empty spans.
# `useLastPlayPopup` is left out on purpose - it prompts, it does not refuse.
_MESSAGE_ID_MARKERS = ("restrictionpopup", "warningpopup", "resourcenotavailablepopup")

# Tags whose text is not message text: a dialog's own buttons ("Ok", "Close")
# and the PrimeFaces widget-init scripts rendered inside it.
_NON_MESSAGE_TAGS = frozenset({"a", "button", "script", "style"})

# Enough to carry a validation sentence or two into an SMS/Discord reply without
# pasting a re-rendered tee sheet into it.
_MAX_MESSAGE_CHARS = 500

# The day tab for the date already showing. Replaying its handler re-renders the
# whole form without changing what is selected, which is what makes it usable as
# a no-op refresh. Chosen over the ALL/MORNING/AFTERNOON filter because that one
# is a PrimeFaces widget whose behavior lives in an init script the browser
# executes and discards - by the time we read `driver.page_source` it is gone,
# while a day tab's handler is an inline `onclick` that survives.
_SELECTED_DATE_CLASS = "selected-date"

# Class on the label holding a slot's tee time ("08:00 AM") - the same element
# DOM.DISABLED_SLOT.time_label selects on the browser side.
_SLOT_TIME_LABEL_CLASS = "custom-time-label"

# How far to walk out from a Reserve button looking for its slot's time label.
# The label is a sibling subtree (the button sits in the slot's action cell, the
# label in its time cell), so the search has to go up and back down - four
# levels in the captured sheets. The cap is what keeps a button whose slot has
# no label from walking to the document root and scanning the whole tee sheet,
# once per button.
_SLOT_LABEL_MAX_DEPTH = 6

# "08:00 AM", tolerating the spacing and punctuation variants the site has used.
_SLOT_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", re.IGNORECASE)

# The club's own "Booking Starts In : 00:01:05" counter. A sheet carries it
# while the window is shut and drops it once the window opens - but only as of
# the moment that sheet was rendered, which is the whole distinction:
#
# * Against a *fresh* render it is authoritative, so :meth:`_refresh_view` does
#   gate on it - a refreshed sheet still counting down means the club has not
#   opened booking, and 2026-08-07's refreshed sheet confirmed the converse.
# * Against a *Reserve response* it is not, because that is a re-render of an
#   older view. There it is read for the log and never branched on. See
#   :func:`_log_countdown_observation`.
_COUNTDOWN_CLASS = "booking-starts-in"
_COUNTDOWN_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")

# Budget for a single refresh POST. Way under the chain's timeout: a refresh
# that has not answered in this long has already cost more than it can win back,
# and the remaining time is better spent on another attempt.
_REFRESH_TIMEOUT_S = 1.5

# How long past the target to keep trying before giving up on a fresh view.
# Generous next to a race decided in the first second, because every option
# after this point is a bad one - the ceiling exists to bound the damage when
# the site is down, not to pace a healthy run.
_REFRESH_DEADLINE_MS = 4000

# Attempts allowed inside that deadline, and the pause between them. The pause
# matters most for the window-still-shut case: hammering a server that has told
# us the time is not yet is pointless, and the counter ticks in seconds.
_REFRESH_MAX_ATTEMPTS = 4
_REFRESH_RETRY_PAUSE_S = 0.2

# Ceiling on how early the Reserve may be sent. The lead is measured, not
# assumed, and a measurement can be wrong without limit; this bounds what a bad
# one can cost. Comfortably above the ~500ms the club's clock and the flight
# time have actually come to, and well under the countdown's one-second tick, so
# an over-lead is still recoverable by asking again.
_MAX_ARRIVAL_LEAD_MS = 900.0

# Past this, the measurement is not believable and is discarded rather than
# clamped. Two internet-facing servers do not sit seconds apart; a reading that
# says they do is far likelier to be a parse fault than a real clock, and
# clamping one silently converts it into a plausible-looking 900ms lead that
# fires into a shut window every morning without ever looking wrong. Bounded
# ignorance beats a confident guess, so this sends unled and says why.
_MAX_PLAUSIBLE_OFFSET_MS = 5000.0

# Reserve attempts allowed across all candidate tee times. Each costs one round
# trip plus the parse of the sheet that comes back with it - call it 450ms - so
# this is about as far down the fallback list as the first two seconds of the
# race reach. Past that the morning is decided and more requests are noise.
_RESERVE_MAX_ATTEMPTS = 6

# Stop *starting* attempts once the race is this far gone. An attempt already in
# flight is allowed to finish - abandoning it would not un-send it.
_RESERVE_DEADLINE_MS = 6000

# Budget for a single Reserve POST, well under the session default. The deadline
# above cannot cancel a request already handed to the socket, so without this a
# single stalled Reserve spends the entire race and the fallback list - the
# thing that exists to survive exactly this - is never walked. Round trips have
# run 230-535ms; one that has not answered in this long has lost anyway.
#
# A timeout is deliberately left terminal rather than advancing to the next tee
# time. The request may well have reached the club, and reserving a second slot
# on top of a hold we cannot see would collide with the one-round-per-day rule -
# trading a lost morning for a booking mess.
_RESERVE_TIMEOUT_S = 2.0

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
PHASE_VIEW_REFRESH = "view_refresh"  # re-rendering the sheet the window opened on
PHASE_RESERVE_SENT = "reserve_sent"  # written to the socket, outcome unknown
PHASE_PLAYER_COUNT = "player_count"
PHASE_TBD_GUESTS = "tbd_guests"
PHASE_BOOK_NOW = "book_now"
PHASE_COMPLETE = "complete"

# The only phases in which nothing can have reached the server, and therefore
# the only ones after which a browser retry is safe.
#
# PHASE_VIEW_REFRESH belongs here even though it does send a request: that
# request re-renders a tee sheet and holds nothing, so a browser retry cannot
# race a reservation of ours. It does advance the JSF view the browser is also
# holding, so the retry may find its own ViewState a token behind - which costs
# a booking that was already lost, rather than risking a double one.
PRE_SUBMIT_PHASES = frozenset(
    {PHASE_INIT, PHASE_PRECISION_WAIT, PHASE_RESERVE_STAGED, PHASE_VIEW_REFRESH}
)


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
    # The tee sheet the refresh returned, kept solely so a failed booking can be
    # diagnosed. A blocked verdict raises exactly two questions about it - was
    # the club still counting down, and was the slot open - and neither can be
    # answered from the Reserve response alone. Empty when no refresh landed.
    refresh_markup: str = ""
    # The tee time the club actually accepted, which is not always the one asked
    # for: a blocked slot sends the chain down the fallback list. The caller
    # booked a slot by index long before this point and would otherwise report
    # and verify the wrong one.
    booked_slot_time: time | None = None
    # Every tee time a Reserve went out for, in order. The record of how hard the
    # morning was fought - one entry is a clean win, five is a sheet being taken
    # apart around us - and the only place a fallback booking's reason comes from.
    attempted_times: list[time] = field(default_factory=list)

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
            "refreshMarkup": self.refresh_markup,
            "bookedSlotTime": self.booked_slot_time,
            "attemptedTimes": list(self.attempted_times),
            "path": DIRECT_HTTP_PATH,
        }


class DirectHttpBooker:
    """Runs one booking over HTTP against an adopted browser session."""

    def __init__(self, session: PrimeFacesSession) -> None:
        """Bind the booker to an adopted, authenticated session."""
        self.session = session
        self._reserve_config: AbConfig | None = None
        self._reserve_body: bytes | None = None
        self._refresh_config: AbConfig | None = None
        self._slot_time: time | None = None
        self._fallback_times: tuple[time, ...] = ()
        self._lead_ms: float = 0.0

    # -- staging ----------------------------------------------------------

    def prepare(
        self,
        reserve_button_id: str,
        page_html: str,
        *,
        fallback_times: Sequence[time] = (),
        measure_skew: bool = False,
        refresh_at_window: bool = False,
    ) -> None:
        """Resolve and pre-serialize the Reserve request, and warm the socket.

        Everything expensive happens here, before the booking window opens:
        parsing the tee sheet, resolving the button's AJAX config, urlencoding
        the body, DNS/TCP/TLS, and measuring how early to send. What remains for
        the target instant is a socket write.

        Args:
            reserve_button_id: Component id of the slot's Reserve link, e.g.
                ``..._:teeTimeForm:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:reserve_button``.
            page_html: Current tee sheet source, used to resolve the handler.
            fallback_times: Tee times to fall back to, best first, when the club
                refuses the one above as held by another member. Ranked by the
                caller, which is the side that knows the fallback window and
                which times are spoken for by the rest of the batch.
            measure_skew: Probe the club's clock so the Reserve can be timed to
                *arrive* as the window opens rather than to leave then. Timed
                bookings only - it costs ~1.3s of probing, and an immediate
                booking has no instant to hit.
            refresh_at_window: Re-render the tee sheet at the target instant and
                fire Reserve against that render. Off by default; see
                :meth:`_refresh_view` for why it is no longer the way in.
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
        # Read now, while the button is in hand. The chain reports which tee
        # time it ended up holding, and after a fallback that is no longer the
        # one the caller picked by row index.
        self._slot_time = _slot_time_of(button)
        # Anything already held, and the slot itself, are not fallbacks.
        self._fallback_times = tuple(
            dict.fromkeys(t for t in fallback_times if t != self._slot_time)
        )
        logger.info(
            "DIRECT_HTTP: Reserve request staged - source=%s, slot=%s, %d body bytes, "
            "viewState=%s, fallbacks=%s",
            config.source,
            self._slot_time.strftime("%I:%M %p") if self._slot_time else "unreadable",
            len(self._reserve_body),
            # Fingerprinted rather than logged: this is a live session token,
            # and all a post-mortem needs is whether the one that fired differs
            # from the one staged here.
            _view_state_fingerprint(self.session),
            ", ".join(t.strftime("%I:%M %p") for t in self._fallback_times) or "none",
        )
        if refresh_at_window:
            self._stage_view_refresh(document, page_html, button)
        self.session.warm_up()
        if measure_skew:
            self._stage_arrival_lead()

    def _stage_arrival_lead(self) -> None:
        """Work out how far ahead of the target the Reserve should be sent.

        Two clocks and a network sit between deciding to reserve and the club
        acting on it, and both of them run against us: the club reaches 06:30:00
        before we do, and the request still has to fly there. Sending at our own
        06:30:00.000 therefore lands well inside a window other members have
        already been clicking into.

        A measurement that fails leaves the lead at zero - the behaviour before
        this existed. Guessing would be worse than being late: nothing in the
        response distinguishes a Reserve that arrived early from one that lost
        the slot, so an over-led request spends an attempt and reports the loss
        as a block. Being a little late is at least legible.
        """
        try:
            skew = self.session.measure_clock_skew()
        except Exception as exc:  # noqa: BLE001 - staging must never lose a booking
            logger.warning("DIRECT_HTTP: Clock skew probing failed (%s); sending unled", exc)
            return
        if skew is None:
            return

        if abs(skew.offset_ms) > _MAX_PLAUSIBLE_OFFSET_MS:
            logger.error(
                "DIRECT_HTTP: Measured clock offset of %+.0fms is not believable "
                "(over %.0fms); discarding it and sending unled",
                skew.offset_ms,
                _MAX_PLAUSIBLE_OFFSET_MS,
            )
            return

        # Clamped because the lead is derived from a measurement, and a wrong
        # one is unbounded in the direction that hurts. Half a second of skew is
        # already more than anything observed; past the ceiling the likelier
        # explanation is a bad probe than a club running that far ahead.
        self._lead_ms = max(0.0, min(skew.lead_ms, _MAX_ARRIVAL_LEAD_MS))
        logger.info(
            "DIRECT_HTTP: Reserve will be sent %.0fms early so it arrives as the window "
            "opens (offset %+.0fms, one-way %.0fms%s)",
            self._lead_ms,
            skew.offset_ms,
            skew.one_way_ms,
            f", clamped from {skew.lead_ms:.0f}ms" if skew.lead_ms > _MAX_ARRIVAL_LEAD_MS else "",
        )

    def _stage_view_refresh(self, document: Node, page_html: str, button: Node) -> None:
        """Resolve what to re-render the tee sheet with once the window opens.

        Best-effort: anything unresolvable leaves ``_refresh_config`` unset, and
        :meth:`book` falls back to firing the staged request on its own - which
        is what this path did before the refresh existed.
        """
        slot_time = _slot_time_of(button)
        if slot_time is None:
            logger.warning(
                "DIRECT_HTTP: Reserve button %s has no readable tee time beside it; "
                "skipping the view refresh",
                button.id,
            )
            return

        tabs = [n for n in document.descendants() if _SELECTED_DATE_CLASS in n.classes]
        if not tabs:
            logger.warning(
                "DIRECT_HTTP: No .%s day tab in the tee sheet; skipping the view refresh",
                _SELECTED_DATE_CLASS,
            )
            return

        # Every candidate, not just the first. The captured sheets each carry
        # exactly one, but the class is a styling hook - if the site ever marks
        # a wrapper with it too, taking the first and finding no handler would
        # abandon a refresh that the element beside it could have performed.
        config = next(
            (c for c in (find_ab_for_element(tab, page_html) for tab in tabs) if c is not None),
            None,
        )
        if config is None:
            logger.warning(
                "DIRECT_HTTP: None of the %d .%s day tab(s) has a PrimeFaces.ab handler "
                "to replay; skipping the view refresh",
                len(tabs),
                _SELECTED_DATE_CLASS,
            )
            return

        self._refresh_config = config
        self._slot_time = slot_time
        logger.info(
            "DIRECT_HTTP: View refresh staged - source=%s, slot=%s",
            config.source,
            slot_time.strftime("%I:%M %p"),
        )

    # -- the race ---------------------------------------------------------

    def book(
        self,
        num_players: int,
        *,
        target_timestamp_ms: int | None = None,
    ) -> DirectBookingResult:
        """Run the full chain, firing Reserve at ``target_timestamp_ms``.

        When ``prepare`` staged a view refresh, a timed booking spends one round
        trip re-rendering the tee sheet at the target instant and fires Reserve
        against that render. An untimed one skips it: the window is already open
        and the staged view was built against it.

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
        config, body = self._reserve_config, self._reserve_body

        if target_timestamp_ms is not None:
            result.phase = PHASE_PRECISION_WAIT
            result.timing["msUntilTarget"] = target_timestamp_ms - int(time_module.time() * 1000)
            result.timing["arrivalLeadMs"] = round(self._lead_ms)
            # Led, so that the request *arrives* as the window opens. The drift
            # is still reported against the lead-adjusted instant, which is the
            # one the wait was actually aiming at.
            result.timing["clickDriftMs"] = sleep_until(
                target_timestamp_ms - int(round(self._lead_ms))
            )

            # After the wait, not before: a sheet re-rendered while the window
            # is still shut is the very thing being refreshed away from.
            if self._refresh_config is not None:
                result.phase = PHASE_VIEW_REFRESH
                refreshed = self._refresh_view(config, body, result)
                if refreshed is None:
                    # Deliberately nothing sent. Staying in this phase is what
                    # tells the caller a browser retry is still safe to make.
                    return result
                config, body = refreshed

        start = time_module.perf_counter()

        def elapsed_ms() -> int:
            """Milliseconds since the first Reserve request went out."""
            return int((time_module.perf_counter() - start) * 1000)

        response = self._reserve_with_fallbacks(config, body, target_timestamp_ms, result)
        result.timing["reserveMs"] = elapsed_ms()
        if response is None:
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

    def _reserve_with_fallbacks(
        self,
        config: AbConfig,
        body: bytes,
        target_timestamp_ms: int | None,
        result: DirectBookingResult,
    ) -> PartialResponse | None:
        """Fire Reserve, and keep firing until one is accepted or the list runs out.

        The club answers a Reserve on a slot another member is holding with
        "This slot is blocked by another user" while still rendering that slot
        as Available - a hold is not a booking, and the sheet only shows the
        second. So the refusal is the only signal that a slot is gone, and one
        Reserve per morning means one guess at which slot was not.

        The response to a refusal carries the whole re-rendered tee sheet, every
        row's Reserve handler included, so walking to the next candidate costs a
        parse rather than a round trip.

        Returns:
            The response that accepted a Reserve, or None with ``result``
            describing the refusal that ended it.
        """
        started = time_module.perf_counter()
        slot_time = self._slot_time
        remaining = list(self._fallback_times)
        last_reason: str | None = None

        for attempt in range(1, _RESERVE_MAX_ATTEMPTS + 1):
            result.timing["reserveAttempts"] = attempt
            if slot_time is not None:
                result.attempted_times.append(slot_time)

            if target_timestamp_ms is not None and attempt == 1:
                # First attempt only. Later ones are paced by the club's
                # answers, not by the clock, and reporting each against the
                # window would bury the number that says whether we were on time.
                result.timing["reserveSentAtMs"] = (
                    int(time_module.time() * 1000) - target_timestamp_ms
                )
            logger.info(
                "DIRECT_HTTP: Firing Reserve %d/%d for %s - source=%s, viewState=%s, "
                "%sms past the window",
                attempt,
                _RESERVE_MAX_ATTEMPTS,
                slot_time.strftime("%I:%M %p") if slot_time else "an unreadable slot",
                config.source,
                _view_state_fingerprint(self.session),
                result.timing.get("reserveSentAtMs", "n/a - untimed")
                if attempt == 1
                else int((time_module.perf_counter() - started) * 1000),
            )

            # Advance before the call, not after. Once the request is handed to
            # the socket we cannot tell "never sent" from "sent, response lost",
            # and a browser retry in the second case races our own reservation.
            result.phase = PHASE_RESERVE_SENT
            response = self.session.post(config, body=body, timeout_s=_RESERVE_TIMEOUT_S)
            result.final_markup = response.markup

            # One parse, two questions. At ~190ms for a 500KB sheet this is the
            # largest single cost in the retry loop, and both the verdict and
            # the next candidate's handler come out of the same tree.
            document = parse_html(response.markup)
            _log_countdown_observation(document, attempt)
            reason = _find_blocked_message_in(document)
            if reason is None:
                result.booked_slot_time = slot_time
                if attempt > 1:
                    logger.info(
                        "DIRECT_HTTP: Reserve accepted for %s on attempt %d after %dms",
                        slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                        attempt,
                        int((time_module.perf_counter() - started) * 1000),
                    )
                return response

            last_reason = reason
            spent_ms = (time_module.perf_counter() - started) * 1000
            if spent_ms >= _RESERVE_DEADLINE_MS:
                logger.warning(
                    "DIRECT_HTTP: %dms spent on Reserve attempts; the race is over, stopping",
                    int(spent_ms),
                )
                break

            # Blocked: the slot is held. Nothing about asking again for it will
            # change that, so the only move left is a different tee time.
            next_candidate = self._next_candidate(document, response.markup, remaining)
            if next_candidate is None:
                logger.warning("DIRECT_HTTP: %s and no fallback tee time left to try", reason)
                break
            config, slot_time = next_candidate
            body = self.session.build_body(config)
            logger.info(
                "DIRECT_HTTP: %s; falling back to %s",
                reason,
                slot_time.strftime("%I:%M %p"),
            )

        result.blocked = True
        result.error = last_reason or "Slot blocked by another user"
        logger.warning(
            "DIRECT_HTTP: No Reserve accepted after %d attempt(s) over %dms - tried %s",
            result.timing.get("reserveAttempts", 0),
            int((time_module.perf_counter() - started) * 1000),
            ", ".join(t.strftime("%I:%M %p") for t in result.attempted_times) or "nothing",
        )
        return None

    def _next_candidate(
        self,
        document: Node,
        markup: str,
        remaining: list[time],
    ) -> tuple[AbConfig, time] | None:
        """Pop the best fallback tee time still reservable in ``document``.

        Consumes ``remaining`` as it goes, so a candidate the sheet no longer
        offers a Reserve for is dropped rather than retried against the next
        response - if it has no button now it is gone, not late.
        """
        while remaining:
            candidate = remaining.pop(0)
            config = _relocate_reserve_in(document, markup, candidate)
            if config is not None:
                return config, candidate
            logger.info(
                "DIRECT_HTTP: Fallback %s has no Reserve in the returned sheet; skipping it",
                candidate.strftime("%I:%M %p"),
            )
        return None

    def _refresh_view(
        self,
        staged_config: AbConfig,
        staged_body: bytes,
        result: DirectBookingResult,
    ) -> tuple[AbConfig, bytes] | None:
        """Re-render the tee sheet now the window is open, and restage Reserve.

        Off by default, and kept only as a way back. It was built on the reading
        that a Reserve staged before the window is refused for being stale; on
        2026-08-07 the refresh ran exactly as designed - fresh sheet, countdown
        gone, 86 of 87 rows offering a Reserve - and the club refused anyway,
        with the *same* ViewState and component id the staged request already
        carried. It cost 730ms of the race and changed no byte of the request.

        Replays the selected day tab, which re-renders the whole form without
        changing the date. What comes back is a view built after the window
        opened - one the club will act on - carrying a fresh ViewState and the
        slot rows as they now stand.

        Retries within :data:`_REFRESH_DEADLINE_MS`, because the two things that
        go wrong here are both worth a second try: a transport blip, and a sheet
        that still carries the club's countdown, which is the club saying the
        window is not open on *its* clock yet. Firing into either is how the
        mornings this exists to fix were lost.

        Returns:
            The Reserve request to fire as ``(config, body)``, or None when the
            chain should send nothing at all and let the caller fall back.
        """
        # Staging sets both or neither, so a configured refresh always knows
        # which tee time it is restaging.
        assert self._refresh_config is not None and self._slot_time is not None
        started = time_module.perf_counter()
        deadline = started + _REFRESH_DEADLINE_MS / 1000
        response: PartialResponse | None = None
        document: Node | None = None
        countdown_s: int | None = None
        last_error: str | None = None

        for attempt in range(1, _REFRESH_MAX_ATTEMPTS + 1):
            result.timing["viewRefreshAttempts"] = attempt
            try:
                candidate = self.session.post(self._refresh_config, timeout_s=_REFRESH_TIMEOUT_S)
            except ViewExpiredError as exc:
                # The session itself is gone, and the staged Reserve carries the
                # very ViewState just rejected. Sending it could only fail, and
                # would spend the one retry the caller still has.
                logger.error(
                    "DIRECT_HTTP: View refresh rejected the adopted session (%s); "
                    "sending no Reserve so the browser chain can still try",
                    exc,
                )
                result.timing["viewRefreshFailed"] = "session-expired"
                result.error = f"Adopted session expired at the window: {exc}"
                return None
            except DirectHttpError as exc:
                # Nothing was applied to the session, so the staged request is
                # still sendable - but it is also still stale, so try again
                # before settling for it.
                last_error = str(exc)
                logger.warning("DIRECT_HTTP: View refresh attempt %d failed (%s)", attempt, exc)
            else:
                # Parsed once and carried: the relocation below needs the same
                # tree, and this is a ~50ms parse of a 500KB sheet sitting on
                # the critical path, not a cheap lookup to repeat.
                document = parse_html(candidate.markup)
                countdown_s = _countdown_in(document)
                response = candidate
                result.refresh_markup = candidate.markup
                if countdown_s is None:
                    # Logged on the way through, not inferred later from the
                    # absence of the warning below: "the club confirmed booking
                    # was open" is the single most load-bearing fact in a
                    # post-mortem, and silence is not a record of it.
                    logger.info(
                        "DIRECT_HTTP: View refreshed on attempt %d after %dms - "
                        "no countdown in the sheet, so the club has booking open; "
                        "viewState=%s",
                        attempt,
                        int((time_module.perf_counter() - started) * 1000),
                        _view_state_fingerprint(self.session),
                    )
                    break
                # The refresh worked and the club still says "not yet". Reserving
                # now is the exact failure this method exists to avoid. Recorded
                # after the loop rather than here, so a run that counts down once
                # and then comes back clean does not leave the marker behind and
                # report a countdown that no longer applied.
                logger.warning(
                    "DIRECT_HTTP: Refreshed sheet still counting down %ds to the "
                    "window on attempt %d; the club has not opened booking yet",
                    countdown_s,
                    attempt,
                )

            if time_module.perf_counter() + _REFRESH_RETRY_PAUSE_S >= deadline:
                break
            time_module.sleep(_REFRESH_RETRY_PAUSE_S)

        result.timing["viewRefreshMs"] = int((time_module.perf_counter() - started) * 1000)

        if response is None:
            # Every attempt failed in transport. The staged request is untouched
            # and remains the only thing left to try: the browser chain the
            # caller would fall back to holds the same pre-window view, so
            # declining to send costs the booking outright rather than saving it.
            logger.warning(
                "DIRECT_HTTP: No view refresh landed in %dms (%s); firing the "
                "request staged before the window as a last resort",
                _REFRESH_DEADLINE_MS,
                last_error,
            )
            result.timing["viewRefreshFailed"] = "no-response"
            return staged_config, staged_body

        # Recorded only now, describing the sheet actually being reserved
        # against - the loop ran out with the club still saying the window was
        # shut. Marked as a failure too: a countdown alone reads like colour
        # next to a healthy refresh, when it is the one outcome that says the
        # Reserve about to go out is the same doomed one as yesterday's.
        if countdown_s is not None:
            result.timing["viewRefreshCountdownS"] = countdown_s
            result.timing["viewRefreshFailed"] = "still-counting-down"
            logger.error(
                "DIRECT_HTTP: Out of refresh attempts with the club still counting "
                "down %ds; reserving into a window it says is shut",
                countdown_s,
            )

        # Past here a refresh has been folded into the session, so the staged
        # body carries a superseded ViewState and has to be rebuilt either way.
        assert document is not None  # set with `response`, on the same branch
        config = _relocate_reserve_in(document, response.markup, self._slot_time)
        if config is None:
            logger.warning(
                "DIRECT_HTTP: No Reserve button for %s in the refreshed tee sheet; "
                "replaying the staged component id against the new view",
                self._slot_time.strftime("%I:%M %p"),
            )
            config = staged_config
        elif config.source != staged_config.source:
            # The row index is baked into the component id, so this is routine
            # rather than alarming - and it is exactly what firing the staged id
            # blind would have got wrong.
            logger.info(
                "DIRECT_HTTP: Slot moved in the refreshed tee sheet - %s -> %s",
                staged_config.source,
                config.source,
            )

        return config, self.session.build_body(config)

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


def _parse_slot_time(text: str) -> time | None:
    """Read the first ``HH:MM AM/PM`` in some text, or None if it holds none."""
    match = _SLOT_TIME_RE.search(text)
    if match is None:
        return None
    # 12 AM is hour 0 and 12 PM is hour 12, which "% 12 then add" gets right
    # where a plain "+ 12 if PM" does not.
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == "P":
        hour += 12
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _slot_time_of(button: Node) -> time | None:
    """The tee time of the slot a Reserve button sits in.

    Walks outward to the nearest ancestor holding a time label. The label is not
    an ancestor of the button but a sibling subtree - the sheet puts the time in
    one cell and the Reserve action in another - so the search has to go up and
    back down, and the nearest such ancestor is the slot's own block.
    """
    node = button.parent
    for _ in range(_SLOT_LABEL_MAX_DEPTH):
        if node is None:
            return None
        for label in node.find_with_class(_SLOT_TIME_LABEL_CLASS):
            parsed = _parse_slot_time(label.text_content())
            if parsed is not None:
                return parsed
        node = node.parent
    return None


def _view_state_fingerprint(session: PrimeFacesSession) -> str:
    """A short, stable stand-in for the session's current ViewState.

    The token itself is live session state and does not belong in a log. What a
    post-mortem actually needs is only whether the ViewState that fired Reserve
    is the one staged before the window or the one the refresh returned, and
    eight hex characters answer that.
    """
    view_state = session.form_state.view_state
    if not view_state:
        return "MISSING"
    return hashlib.sha256(view_state.encode("utf-8", errors="replace")).hexdigest()[:8]


def _window_countdown_s(markup: str) -> int | None:
    """Seconds the club says remain before booking opens, if it still says any.

    The tee sheet carries a ``booking-starts-in`` counter while the window is
    shut and drops the element once it opens, so this answers "is the window
    open on the *server's* clock" - the question our own clock got wrong.

    Returns:
        Seconds remaining, or None when the sheet no longer claims a wait.
    """
    return _countdown_in(parse_html(markup))


def _countdown_in(document: Node) -> int | None:
    """Read the countdown from an already-parsed sheet.

    Split from :func:`_window_countdown_s` so the refresh can parse the sheet
    once and use the tree for both this and the slot lookup.
    """
    for node in document.descendants():
        if _COUNTDOWN_CLASS not in node.classes:
            continue
        match = _COUNTDOWN_RE.search(node.text_content())
        if match is None:
            continue
        hours, minutes, seconds = (int(group) for group in match.groups())
        remaining = hours * 3600 + minutes * 60 + seconds
        if remaining > 0:
            return remaining
    return None


def _relocate_reserve(markup: str, slot_time: time) -> AbConfig | None:
    """Find the Reserve request for ``slot_time`` in a freshly rendered sheet.

    Matched by tee time, never by the component id resolved earlier: the slot's
    row index is part of that id, and a re-render is free to move it.

    Returns:
        The request a click on that slot's Reserve link would produce, or None
        when the refreshed sheet offers no such slot.
    """
    return _relocate_reserve_in(parse_html(markup), markup, slot_time)


def _relocate_reserve_in(document: Node, markup: str, slot_time: time) -> AbConfig | None:
    """Find the Reserve request for ``slot_time`` in an already-parsed sheet.

    Takes both the tree and its source because resolving a handler may have to
    fall back to scanning the document text for a widget-init script.
    """
    for node in document.descendants():
        if "reserve_button" not in node.id:
            continue
        if _slot_time_of(node) != slot_time:
            continue
        return find_ab_for_element(node, markup)
    return None


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
        # something the member was shown - and a container inside a hidden
        # dialog is just as unseen as one hidden itself.
        if _is_hidden(node) or _has_hidden_ancestor(node):
            continue
        text = _visible_message_text(node)
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


def container_message_text(markup: str) -> str:
    """Message text of one already-matched container's markup.

    The browser path selects its containers with CSS and then has to read them,
    and ``WebElement.text`` sweeps in whatever the container holds - a refusal
    dialog reaches the member as "... per Day Ok". Handing the element's
    ``outerHTML`` here keeps one definition of message text for both paths,
    rather than a second pruner that can drift from this one.

    Returns:
        The pruned text, or "" when the container holds none.
    """
    return _visible_message_text(parse_html(markup))


def _is_hidden(node: Node) -> bool:
    """Report whether a node is explicitly hidden from the member."""
    return node.attrs.get("aria-hidden", "").lower() == "true"


def _has_hidden_ancestor(node: Node) -> bool:
    """Report whether any ancestor hides this node."""
    current = node.parent
    while current is not None:
        if _is_hidden(current):
            return True
        current = current.parent
    return False


def _visible_message_text(node: Node) -> str:
    """Text of a node's subtree with hidden branches pruned.

    ``text_content()`` would sweep in an ``aria-hidden`` child template sitting
    inside a visible wrapper, which reports stale text and - because the nesting
    check drops a message already contained in a collected one - can hide the
    real message behind it. A popup's own controls and widget-init script are
    pruned for the same reason: quoting "Ok" or a ``PrimeFaces.cw`` call back to
    the member buries the sentence they need.
    """
    parts: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if _is_hidden(current) or current.tag.lower() in _NON_MESSAGE_TAGS:
            continue
        if current.text:
            parts.append(current.text)
        stack.extend(reversed(current.children))
    return " ".join(" ".join(parts).split())


def _is_message_container(node: Node) -> bool:
    """Report whether a node is one of the site's message/alert containers.

    Mirrors ``DOM.ERROR_MESSAGES.containers`` - which is a CSS selector list,
    and this tree matches by class or id substring rather than selectors.
    """
    if node.attrs.get("role", "").lower() == "alert":
        return True
    if node.attrs.get("aria-live", "").lower() in ("assertive", "polite"):
        return True
    node_id = node.id.lower()
    if any(marker in node_id for marker in _MESSAGE_ID_MARKERS):
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
    return _find_blocked_message_in(parse_html(response.markup))


def _log_countdown_observation(document: Node, attempt: int) -> None:
    """Note the club's countdown, if the response carried the sheet holding it.

    Recorded and never branched on. It was briefly used to decide that a Reserve
    had been sent early, and it cannot answer that question in either direction:

    * Present does not mean the window is shut. It tracks the age of the *view*
      being re-rendered. On 2026-08-08 the Reserve re-rendered the view built at
      06:28:57, which carried "00:01:04" while members were already booking; the
      re-fires that reading triggered cost 1.3s of the race.
    * Absent does not mean the window is open. The third Reserve that morning
      answered with 81KB of booking dialog and no tee sheet at all - no slots,
      no Reserve buttons, and so no countdown either. The verdict flipped on
      which fragment the server re-rendered, 50ms after the previous one.

    A fresh render does settle it - 2026-08-07's refreshed sheet, built at
    06:30:00.5, had 151 slots and no countdown - but the Reserve response is not
    a fresh render, so this stays a log line.
    """
    countdown_s = _countdown_in(document)
    if countdown_s is not None:
        logger.info(
            "DIRECT_HTTP: Response to Reserve attempt %d still carries a %ds countdown; "
            "that dates the view it re-rendered, not the state of the window",
            attempt,
            countdown_s,
        )


def _find_blocked_message_in(document: Node) -> str | None:
    """The blocked-popup check, against a sheet already parsed.

    Split out so the retry loop can ask this and :func:`_countdown_in` of one
    parse: the sheet is ~500KB and the parse is the loop's largest single cost.
    """
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
