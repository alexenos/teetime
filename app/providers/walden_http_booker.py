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

import concurrent.futures
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
    DirectHttpConnectionError,
    DirectHttpError,
    DirectHttpTimeoutError,
    Node,
    PartialResponse,
    PrimeFacesSession,
    ViewExpiredError,
    find_ab_for_element,
    parse_html,
    parse_partial_response,
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
#
# 6000 was sized when a timeout ended the run outright, so it only ever had to
# bound a healthy ladder. Now that a stalled Reserve is survivable, one stall
# can spend 3s of it, and 6000 would leave a ladder that survived the stall with
# no room left to walk. The club has granted slots at ~1.24s and the member
# wants the tee time whenever it comes, so asking out to 10s costs nothing on a
# morning already being lost.
_RESERVE_DEADLINE_MS = 10000

# Budget for a single Reserve POST, well under the session default. The deadline
# above cannot cancel a request already handed to the socket, so without this a
# single stalled Reserve spends the entire race and the fallback list - the
# thing that exists to survive exactly this - is never walked.
#
# Sized against measured round trips, and the earlier 2.0s was sized against a
# stale set of them. "230-535ms" came from runs that predate the sweep; the two
# mornings of 2026-08-13/14 answered in 593ms and 647ms, and an uncontested
# ad-hoc booking on 08-14 - no race, quiet server, warm connection - took 828ms.
# 2.0s was therefore barely 2.4x a quiet day, and rung 6 blew through it on both
# mornings, ending each run two rungs short of the offset that had been granted
# on 08-08 and 08-12.
#
# Raised rather than lowered, and not raised further, because the timeout now
# trades against _RESERVE_DEADLINE_MS instead of ending the run: every second
# spent waiting on a stalled request is a second of ladder not walked. This is
# ~3.6x a quiet-day round trip, which leaves a stall recoverable inside the
# deadline below.
_RESERVE_TIMEOUT_S = 3.0

# How far past its instant a ladder rung is still worth firing at once.
#
# Rungs are reached only after the previous answer lands, so a rung spaced closer
# to its predecessor than one round trip is already gone by the time it is
# considered. Sized above the slowest Reserve round trip measured (828ms) so that
# our own latency never silently costs a rung, and far below the minutes by which
# a later booking in a batch overshoots its shared target - which is the case
# _next_future_rung exists to drop.
_RUNG_LATE_GRACE_MS = 2000

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
    # One entry per Reserve sent, in order, naming the tee time it asked for.
    # The record of how hard the morning was fought, and the only place a
    # fallback booking's reason comes from. Since the sweep, consecutive entries
    # repeat when a rung re-asks for the same slot - so its *length* is the
    # number of attempts, which is what the refusal count wants, but reading it
    # as a list of distinct tee times is a mistake. Use distinct_attempted_times
    # for display.
    attempted_times: list[time] = field(default_factory=list)

    def distinct_attempted_times(self) -> list[time]:
        """The tee times asked for, in order, without the sweep's repeats."""
        distinct: list[time] = []
        for slot in self.attempted_times:
            if not distinct or distinct[-1] != slot:
                distinct.append(slot)
        return distinct

    # One row per Reserve exchange: when it went, what the club's clock said,
    # which view came back, and the verdict. Previously only the final response
    # survived, and on both mornings that mattered the answer was in an earlier
    # one. This is the record the boundary gets measured from.
    attempt_log: list["ReserveObservation"] = field(default_factory=list)

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
            "distinctAttemptedTimes": self.distinct_attempted_times(),
            "attemptLog": [observation.as_row() for observation in self.attempt_log],
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
        # Offsets past the window to ask for the staged slot at, first to last.
        # The single 0 is the historical behaviour: one shot, on the instant.
        self._sweep_offsets_ms: tuple[int, ...] = (0,)
        # Fire the first two rungs concurrently rather than waiting out a round
        # trip between them. Off here so the historical one-shot default stays
        # exactly that; prepare() turns it on.
        self._pipeline_opening_pair: bool = False
        # The frame every reported offset is measured from - the club's stated
        # 06:30:00, which the aim may sit past. book() sets it.
        self._window_timestamp_ms: int | None = None

    # -- staging ----------------------------------------------------------

    def prepare(
        self,
        reserve_button_id: str,
        page_html: str,
        *,
        fallback_times: Sequence[time] = (),
        measure_skew: bool = False,
        refresh_at_window: bool = False,
        sweep_offsets_ms: Sequence[int] = (0,),
        pipeline_opening_pair: bool = False,
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
                bookings only - it costs ~5s of probing, and an immediate
                booking has no instant to hit.
            refresh_at_window: Re-render the tee sheet at the target instant and
                fire Reserve against that render. Off by default; see
                :meth:`_refresh_view` for why it is no longer the way in.
            sweep_offsets_ms: Milliseconds past the window to ask for this slot
                at, first to last, before any fallback is tried. Timed bookings
                only. ``(0,)`` is one shot on the instant, which is what every
                lost morning did. See :meth:`_reserve_until_accepted`.
            pipeline_opening_pair: Fire the first two offsets without waiting for
                the first one's answer, so the pair brackets the refusal boundary
                inside a single round trip. Needs at least two offsets and a
                timed booking; ignored otherwise.
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
        # Sorted and deduplicated: the ladder is walked in order and each rung
        # is slept to absolutely, so an unordered list would sleep backwards
        # (returning instantly) and quietly collapse the sweep into a burst.
        self._sweep_offsets_ms = tuple(sorted(dict.fromkeys(sweep_offsets_ms))) or (0,)
        # Needs a pair to pipeline. A single-rung ladder is the one-shot case,
        # where there is nothing to overlap and a worker thread would only add a
        # handoff to the one request that has to be fast.
        self._pipeline_opening_pair = pipeline_opening_pair and len(self._sweep_offsets_ms) >= 2
        logger.info(
            "DIRECT_HTTP: Reserve request staged - source=%s, slot=%s, %d body bytes, "
            "viewState=%s, sweep=%s%s, fallbacks=%s",
            config.source,
            self._slot_time.strftime("%I:%M %p") if self._slot_time else "unreadable",
            len(self._reserve_body),
            # Fingerprinted rather than logged: this is a live session token,
            # and all a post-mortem needs is whether the one that fired differs
            # from the one staged here.
            _view_state_fingerprint(self.session),
            "+".join(str(offset) for offset in self._sweep_offsets_ms) + "ms",
            " (first two pipelined)" if self._pipeline_opening_pair else "",
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
        window_timestamp_ms: int | None = None,
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
            window_timestamp_ms: The club's stated window, when the aim above has
                been moved off it. Every offset reported - the ledger's
                ``sentMsPastWindow`` and ``serverMsPastWindow``, and the timing
                summary - is measured from here, so moving the aim does not put
                a morning on a different scale from the ones before it. Defaults
                to the target, which is what it was before the two diverged.

        Returns:
            The outcome; never raises for an ordinary booking failure.
        """
        result = DirectBookingResult()
        # Held on the instance so the observation calls deep in the ladder do not
        # each need it threaded down. Set before any early return so a misuse
        # result carries a coherent frame too.
        self._window_timestamp_ms = (
            window_timestamp_ms if window_timestamp_ms is not None else target_timestamp_ms
        )

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
            # The first rung is the aim point, and it is no longer 0: the club
            # refuses while its own clock still reads 06:30:00, so arriving on
            # the instant asks a question that has never once been answered yes.
            # Slept to here rather than inside the ladder because this is the
            # wait that has to be precise - everything after it is paced by how
            # fast the club answers.
            opening_offset_ms = self._sweep_offsets_ms[0]
            result.timing["openingOffsetMs"] = opening_offset_ms
            # Led, so that the request *arrives* at the aimed-at offset. The
            # drift is still reported against the lead-adjusted instant, which is
            # the one the wait was actually aiming at.
            result.timing["clickDriftMs"] = sleep_until(
                target_timestamp_ms + opening_offset_ms - int(round(self._lead_ms))
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

        response = self._reserve_until_accepted(config, body, target_timestamp_ms, result)
        result.timing["reserveMs"] = elapsed_ms()
        if response is None:
            result.timing["blockedDetectedMs"] = elapsed_ms()
            return result

        # The popup the club granted the slot alongside. It was already in the
        # response that gave us the tee time, so it cannot be about anything the
        # steps below do - only a *different* message from here on is news. This
        # is the same staleness the countdown has, and without allowing for it
        # the chain would refuse itself one step after winning.
        stale_message = _find_blocked_message(response)
        if stale_message is not None:
            logger.info(
                "DIRECT_HTTP: The granting response also carried %r; treating it as stale "
                "for the rest of the chain",
                stale_message,
            )

        result.phase = PHASE_PLAYER_COUNT
        response = self._select_player_count(response, num_players)
        result.final_markup = response.markup
        result.timing["playerCountMs"] = elapsed_ms()

        blocked_reason = _find_new_blocked_message(response, stale_message)
        if blocked_reason is not None:
            result.blocked = True
            result.error = blocked_reason
            return result

        if num_players > 1:
            result.phase = PHASE_TBD_GUESTS
            response = self._add_tbd_guests(response, num_players)
            result.final_markup = response.markup
            result.timing["tbdGuestsMs"] = elapsed_ms()

            blocked_reason = _find_new_blocked_message(response, stale_message)
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
        blocked_reason = _find_new_blocked_message(response, stale_message)
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

    def _reserve_until_accepted(
        self,
        config: AbConfig,
        body: bytes,
        target_timestamp_ms: int | None,
        result: DirectBookingResult,
    ) -> PartialResponse | None:
        """Fire Reserve until the club grants a slot, or the budget runs out.

        Two things the club does shape this loop, and both were read wrong for
        five mornings.

        It refuses for about the first second past the window, and the refusal
        it sends is "This slot is blocked by another user" whatever the reason.
        On 2026-08-08 the *identical* request - same slot, same component id,
        same ViewState - was refused at 0ms, refused at 812ms and accepted at
        1291ms; on 08-12 the pattern repeated at 0/817/1239ms. No member's
        behaviour changes in 479ms, and on both mornings every slot on the sheet
        was still open an hour later. So an early refusal is not evidence that
        the slot is taken, and treating it as such walked us off the tee time we
        wanted onto a worse one for no reason.

        Hence the ladder: while rungs remain, ask for the *same* slot again a
        little further past the window. Only once the ladder is spent - by which
        point a refusal plausibly is about the slot - does the fallback list come
        into play. Each rung is also a measurement, recorded in
        ``result.attempt_log``, so a morning spent losing still narrows where the
        club's boundary actually sits.

        A Reserve that never answers is survivable but contagious. It used to end
        the run, which on 2026-08-13 and 08-14 stopped the ladder at +1000ms -
        two rungs short of the ~1.24s that had been granted on the two mornings
        before. So a timeout now continues the ladder, but only ever onto the
        *same* slot: the request may have reached the club, and a second tee time
        stacked on a hold we cannot see would collide with the one-round-per-day
        rule. Once anything has timed out the fallback list is closed for the
        rest of the run, because that risk does not expire with the attempt.

        Note the ladder cannot be walked faster than the club answers. Rungs
        closer together than one round trip - ~600-830ms in every run measured -
        are already in the past when the previous answer lands and are skipped,
        so the configured 150/300/500/750 rungs have never fired.

        Returns:
            The response granting a slot, or None with ``result`` describing the
            refusal that ended it.
        """
        started = time_module.perf_counter()
        slot_time = self._slot_time
        remaining = list(self._fallback_times)
        # Every *reported* offset is measured from the club's stated window; every
        # instant slept to is measured from the aim. Keeping the two apart is what
        # lets the aim move to 06:30:01 without the ledger changing scale.
        window_frame_ms = (
            self._window_timestamp_ms
            if self._window_timestamp_ms is not None
            else target_timestamp_ms
        )
        # Latches on the first timeout and never clears; see the docstring.
        timed_out = False
        # Untimed bookings have no window to sweep around; the first rung is
        # already spent by the precision wait in _run_chain.
        rungs = list(self._sweep_offsets_ms[1:]) if target_timestamp_ms is not None else []
        # The second rung leaves the ladder when it is pipelined: it is fired
        # alongside the first rather than after it, so the loop must not also
        # walk to it.
        paired_rung_ms: int | None = None
        if self._pipeline_opening_pair and target_timestamp_ms is not None and rungs:
            paired_rung_ms = rungs.pop(0)
        max_attempts = _RESERVE_MAX_ATTEMPTS + len(rungs)
        last_reason: str | None = None
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            result.timing["reserveAttempts"] = attempt
            if slot_time is not None:
                result.attempted_times.append(slot_time)

            if window_frame_ms is not None and attempt == 1:
                # First attempt only. Later ones are paced by the ladder, and
                # reporting each against the window would bury the number that
                # says whether we were on time.
                #
                # Measured from the club's stated window rather than from where
                # we aimed, so this stays the same number the mornings before
                # this one reported - "1030ms past the window", not "0ms past
                # the thing we decided to aim at".
                result.timing["reserveSentAtMs"] = int(time_module.time() * 1000) - window_frame_ms
            # Attempt 1 is in the window frame; later attempts are elapsed since
            # the chain started. Those are different clocks, and until
            # 2026-08-21 both were labelled "past the window" - which understated
            # that morning's attempt 3 as "2228ms past the window" when the
            # ledger's truth was +3243ms, compressing the whole tail of the race
            # by the first rung's offset for anyone reading logs alone. Say which
            # frame each number is in.
            logger.info(
                "DIRECT_HTTP: Firing Reserve %d/%d for %s - source=%s, viewState=%s, %s",
                attempt,
                max_attempts,
                slot_time.strftime("%I:%M %p") if slot_time else "an unreadable slot",
                config.source,
                _view_state_fingerprint(self.session),
                f"{result.timing['reserveSentAtMs']}ms past the window"
                if attempt == 1 and "reserveSentAtMs" in result.timing
                else "untimed"
                if attempt == 1
                else f"{int((time_module.perf_counter() - started) * 1000)}ms into the chain",
            )

            # Advance before the call, not after. Once the request is handed to
            # the socket we cannot tell "never sent" from "sent, response lost",
            # and a browser retry in the second case races our own reservation.
            result.phase = PHASE_RESERVE_SENT
            view_state = _view_state_fingerprint(self.session)
            # Read here rather than reused from `reserveSentAtMs` above, and kept
            # for every attempt rather than the first: an answered request takes
            # this from the response's own send timestamp, so this value is only
            # ever consumed when there is no response to take it from. The log
            # line and fingerprint between the two reads are real work, and the
            # closer read is the honest one for the ledger.
            sent_ms_past_window = (
                int(time_module.time() * 1000) - window_frame_ms
                if window_frame_ms is not None
                else None
            )
            if paired_rung_ms is not None and attempt == 1:
                assert target_timestamp_ms is not None  # paired only when timed
                pair = self._fire_opening_pair(
                    config,
                    body,
                    target_timestamp_ms=target_timestamp_ms,
                    window_frame_ms=window_frame_ms,
                    second_rung_ms=paired_rung_ms,
                    slot_time=slot_time,
                    view_state=view_state,
                    first_sent_ms_past_window=sent_ms_past_window,
                )
                # Both requests are one iteration to the loop but two questions
                # to the club, and the ledger is the record of which offsets were
                # asked. Counting them here keeps "attempts" honest.
                if pair.never_connected:
                    # Neither half reached the socket, so nothing was submitted.
                    # Same reasoning as the single-send path: rolling the phase
                    # back is what keeps the browser chain a safe retry, and a
                    # 6:30 that cannot open a socket can still be won by it.
                    result.phase = PHASE_RESERVE_STAGED
                    raise DirectHttpConnectionError(
                        f"Neither opening Reserve for {config.source} connected: {pair.reason}"
                    )
                result.attempt_log.extend(pair.observations)
                for pair_observation in pair.observations:
                    _log_reserve_observation(pair_observation)
                if slot_time is not None and len(pair.observations) > 1:
                    result.attempted_times.append(slot_time)
                attempt += len(pair.observations) - 1
                result.timing["reserveAttempts"] = attempt
                timed_out = timed_out or pair.timed_out

                if pair.accepted is not None:
                    result.final_markup = pair.accepted.markup
                    self.session.adopt(pair.accepted)
                    result.booked_slot_time = slot_time
                    logger.info(
                        "DIRECT_HTTP: Reserve accepted for %s from the opening pair at +%dms "
                        "after %dms (%s)",
                        slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                        pair.accepted_rung_ms or 0,
                        int((time_module.perf_counter() - started) * 1000),
                        pair.accepted_reason or "",
                    )
                    return pair.accepted

                if pair.carried is None:
                    # Neither half answered. Same situation as a serial stall,
                    # and handled the same way: the ladder's schedule is moot but
                    # its length is still the budget for asking again.
                    last_reason = pair.reason
                    spent_ms = (time_module.perf_counter() - started) * 1000
                    if spent_ms >= _RESERVE_DEADLINE_MS or not rungs:
                        logger.warning(
                            "DIRECT_HTTP: Neither opening Reserve answered after %dms; stopping "
                            "rather than asking for a different tee time",
                            int(spent_ms),
                        )
                        break
                    stall_rung_ms = rungs.pop(0)
                    logger.info(
                        "DIRECT_HTTP: Neither opening Reserve answered after %dms; asking again "
                        "for %s at +%dms (same slot only - the club may already be holding it)",
                        int(spent_ms),
                        slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                        stall_rung_ms,
                    )
                    sleep_until(target_timestamp_ms + stall_rung_ms - int(round(self._lead_ms)))
                    continue

                # Refused, both times. Carry the later response forward: it is
                # the freshest view of the sheet, which is what any fallback has
                # to be relocated in.
                self.session.adopt(pair.carried)
                result.final_markup = pair.carried.markup
                response = pair.carried
                document = pair.carried_document
                observation = pair.carried_observation
                assert document is not None and observation is not None
            else:
                # Bound to their own names and only widened into the loop's
                # variables past the stall check below. Assigning straight into
                # `response` would give it an optional type on this branch and a
                # non-optional one on the paired branch above, which is a real
                # ambiguity rather than a typing nuisance: the two branches have
                # to hand the shared tail the same thing.
                sent, sent_document, sent_observation, stalled = self._fire_single_reserve(
                    config,
                    body,
                    window_frame_ms=window_frame_ms,
                    slot_time=slot_time,
                    view_state=view_state,
                    sent_ms_past_window=sent_ms_past_window,
                    attempt=attempt,
                    result=result,
                )
                if stalled is not None:
                    timed_out = True
                    last_reason = stalled
                    spent_ms = (time_module.perf_counter() - started) * 1000
                    if spent_ms >= _RESERVE_DEADLINE_MS or target_timestamp_ms is None:
                        logger.warning(
                            "DIRECT_HTTP: Reserve %d never answered and %dms is spent; stopping "
                            "rather than asking for a different tee time",
                            attempt,
                            int(spent_ms),
                        )
                        break
                    # Deliberately not _next_future_rung. A stall of seconds has
                    # already carried us past every offset the ladder was there
                    # to explore, so its *schedule* is moot - but its length is
                    # still the budget for how many more times to ask. Take the
                    # next rung whether or not its instant has passed;
                    # sleep_until no-ops on one already gone, which fires the
                    # retry at once. That is the right move anyway: the club
                    # granted at ~1.24s on 08-08 and 08-12, and a stall leaves us
                    # well past that.
                    if not rungs:
                        logger.warning(
                            "DIRECT_HTTP: Reserve %d never answered and the ladder is spent; "
                            "stopping rather than asking for a different tee time",
                            attempt,
                        )
                        break
                    stall_rung_ms = rungs.pop(0)
                    logger.info(
                        "DIRECT_HTTP: Reserve %d never answered after %dms; asking again for %s "
                        "at +%dms (same slot only - the club may already be holding it)",
                        attempt,
                        int(spent_ms),
                        slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                        stall_rung_ms,
                    )
                    sleep_until(target_timestamp_ms + stall_rung_ms - int(round(self._lead_ms)))
                    continue
                assert (
                    sent is not None and sent_document is not None and sent_observation is not None
                )
                response, document, observation = sent, sent_document, sent_observation
            if observation.verdict != RESERVE_REFUSED:
                result.booked_slot_time = slot_time
                if attempt > 1:
                    logger.info(
                        "DIRECT_HTTP: Reserve accepted for %s on attempt %d after %dms " "(%s)",
                        slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                        attempt,
                        int((time_module.perf_counter() - started) * 1000),
                        observation.reason,
                    )
                return response

            last_reason = observation.reason
            spent_ms = (time_module.perf_counter() - started) * 1000
            if spent_ms >= _RESERVE_DEADLINE_MS:
                logger.warning(
                    "DIRECT_HTTP: %dms spent on Reserve attempts; the race is over, stopping",
                    int(spent_ms),
                )
                break

            rung_ms = _next_future_rung(rungs, target_timestamp_ms)
            # The target is always set when a rung comes back - rungs are built
            # only for a timed booking - but the sleep below is to an absolute
            # instant, so it is stated rather than assumed.
            if rung_ms is not None and target_timestamp_ms is not None:
                # Same slot, a little later. Re-staged against the sheet the
                # club just returned rather than the one staged pre-window, and
                # re-serialized so the ViewState is the one it just handed back.
                offset_ms = rung_ms
                restaged = (
                    _relocate_reserve_in(document, response.markup, slot_time)
                    if slot_time is not None
                    else None
                )
                if restaged is not None:
                    config = restaged
                body = self.session.build_body(config)
                logger.info(
                    "DIRECT_HTTP: Refused %sms past the window; asking again for %s at +%dms",
                    observation.sent_ms_past_window
                    if observation.sent_ms_past_window is not None
                    else "?",
                    slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                    offset_ms,
                )
                sleep_until(target_timestamp_ms + offset_ms - int(round(self._lead_ms)))
                continue

            # The ladder is spent. A refusal this far past the window is the
            # kind the fallback list was built for - unless a Reserve went
            # unanswered earlier, in which case the club may be holding the slot
            # we just asked about and a different tee time would be a second
            # booking on the same day. Losing the morning beats that.
            if timed_out:
                logger.warning(
                    "DIRECT_HTTP: %s, but an earlier Reserve never answered - not trying "
                    "another tee time, as the club may be holding %s",
                    observation.reason,
                    slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                )
                break

            next_candidate = self._next_candidate(document, response.markup, remaining)
            if next_candidate is None:
                logger.warning(
                    "DIRECT_HTTP: %s and no fallback tee time left to try", observation.reason
                )
                break
            config, slot_time = next_candidate
            body = self.session.build_body(config)
            logger.info(
                "DIRECT_HTTP: %s; falling back to %s",
                observation.reason,
                slot_time.strftime("%I:%M %p"),
            )

        # Only a run the club actually refused is "blocked". One that ended
        # because a Reserve never answered gets reported as a plain failure, the
        # same shape the raised timeout produced before it was survivable, and
        # for the same reason: `blocked` selects the "another member took it"
        # message for the member, and it feeds the untimed retry - which must
        # never re-fire at a slot the club may be silently holding.
        result.blocked = not timed_out
        result.error = last_reason or "Slot blocked by another user"
        logger.warning(
            "DIRECT_HTTP: No Reserve accepted after %d attempt(s) over %dms - tried %s%s",
            result.timing.get("reserveAttempts", 0),
            int((time_module.perf_counter() - started) * 1000),
            ", ".join(t.strftime("%I:%M %p") for t in result.distinct_attempted_times())
            or "nothing",
            "; at least one Reserve never answered, so the outcome is unknown" if timed_out else "",
        )
        return None

    def _fire_single_reserve(
        self,
        config: AbConfig,
        body: bytes,
        *,
        window_frame_ms: int | None,
        slot_time: time | None,
        view_state: str,
        sent_ms_past_window: int | None,
        attempt: int,
        result: DirectBookingResult,
    ) -> "tuple[PartialResponse | None, Node | None, ReserveObservation | None, str | None]":
        """Send one Reserve and observe the answer.

        ``window_frame_ms`` is the club's stated window, which every offset in
        the observation is measured from. Nothing here sleeps, so the aim does
        not reach this far.

        Extracted from :meth:`_reserve_until_accepted` when the opening pair grew
        a second way of sending, so both paths hand the loop the same three
        things and the loop keeps one copy of what to do with them.

        Returns:
            ``(response, document, observation, None)`` when the club answered,
            or ``(None, None, None, reason)`` when it did not. The stall reason
            is the caller's signal to walk the ladder rather than the sheet.
        """
        try:
            response = self.session.post(config, body=body, timeout_s=_RESERVE_TIMEOUT_S)
        except DirectHttpConnectionError:
            # The phase was advanced before the call because a request on the
            # socket cannot be told from one that was answered and lost. This is
            # the one case where it can: no connection was established, so
            # nothing was submitted. Rolling the phase back is what keeps the
            # browser chain available - past PRE_SUBMIT_PHASES the provider
            # treats a retry as racing our own booking and reports instead.
            result.phase = PHASE_RESERVE_STAGED
            raise
        except DirectHttpTimeoutError as exc:
            # Recorded like any other attempt. The ledger previously lost the
            # attempt that decided both mornings, so a run whose ladder was cut
            # short read as "1 attempt, every attempt refused".
            observation = ReserveObservation(
                attempt=attempt,
                slot_time=slot_time,
                source=config.source,
                view_state=view_state,
                verdict=RESERVE_TIMEDOUT,
                reason=str(exc),
                sent_ms_past_window=sent_ms_past_window,
                round_trip_ms=int(_RESERVE_TIMEOUT_S * 1000),
            )
            result.attempt_log.append(observation)
            _log_reserve_observation(observation)
            return None, None, None, str(exc)

        result.final_markup = response.markup
        # One parse, three questions: the verdict, the ledger row and the next
        # candidate's handler all come out of it. It is the largest single cost
        # in the loop - 36.9ms for a 670KB sheet on an idle container, and
        # 187-704ms in production on 2026-08-21, which is the gap the counters
        # below exist to explain.
        #
        # Measured as a pair rather than as elapsed time, because elapsed time
        # alone only restates the mystery. cpu/wall near 1.0 means the cycles
        # were genuinely burned - a slower or throttled vCPU, or more work than
        # benchmarked. Near 0.1 means this process was descheduled and something
        # else took the CPU. See docs/booking-post-mortem-2026-08-21.md.
        wall_start = time_module.perf_counter()
        cpu_start = time_module.process_time()
        container_cpu_start = _container_cpu_ms()

        document = parse_html(response.markup)
        observation = observe_reserve_response(
            attempt=attempt,
            slot_time=slot_time,
            source=config.source,
            view_state=view_state,
            response=response,
            document=document,
            markup=response.markup,
            target_timestamp_ms=window_frame_ms,
            defer_telemetry=True,
        )
        observation.post_response_wall_ms = int((time_module.perf_counter() - wall_start) * 1000)
        observation.post_response_cpu_ms = int((time_module.process_time() - cpu_start) * 1000)
        if container_cpu_start is not None:
            container_cpu_end = _container_cpu_ms()
            if container_cpu_end is not None:
                observation.container_cpu_ms = container_cpu_end - container_cpu_start
        result.attempt_log.append(observation)
        _log_reserve_observation(observation)
        return response, document, observation, None

    def _fire_opening_pair(
        self,
        config: AbConfig,
        body: bytes,
        *,
        target_timestamp_ms: int,
        window_frame_ms: int | None,
        second_rung_ms: int,
        slot_time: time | None,
        view_state: str,
        first_sent_ms_past_window: int | None,
    ) -> "_OpeningPair":
        """Ask for the same slot at the first two offsets without waiting between.

        Serially the ladder cannot ask twice inside one round trip. On 2026-08-15
        the first Reserve went at -60ms and its refusal landed at +940ms, by
        which point the +900 rung was gone and the next question could not go
        until +1240ms. That is fine when the first rung is 0 and expected to
        fail, but the first rung is now aimed at the club's second tick - and
        aiming there serially would push the follow-up to ~+1780ms, later than
        the +1240ms that has been granted three times. Overlapping them means a
        mis-measured tick costs one rung rather than the morning.

        Both requests are for the *same* slot, so neither can reserve a second
        tee time and collide with the one-round-per-day rule; the worst case is
        that the club grants the same hold twice and the later grant is dropped.

        Sent detached, because :meth:`PrimeFacesSession.post` folds the response
        into the session's form state and two in-flight requests doing that would
        race over the fields the rest of the chain is built from. The caller
        adopts exactly one.
        """
        lead_ms = int(round(self._lead_ms))
        frame_ms = window_frame_ms if window_frame_ms is not None else target_timestamp_ms

        def send(
            rung_ms: int, wait: bool
        ) -> tuple[int, int, PartialResponse | None, Exception | None]:
            # Slept to against the aim, reported against the club's stated
            # window. The two differ by the offset at which we believe the sheet
            # actually opens.
            if wait:
                sleep_until(target_timestamp_ms + rung_ms - lead_ms)
            sent_ms = int(time_module.time() * 1000) - frame_ms
            try:
                sent = self.session.send_detached(config, body=body, timeout_s=_RESERVE_TIMEOUT_S)
            except (DirectHttpError, ViewExpiredError) as exc:
                return rung_ms, sent_ms, None, exc
            return rung_ms, sent_ms, sent, None

        # The first request goes on this thread: it is the one the whole morning
        # is timed around, and handing it to a pool would put a scheduling hop in
        # front of the only send that has to be exact. The worker takes the later
        # rung, where a hundred microseconds does not matter.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="reserve-pair"
        ) as pool:
            deferred = pool.submit(send, second_rung_ms, True)
            first = send(self._sweep_offsets_ms[0], False)
            second = deferred.result()

        pair = _OpeningPair(observations=[])
        # Only counts when *nothing* was sent. A read or write timeout may have
        # landed at the club, and treating that as "never happened" is what would
        # let the run book a second tee time on top of a hold it cannot see.
        never_connected = True
        for index, (rung_ms, sent_ms, response, exc) in enumerate((first, second)):
            attempt_number = index + 1
            if exc is not None:
                if not isinstance(exc, DirectHttpConnectionError):
                    never_connected = False
                    pair.timed_out = True
                pair.reason = str(exc)
                pair.observations.append(
                    ReserveObservation(
                        attempt=attempt_number,
                        slot_time=slot_time,
                        source=config.source,
                        view_state=view_state,
                        verdict=RESERVE_TIMEDOUT,
                        reason=str(exc),
                        sent_ms_past_window=sent_ms,
                        round_trip_ms=int(_RESERVE_TIMEOUT_S * 1000),
                    )
                )
                continue

            never_connected = False
            assert response is not None
            document = parse_html(response.markup)
            observation = observe_reserve_response(
                attempt=attempt_number,
                slot_time=slot_time,
                source=config.source,
                view_state=view_state,
                response=response,
                document=document,
                markup=response.markup,
                target_timestamp_ms=frame_ms,
            )
            pair.observations.append(observation)
            if observation.verdict != RESERVE_REFUSED and pair.accepted is None:
                # First grant in rung order wins. A second grant for the same
                # slot is the same hold said twice, and adopting the later one
                # would only throw away the earlier view for nothing.
                pair.accepted = response
                pair.accepted_rung_ms = rung_ms
                pair.accepted_reason = observation.reason
            elif observation.verdict == RESERVE_REFUSED:
                pair.reason = observation.reason
                # Latest refusal wins as the one carried forward: it is the
                # freshest sheet, and a fallback has to be relocated in it.
                pair.carried = response
                pair.carried_document = document
                pair.carried_observation = observation

        pair.never_connected = never_connected
        # first_sent_ms_past_window is read for the log line only; the ledger
        # takes each request's own send time from the exchange above.
        logger.info(
            "DIRECT_HTTP: Opening pair fired at +%dms and +%dms (aimed from %sms)",
            first[0],
            second[0],
            first_sent_ms_past_window if first_sent_ms_past_window is not None else "?",
        )
        return pair

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


# ---------------------------------------------------------------------------
# Reading what the club did with a Reserve
# ---------------------------------------------------------------------------

RESERVE_ACCEPTED = "accepted"
RESERVE_REFUSED = "refused"
RESERVE_UNKNOWN = "unknown"
# No response at all, as against RESERVE_UNKNOWN's "a response we cannot read".
# The distinction is the whole point: an unreadable response says the club
# answered, a timeout says we do not know whether it acted. Only the latter
# closes the fallback list off for the rest of the run.
RESERVE_TIMEDOUT = "timeout"

# The populated header of the booking form the club returns once it has given us
# the slot: "Reservation at </label><label>04:53 PM". Populated is the point -
# the tee sheet carries no such element at all, so a match is proof the view
# advanced rather than re-rendered.
_RESERVATION_FORM_RE = re.compile(
    r"Reservation at</label>\s*<label[^>]*>\s*([^<]+?)\s*</label>", re.IGNORECASE
)
# Rows of the tee sheet itself. Present means the club handed the sheet back,
# which is what a refusal looks like and what an acceptance never does.
_SHEET_ROW_RE = re.compile(r"teeTimeSlots:(\d+):")
_RESERVE_BUTTON_RE = re.compile(r"reserve_button")
# The club's own "this sheet is not open for booking yet" marker, on the
# datascroller that holds the slot rows. Established 2026-08-21: diffing a
# refusal at +1015ms against one at +4009ms changed exactly two things in 670KB
# - the countdown div, and this class going away. Unlike the countdown beside it,
# which is frozen at whatever the pre-window staging saw, this one is live, and
# it is the only direct read of whether the club was open when it answered.
_SHEET_SCROLLER_RE = re.compile(r'id="[^"]*teeTimeSlots"[^>]*\sclass="([^"]*)"', re.IGNORECASE)
_SHEET_DISABLED_CLASS = "disable-div"


def _sheet_open_in(markup: str) -> bool | None:
    """Whether the club rendered its tee sheet as open, or None if it sent none.

    A substring test on markup already in memory - 0.02ms against a 670KB sheet,
    which is why this can sit on the race's critical path at all.
    """
    match = _SHEET_SCROLLER_RE.search(markup)
    if match is None:
        return None
    return _SHEET_DISABLED_CLASS not in match.group(1).lower()


def _container_cpu_ms() -> int | None:
    """CPU milliseconds burned by *every* process in this container, or None.

    Read alongside this process's own CPU time it answers the second half of the
    2026-08-21 question. If the container burned far more than we did over the
    same span, another process took the CPU - and Chrome is the standing
    suspect, since the driver stays open on the tee sheet page, with its own JS
    timers running, until well after the race is decided.

    Best-effort by design: /proc is Linux-only and this must never be the reason
    a booking fails, so every failure reads as "no measurement" rather than
    raising into the race.
    """
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            fields = handle.readline().split()
        if not fields or fields[0] != "cpu":
            return None
        # USER_HZ is 100 on every platform this runs on, so jiffies -> ms is x10.
        return sum(int(field) for field in fields[1:]) * 10
    except (OSError, ValueError):
        return None


def _reservation_form_slot(markup: str) -> str | None:
    """The tee time on the club's booking form, or None if it is not one.

    This is the acceptance signal. It cannot be confused with a template: the
    pre-window tee sheet contains no occurrence of the phrase, so the label only
    exists once the club has moved the view onto a slot it granted.
    """
    match = _RESERVATION_FORM_RE.search(markup)
    return match.group(1).strip() if match else None


def classify_reserve_response(document: Node, markup: str) -> tuple[str, str]:
    """Decide what the club did with a Reserve, from the shape of its answer.

    Ordered deliberately, because the two signals co-occur and the wrong order
    lost two mornings. The "This slot is blocked by another user" popup is
    emitted as inert markup in *every* Reserve response - all five saved from
    2026-08-06 through 08-12 carry it, including the two the club had accepted -
    and nothing in the re-rendered markup says whether it was shown. Read alone
    it is not a verdict, and reading it alone discarded a held tee time on both
    08-08 and 08-12.

    What does separate the five cleanly is which view came back:

    * accepted - a populated booking form, no tee sheet at all (79KB, 0 slot
      rows, 0 Reserve buttons, "Reservation at 04:53 PM")
    * refused - the whole tee sheet re-rendered, 79-87 rows and 148-276 Reserve
      buttons, and no booking form anywhere in it

    Returns:
        ``(verdict, reason)``; the reason is for logs and the ledger.
    """
    slot = _reservation_form_slot(markup)
    if slot is not None:
        return RESERVE_ACCEPTED, f"club returned its booking form for {slot}"

    blocked = _find_blocked_message_in(document)
    if blocked is not None:
        return RESERVE_REFUSED, blocked

    # Neither shape. Treated as progress rather than refusal, which is what this
    # path did before any of it was classified: the next step looks for the
    # element it needs and reports precisely what was missing if it is not there.
    return RESERVE_UNKNOWN, "no booking form and no refusal in the response"


@dataclass
class ReserveObservation:
    """One Reserve exchange, recorded whatever came back.

    Until now only the *last* attempt's response was kept, and every post-mortem
    that mattered turned on an earlier one. Two mornings were spent establishing
    facts about attempt 3 while attempts 1 and 2 were unrecoverable.
    """

    attempt: int
    slot_time: time | None
    source: str
    view_state: str
    verdict: str
    reason: str
    # Timing, all relative to the booking window rather than to each other.
    sent_ms_past_window: int | None = None
    round_trip_ms: int | None = None
    # The club's own clock when it answered, and where that sits relative to the
    # window. Whole seconds, which is coarse but decisive for the open question:
    # a boundary the club enforces looks different here from a clock we misread.
    server_date_ms: int | None = None
    server_ms_past_window: int | None = None
    # Structure, the basis of the verdict.
    response_bytes: int = 0
    sheet_rows: int = 0
    reserve_buttons: int = 0
    reservation_form_slot: str | None = None
    countdown_s: int | None = None
    popup_present: bool = False
    # Whether the club considered its own sheet open when it answered. The
    # decisive field of 2026-08-21: attempt 1 was refused at +1015ms with the
    # sheet still rendered closed, which is a boundary refusal rather than a
    # slot lost to another member, and establishing that took hand-diffing two
    # 670KB payloads. None means the response carried no sheet to read it from,
    # which is what an acceptance looks like.
    sheet_open: bool | None = None
    # Set while ``countdown_s`` and ``popup_present`` are still waiting on
    # backfill, so a ledger row written without it is recognisable as unread
    # rather than as a sheet with no countdown and no popup.
    telemetry_deferred: bool = False
    # Where the time between "body in hand" and "verdict known" actually goes.
    # Wall and CPU are a pair on purpose - see _fire_single_reserve.
    post_response_wall_ms: int | None = None
    post_response_cpu_ms: int | None = None
    container_cpu_ms: int | None = None
    # The parts of the payload that were dropped before they could be read. The
    # dialog is opened by script, so if anything distinguishes a shown popup
    # from a re-rendered one, it is in here.
    eval_text: str = ""
    callback_args: str | None = None
    # The unparsed payload. Held by reference to the string httpx already
    # materialized, so keeping it costs no copy on the critical path, and it is
    # the only way a future morning can be re-read for a signal nobody has
    # thought of yet. Excluded from as_row(); it is stored as its own object.
    raw_xml: str = ""

    def as_row(self) -> dict[str, Any]:
        """Flatten for the JSONL ledger."""
        return {
            "attempt": self.attempt,
            "slot": self.slot_time.strftime("%I:%M %p") if self.slot_time else None,
            "source": self.source,
            "viewState": self.view_state,
            "verdict": self.verdict,
            "reason": self.reason,
            "sentMsPastWindow": self.sent_ms_past_window,
            "roundTripMs": self.round_trip_ms,
            "serverDateMs": self.server_date_ms,
            "serverMsPastWindow": self.server_ms_past_window,
            "responseBytes": self.response_bytes,
            "sheetRows": self.sheet_rows,
            "reserveButtons": self.reserve_buttons,
            "reservationFormSlot": self.reservation_form_slot,
            "countdownS": self.countdown_s,
            "popupPresent": self.popup_present,
            "sheetOpen": self.sheet_open,
            "postResponseWallMs": self.post_response_wall_ms,
            "postResponseCpuMs": self.post_response_cpu_ms,
            "containerCpuMs": self.container_cpu_ms,
            "evalText": self.eval_text[:2000],
            "callbackArgs": self.callback_args,
        }


@dataclass
class _OpeningPair:
    """What the two overlapped opening Reserves came back with.

    ``accepted`` is the first grant in rung order; ``carried`` is the latest
    refusal, which is the response the chain continues against when neither was
    granted - it holds the freshest sheet for relocating a fallback in. Both are
    detached: exactly one must be adopted before the chain goes on.

    Defined here rather than beside :class:`DirectHttpBooker` because it names
    :class:`ReserveObservation`, which the module resolves at class-creation
    time.
    """

    observations: list[ReserveObservation]
    accepted: PartialResponse | None = None
    accepted_rung_ms: int | None = None
    accepted_reason: str | None = None
    carried: PartialResponse | None = None
    carried_document: Node | None = None
    carried_observation: ReserveObservation | None = None
    # A read or write timeout, which may have reached the club. Closes the
    # fallback list for the rest of the run.
    timed_out: bool = False
    # Every request failed before a byte left, so nothing was submitted and the
    # browser chain is still a safe retry.
    never_connected: bool = False
    reason: str | None = None


def observe_reserve_response(
    *,
    attempt: int,
    slot_time: time | None,
    source: str,
    view_state: str,
    response: PartialResponse,
    document: Node,
    markup: str,
    target_timestamp_ms: int | None,
    defer_telemetry: bool = False,
) -> ReserveObservation:
    """Build the ledger row for one Reserve exchange.

    ``defer_telemetry`` leaves the two fields that need their own walk of the
    parsed sheet - the countdown and the popup flag - unset, to be filled in by
    :func:`backfill_reserve_telemetry` once the race is over. Neither feeds a
    decision: they are read by post-mortems, off a ledger written after the last
    Reserve, so computing them between the club's answer and the next rung buys
    nothing and costs ~11ms of the ~54ms post-response path. The verdict, which
    *is* a decision, is never deferred.
    """
    verdict, reason = classify_reserve_response(document, markup)

    sent_past = received_past = None
    if target_timestamp_ms is not None:
        if response.sent_at_ms is not None:
            sent_past = response.sent_at_ms - target_timestamp_ms
        if response.server_date_ms is not None:
            received_past = response.server_date_ms - target_timestamp_ms

    round_trip = None
    if response.sent_at_ms is not None and response.received_at_ms is not None:
        round_trip = response.received_at_ms - response.sent_at_ms

    return ReserveObservation(
        attempt=attempt,
        slot_time=slot_time,
        source=source,
        view_state=view_state,
        verdict=verdict,
        reason=reason,
        sent_ms_past_window=sent_past,
        round_trip_ms=round_trip,
        server_date_ms=response.server_date_ms,
        server_ms_past_window=received_past,
        response_bytes=len(markup),
        sheet_rows=len(set(_SHEET_ROW_RE.findall(markup))),
        reserve_buttons=len(_RESERVE_BUTTON_RE.findall(markup)),
        reservation_form_slot=_reservation_form_slot(markup),
        countdown_s=None if defer_telemetry else _countdown_in(document),
        popup_present=False if defer_telemetry else _find_blocked_message_in(document) is not None,
        telemetry_deferred=defer_telemetry,
        sheet_open=_sheet_open_in(markup),
        eval_text=response.eval_text,
        callback_args=response.callback_args,
        raw_xml=response.raw_xml,
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


def _next_future_rung(rungs: list[int], target_timestamp_ms: int | None) -> int | None:
    """Pop the next ladder rung not yet meaningfully past, discarding stale ones.

    Every booking in a batch carries the same target timestamp - deliberately,
    so the batch's 6:30 gate does not depend on booking 1 reaching its wait - and
    the precision wait no-ops on a target already gone. For the second and later
    bookings that means the entire ladder is in the past, and every rung's sleep
    would return instantly. The sweep would then fire its whole ladder as fast as
    the socket allows: a burst of identical Reserves, which is the opposite of
    what spacing them out is for.

    Once the window is minutes old there is no boundary left to find, a refusal
    is much more likely to be a slot genuinely held, and the fallback list is the
    right next move.

    But "still ahead" was too strict once the ladder became retries rather than a
    search over offsets. A rung is reached only after the previous answer lands,
    so our own round trip - 593-828ms measured - eats whatever rung falls inside
    it. With the opening pair resolving around aim+1000ms, a 1000ms rung would
    fire on a fast morning and be silently dropped on a slow one, sending the run
    to the fallback list while the tee time we wanted had one ask left. The grace
    below is what separates "our round trip ate this rung" from "this booking is
    minutes past its window".

    Consumes ``rungs`` as it goes. Returns None when only stale ones are left.
    """
    if target_timestamp_ms is None:
        return None
    now_ms = int(time_module.time() * 1000)
    while rungs:
        rung = rungs.pop(0)
        if target_timestamp_ms + rung + _RUNG_LATE_GRACE_MS > now_ms:
            return rung
    return None


def _find_new_blocked_message(response: PartialResponse, stale: str | None) -> str | None:
    """A refusal in ``response``, unless the club was already saying it.

    The validation popup is view-scoped: once a message is set it re-renders
    into every later response for the same view, which is why a Reserve the club
    accepted still came back carrying "This slot is blocked by another user".
    A message that was already present before a step ran cannot be that step's
    answer, so only a changed one counts.
    """
    found = _find_blocked_message(response)
    if found is None or found == stale:
        return None
    return found


def backfill_reserve_telemetry(observations: list[ReserveObservation]) -> None:
    """Fill in the ledger fields that were skipped during the race.

    Call once the chain is done and before the ledger is written. Re-parses each
    deferred response from the markup the observation is still holding, which
    costs a parse per attempt at a point where nothing is racing a clock.

    Best-effort: a row that cannot be re-read keeps its unset values and stays
    flagged, because losing a telemetry field must never turn into losing a
    booking that already succeeded.
    """
    for observation in observations:
        if not observation.telemetry_deferred or not observation.raw_xml:
            continue
        try:
            markup = parse_partial_response(observation.raw_xml).markup
            document = parse_html(markup)
            observation.countdown_s = _countdown_in(document)
            observation.popup_present = _find_blocked_message_in(document) is not None
            observation.telemetry_deferred = False
        except Exception:  # noqa: BLE001 - telemetry must not break a booking
            logger.warning(
                "DIRECT_HTTP: could not backfill telemetry for Reserve %d; "
                "its countdown and popup fields stay unread",
                observation.attempt,
                exc_info=True,
            )


def _post_response_phrase(observation: ReserveObservation) -> str:
    """Wall and CPU for the post-response segment, with the ratio spelled out.

    The ratio is the whole point, so it is stated rather than left for a reader
    to divide: near 1.0 the cycles were genuinely burned, near 0 this process
    was descheduled and something else had the CPU.
    """
    wall = observation.post_response_wall_ms
    cpu = observation.post_response_cpu_ms
    if wall is None or cpu is None:
        return "unmeasured"
    phrase = f"wall {wall}ms / cpu {cpu}ms"
    if wall > 0:
        ratio = cpu / wall
        phrase += f" (cpu/wall {ratio:.2f} - {'burned' if ratio >= 0.6 else 'descheduled'})"
    if observation.container_cpu_ms is not None:
        phrase += f", container cpu {observation.container_cpu_ms}ms"
    return phrase


def _log_reserve_observation(observation: ReserveObservation) -> None:
    """Log one Reserve exchange as a single, greppable line.

    Everything a post-mortem has had to reconstruct from artifacts is on this
    line: when it was sent relative to the window, what the club's own clock
    said, which view came back, and the verdict that view produced.
    """
    logger.info(
        "DIRECT_HTTP: Reserve %d -> %s (%s) - sent %sms past the window, club clock %sms "
        "past, round trip %sms, %d bytes, %d sheet row(s), %d Reserve button(s), "
        "form=%s, countdown=%s, popup=%s, sheet=%s, post-response %s",
        observation.attempt,
        observation.verdict,
        observation.reason,
        observation.sent_ms_past_window if observation.sent_ms_past_window is not None else "n/a",
        observation.server_ms_past_window
        if observation.server_ms_past_window is not None
        else "unreadable",
        observation.round_trip_ms if observation.round_trip_ms is not None else "n/a",
        observation.response_bytes,
        observation.sheet_rows,
        observation.reserve_buttons,
        observation.reservation_form_slot or "none",
        observation.countdown_s if observation.countdown_s is not None else "none",
        observation.popup_present,
        "open"
        if observation.sheet_open
        else "closed"
        if observation.sheet_open is False
        else "none",
        _post_response_phrase(observation),
    )
    # The dialog is opened by script, not by markup, so whatever separates a
    # shown popup from a re-rendered one lives here. Logged in full the first
    # time it is ever captured, because no saved morning contains it.
    if observation.eval_text:
        logger.info(
            "DIRECT_HTTP: Reserve %d server script: %r",
            observation.attempt,
            observation.eval_text[:1000],
        )
    if observation.callback_args:
        logger.info(
            "DIRECT_HTTP: Reserve %d callback args: %r",
            observation.attempt,
            observation.callback_args[:500],
        )


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
    """The blocked-popup text, against a sheet already parsed.

    Split out so the retry loop can ask this and :func:`_countdown_in` of one
    parse: the sheet is ~500KB and the parse is the loop's largest single cost.

    Not a verdict on its own. The ``aria-hidden`` test below can never exclude
    anything on this path - the club serves the popup with ``ui-hidden-container``
    and no ``aria-hidden`` at all, and it is PrimeFaces' client-side script, which
    no browser here runs, that would set it. So this returns the message whenever
    the club rendered one, shown or not. Use :func:`classify_reserve_response`
    for a Reserve, and :func:`_find_new_blocked_message` for later steps.
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
