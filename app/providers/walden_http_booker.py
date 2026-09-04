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
import threading
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
    DirectHttpStatusError,
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

# How much of a non-200 body to put in the log; the ledger row keeps more.
_ERROR_BODY_LOG_CHARS = 300

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
# no room left to walk. The member wants the tee time whenever it comes, so
# asking further costs nothing on a morning already being lost.
#
# 30000 since 2026-09-04, from 10000. That morning's race hit the 10s wall on
# attempt 14 having reached three of its seven fallbacks; the seventh, 09:08 AM,
# was still Available on the sheet photographed 44 seconds after the window.
# Every Friday on record has cleared the whole 07:53-09:00 block inside a
# minute, so the fallback walk's reach is what a Friday is decided by once the
# opening is lost, and a walk of ~750ms per ask needs the room.
_RESERVE_DEADLINE_MS = 30000

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

# The two ways of asking at the opening. See prepare() and, for why the burst
# exists, _fire_opening_burst.
OPENING_MODE_LADDER = "ladder"
OPENING_MODE_BURST = "burst"

# Ceiling on burst members, and so on threads. The default plan is twelve; the
# cap exists so a misconfigured offsets string cannot turn into a hundred
# sockets against a club that has only ever seen two in flight.
_BURST_MAX_MEMBERS = 16

# After the burst, how many more times the fallback list may be walked before
# the deadline ends the run. The burst asks the first few fallbacks inside the
# first seconds, when a Friday's gate may not yet be open; walking the list
# again afterwards is what asks them at the instants both prior Friday wins
# were granted (+4.9s, +5.3s). Bounded so a sheet being emptied around us is
# not asked about forever.
_BURST_WALK_CYCLES = 2

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
        # Keep re-asking the target while the club renders its sheet closed,
        # and re-ask it between fallbacks once the sheet is open. Off here for
        # the same reason as the pair above; prepare() wires the settings in.
        self._hold_until_open: bool = False
        self._hold_cap_ms: int = 8000
        self._target_interleave: bool = True
        # How the opening is asked. The ladder is the class default so that a
        # booker built without prepare() - every test that stages the private
        # fields by hand - behaves as it always has; settings pick the burst.
        self._opening_mode: str = OPENING_MODE_LADDER
        # The burst's send instants past the aim, and how many from the front
        # ask for the target alone before fallbacks are interleaved.
        self._burst_offsets_ms: tuple[int, ...] = (0,)
        self._burst_target_only: int = 4
        # Fallback Reserve requests staged pre-window for the burst, best first:
        # the tee time, its handler as resolved in the staged sheet, and the
        # serialized body. Built in prepare() so that at the window every member
        # of the burst is a socket write, the target and fallbacks alike.
        self._burst_fallback_requests: list[tuple[time, AbConfig, bytes]] = []
        # Raised the moment a Reserve is *granted*. From then on the chain
        # advances the club's view by its own actions, so the resident Chrome
        # page's timers have nothing left to do and the provider's quieting
        # thread may take them apart. Never raised on a refusal: on 2026-09-04
        # the page was quieted at +1.6s on a refusal and every later response
        # was the same frozen pre-window render, because those timers had been
        # what advanced the view our requests are evaluated against. And never
        # raised on a connection that never opened: that is the one failure the
        # browser chain still rescues, and it needs its page alive. Assigned by
        # the provider; None means nobody is listening.
        self.quiet_signal: threading.Event | None = None
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
        hold_until_open: bool = False,
        hold_cap_ms: int = 8000,
        target_interleave: bool = True,
        opening_mode: str = OPENING_MODE_LADDER,
        burst_offsets_ms: Sequence[int] = (0,),
        burst_target_only: int = 4,
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
            hold_until_open: While a refusal's own markup renders the tee sheet
                closed (the club's disable-div marker), re-ask the target slot
                immediately instead of spending rungs or fallbacks on it. The
                closed-sheet refusals of 2026-08-21 and 08-28 are the evidence:
                the sweep's rungs assume the sheet opens at 06:30:01, and both
                Fridays it did not. One path for timed and untimed alike - an
                ad-hoc booking's sheet is already open so the hold has nothing
                to do, and running the same loop there is what exercises the
                race's code off-race.
            hold_cap_ms: How long past the stated window to keep holding before
                conceding the sheet is not opening and walking the fallback
                list anyway. Measured from the stated window, the frame every
                ledger offset is reported in.
            target_interleave: Once the sheet is open, re-ask the target between
                fallback attempts rather than abandoning it on its first
                open-sheet refusal. Only meaningful under ``hold_until_open``.
            opening_mode: ``"burst"`` sends the opening as a pipelined burst
                (see :meth:`_fire_opening_burst`) and ignores the sweep, the
                pair and the hold; ``"ladder"`` is every option above exactly as
                it ran through 2026-09-04. Anything else is treated as the
                ladder and logged, so a typo in a setting cannot cost a morning.
            burst_offsets_ms: Milliseconds past the aim to send each burst
                member at. Timed bookings only; an untimed booking has no
                instant to burst around and sends once.
            burst_target_only: How many members from the front of the burst
                ask for the target alone; after these, members alternate
                fallback and target through ``fallback_times``.
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
        self._hold_until_open = hold_until_open
        self._hold_cap_ms = max(0, hold_cap_ms)
        self._target_interleave = target_interleave
        if opening_mode not in (OPENING_MODE_LADDER, OPENING_MODE_BURST):
            logger.warning("DIRECT_HTTP: Unknown opening mode %r; using the ladder", opening_mode)
            opening_mode = OPENING_MODE_LADDER
        self._opening_mode = opening_mode
        self._burst_offsets_ms = tuple(sorted(dict.fromkeys(burst_offsets_ms)))[
            :_BURST_MAX_MEMBERS
        ] or (0,)
        self._burst_target_only = max(1, burst_target_only)
        self._burst_fallback_requests = []
        if self._opening_mode == OPENING_MODE_BURST:
            # Every fallback the burst may ask is resolved and serialized now,
            # against the staged sheet, so that at the window a fallback member
            # costs exactly what a target member costs: a socket write. Only as
            # many as the plan can use - a burst of twelve with four
            # target-only members interleaves at most four fallbacks.
            wanted = max(0, len(self._burst_offsets_ms) - self._burst_target_only)
            for candidate in self._fallback_times:
                if len(self._burst_fallback_requests) >= wanted:
                    break
                relocated = _relocate_reserve_in(document, page_html, candidate)
                if relocated is None:
                    logger.info(
                        "DIRECT_HTTP: Fallback %s has no Reserve in the staged sheet; "
                        "the burst will not ask for it",
                        candidate.strftime("%I:%M %p"),
                    )
                    continue
                self._burst_fallback_requests.append(
                    (candidate, relocated, self.session.build_body(relocated))
                )
        logger.info(
            "DIRECT_HTTP: Reserve request staged - source=%s, slot=%s, %d body bytes, "
            "viewState=%s, opening=%s, fallbacks=%s",
            config.source,
            self._slot_time.strftime("%I:%M %p") if self._slot_time else "unreadable",
            len(self._reserve_body),
            # Fingerprinted rather than logged: this is a live session token,
            # and all a post-mortem needs is whether the one that fired differs
            # from the one staged here.
            _view_state_fingerprint(self.session),
            self._describe_opening(),
            ", ".join(t.strftime("%I:%M %p") for t in self._fallback_times) or "none",
        )
        if refresh_at_window:
            self._stage_view_refresh(document, page_html, button)
        self.session.warm_up()
        if measure_skew:
            self._stage_arrival_lead()

    def _describe_opening(self) -> str:
        """The opening plan as one phrase for the staging log line."""
        if self._opening_mode == OPENING_MODE_BURST:
            plan = self._burst_plan_slots()
            members = "+".join(
                f"{offset}{'T' if is_target else 'F'}" for offset, _, is_target in plan
            )
            return (
                f"burst of {len(plan)} at +{members}ms past the aim "
                f"({self._burst_target_only} target-only, then alternating "
                f"{len(self._burst_fallback_requests)} staged fallback(s))"
            )
        return (
            "ladder sweep="
            + "+".join(str(offset) for offset in self._sweep_offsets_ms)
            + "ms"
            + (" (first two pipelined)" if self._pipeline_opening_pair else "")
            + (
                f" (hold target until sheet open, cap +{self._hold_cap_ms}ms"
                f"{', interleave' if self._target_interleave else ''})"
                if self._hold_until_open
                else ""
            )
        )

    def _burst_plan_slots(self) -> list[tuple[int, int | None, bool]]:
        """Which slot each burst member asks for: ``(offset_ms, fallback_index, is_target)``.

        The first ``_burst_target_only`` members are the target. After them,
        members alternate fallback and target - F1, T, F2, T, ... - cycling
        through the staged fallbacks. With none staged every member is the
        target, which is the pipelined form of the sweep.
        """
        plan: list[tuple[int, int | None, bool]] = []
        fallback_count = len(self._burst_fallback_requests)
        next_fallback = 0
        for index, offset in enumerate(self._burst_offsets_ms):
            position = index - self._burst_target_only
            if position < 0 or fallback_count == 0 or position % 2 == 1:
                plan.append((offset, None, True))
                continue
            plan.append((offset, next_fallback % fallback_count, False))
            next_fallback += 1
        return plan

    @property
    def _opening_offset_ms(self) -> int:
        """The offset past the aim the precision wait sleeps to.

        The first burst member or the first sweep rung, whichever mode is on.
        Both default to 0 - the aim point itself.
        """
        if self._opening_mode == OPENING_MODE_BURST:
            return self._burst_offsets_ms[0]
        return self._sweep_offsets_ms[0]

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
            opening_offset_ms = self._opening_offset_ms
            result.timing["openingOffsetMs"] = opening_offset_ms
            result.timing["openingMode"] = self._opening_mode
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

        # A slot is held. From here every step is an action of ours the club
        # acts on, so the parked page's timers - which until now were what kept
        # the shared view moving - have nothing left to do, and the provider's
        # quieting thread may take them apart. ~1us; the work it wakes is off
        # the race thread entirely. See quiet_signal in __init__ for why this
        # is raised on a grant and never on a refusal.
        if self.quiet_signal is not None:
            self.quiet_signal.set()

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

        # A response that still renders the Book Now action alongside a player
        # row on its own placeholder value means the club silently declined to
        # advance rather than accepted - 2026-08-22 lost a granted slot exactly
        # this way, refused at Book Now over an unset Resource (cart/walk)
        # field, in a dialog this chain has no pattern for and never even
        # named to the member. There is no signal for which real value the
        # golfer wants, so this takes the club's own first listed option for
        # every field still unset and asks once more - a wrong cart choice is
        # fixable with a phone call, a lost tee time is not.
        if _book_now_still_pending(parse_html(response.markup)):
            retried = self._fill_placeholder_selects(response)
            if retried is not None:
                response = retried
                result.final_markup = response.markup
                result.phase = PHASE_BOOK_NOW
                response = self._click_book_now(response)
                result.final_markup = response.markup
                result.timing["bookNowRetryMs"] = elapsed_ms()

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

        Under ``hold_until_open`` (the race default since the 2026-08-28
        post-mortem) the rungs are superseded: a refusal whose own markup
        renders the sheet closed spends nothing and the same slot is asked for
        again immediately, capped at ``hold_cap_ms`` past the stated window; the
        first answer on an open sheet ends the hold, starts the fallback walk,
        and - under ``target_interleave`` - keeps re-asking the target between
        fallbacks. Both Friday races on record spent every target ask into a
        provably closed sheet and then walked away seconds before the club
        granted anything to anyone; this loop shape is the fix. The rung list
        survives as the retry budget for the stall (timeout) paths only.

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
        # The burst replaces the sweep, the pair and the hold at the opening
        # and leaves the fallback walk below to run after it. Timed bookings
        # only: an untimed one has no instant to burst around, and the ladder's
        # own untimed behaviour - one send, then the walk - is right for it.
        burst_mode = self._opening_mode == OPENING_MODE_BURST and target_timestamp_ms is not None
        if burst_mode:
            rungs = []
        # The second rung leaves the ladder when it is pipelined: it is fired
        # alongside the first rather than after it, so the loop must not also
        # walk to it.
        paired_rung_ms: int | None = None
        if (
            not burst_mode
            and self._pipeline_opening_pair
            and target_timestamp_ms is not None
            and rungs
        ):
            paired_rung_ms = rungs.pop(0)
        if burst_mode:
            # Every member of the burst is an attempt, and the walk after it
            # may cover the list a bounded number of times.
            max_attempts = len(self._burst_offsets_ms) + _BURST_WALK_CYCLES * (
                1 + len(self._fallback_times)
            )
        else:
            max_attempts = _RESERVE_MAX_ATTEMPTS + len(rungs)
        walk_cycles_left = _BURST_WALK_CYCLES - 1 if burst_mode else 0
        last_reason: str | None = None
        attempt = 0
        # The hold-until-open policy (see prepare()): while refusals arrive on a
        # closed sheet they spend nothing, and once the sheet is open the target
        # is re-asked between fallbacks. One execution path, timed or not: an
        # ad-hoc booking's sheet opened days ago, so the hold naturally no-ops
        # there - but running the same loop is what lets a Tuesday-afternoon
        # booking exercise the code the race will run on Friday.
        #
        # Off under the burst, and not only because the burst asks faster than
        # the hold could: the signal the hold reads - the sheet-open marker in
        # a refusal's own markup - turned out on 2026-09-04 to describe the
        # state of *our* view as of its last refresh, not the club. Fourteen
        # refusals read "closed" that morning against a sheet other members
        # were booking from.
        hold_active = self._hold_until_open and not burst_mode
        # Latches on the first answer that is not a closed sheet and never
        # clears: the hold is for a sheet that has not opened yet, not a defense
        # against one the club might re-close.
        sheet_seen_open = not hold_active
        # The slot the caller actually wants, kept apart from the loop's
        # slot_time which the fallback walk reassigns.
        target_slot_time = slot_time
        # The last closed answer's club-clock offset, so the open transition can
        # be logged as a bracket rather than a point.
        last_closed_ms: int | None = None

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
            if burst_mode and attempt == 1:
                assert target_timestamp_ms is not None  # burst only when timed
                burst = self._fire_opening_burst(
                    config,
                    body,
                    target_timestamp_ms=target_timestamp_ms,
                    window_frame_ms=window_frame_ms,
                    view_state=view_state,
                    result=result,
                )
                if burst.never_connected:
                    # No member reached the socket, so nothing was submitted
                    # and the browser chain is still a safe retry - the same
                    # contract as the single send and the pair.
                    result.phase = PHASE_RESERVE_STAGED
                    raise DirectHttpConnectionError(
                        f"No opening Reserve for {config.source} connected: {burst.reason}"
                    )
                result.attempt_log.extend(burst.observations)
                for burst_observation in burst.observations:
                    if burst_observation.slot_time is not None and burst_observation.attempt > 1:
                        result.attempted_times.append(burst_observation.slot_time)
                attempt += max(0, len(burst.observations) - 1)
                result.timing["reserveAttempts"] = attempt
                timed_out = timed_out or burst.timed_out
                _log_gate_summary(result.attempt_log, "after the opening burst")

                if burst.accepted is not None:
                    result.final_markup = burst.accepted.markup
                    self.session.adopt(burst.accepted)
                    result.booked_slot_time = burst.accepted_slot_time
                    logger.info(
                        "DIRECT_HTTP: Reserve accepted for %s from burst member #%s after %dms "
                        "(%s)%s",
                        burst.accepted_slot_time.strftime("%I:%M %p")
                        if burst.accepted_slot_time
                        else "the staged slot",
                        burst.accepted_member if burst.accepted_member is not None else "?",
                        int((time_module.perf_counter() - started) * 1000),
                        burst.accepted_reason or "",
                        "; a surplus hold was also granted and is left to expire"
                        if burst.surplus
                        else "",
                    )
                    return burst.accepted

                if burst.view_expired is not None:
                    # The session is dead; every later ask would fail the same
                    # way. Raised so the chain reports it as the ladder would.
                    raise burst.view_expired

                if timed_out:
                    # A member may have reached the club and be holding its
                    # slot. Same rule as the ladder: nothing else is asked for.
                    last_reason = burst.reason
                    logger.warning(
                        "DIRECT_HTTP: A burst member never answered and none was granted; "
                        "stopping rather than asking for another tee time"
                    )
                    break

                last_reason = burst.reason
                if burst.carried is None:
                    # Nothing came back readable - every member errored. Fall
                    # through to a serial ask of the staged target; if the club
                    # is throttling, that answer says so in one round trip.
                    spent_ms = (time_module.perf_counter() - started) * 1000
                    logger.warning(
                        "DIRECT_HTTP: No burst member returned a sheet (%s); asking once "
                        "more serially %dms in",
                        burst.reason,
                        int(spent_ms),
                    )
                    continue

                # Refused throughout. Carry the latest refusal forward as the
                # sheet to relocate fallbacks in, and let the walk below run.
                self.session.adopt(burst.carried)
                result.final_markup = burst.carried.markup
                response = burst.carried
                document = burst.carried_document
                observation = burst.carried_observation
                assert document is not None and observation is not None
            elif paired_rung_ms is not None and attempt == 1:
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

            if hold_active:
                sheet_closed = observation.sheet_open is False
                if not sheet_seen_open and not sheet_closed:
                    # First answer rendered on an open sheet. None counts as
                    # open: a response with no sheet to read has to fail toward
                    # progress rather than toward an unbounded hold.
                    sheet_seen_open = True
                    logger.info(
                        "DIRECT_HTTP: The club's sheet opened between +%sms and +%sms past "
                        "the window; refusals count from here",
                        last_closed_ms if last_closed_ms is not None else "?",
                        observation.server_ms_past_window
                        if observation.server_ms_past_window is not None
                        else observation.sent_ms_past_window,
                    )
                if not sheet_seen_open:
                    last_closed_ms = (
                        observation.server_ms_past_window
                        if observation.server_ms_past_window is not None
                        else observation.sent_ms_past_window
                    )
                    # The cap's clock: from the stated window when there is
                    # one, else from the first Reserve. An untimed booking
                    # should never see a closed sheet at all, but if one does,
                    # the cap has to run from *something* or the hold would
                    # hammer until the deadline.
                    now_ms_past_window = (
                        int(time_module.time() * 1000) - window_frame_ms
                        if window_frame_ms is not None
                        else int((time_module.perf_counter() - started) * 1000)
                    )
                    if now_ms_past_window < self._hold_cap_ms:
                        # A closed sheet cannot lose a slot to anyone, so this
                        # refusal says nothing about who holds it. Ask again,
                        # now, and spend nothing: not a rung, not a fallback,
                        # not the attempt budget. Pacing is the club's own
                        # answer rate - one ask per round trip.
                        restaged = (
                            _relocate_reserve_in(document, response.markup, slot_time)
                            if slot_time is not None
                            else None
                        )
                        if restaged is not None:
                            config = restaged
                        body = self.session.build_body(config)
                        max_attempts += 1
                        logger.info(
                            "DIRECT_HTTP: Sheet still closed %dms past the window; asking "
                            "again for %s immediately (holding until open, cap +%dms)",
                            now_ms_past_window,
                            slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                            self._hold_cap_ms,
                        )
                        continue
                    sheet_seen_open = True
                    logger.warning(
                        "DIRECT_HTTP: Sheet still closed %dms past the window with the hold "
                        "cap spent; walking the fallback list anyway",
                        now_ms_past_window,
                    )

                # From here the sheet is open (or unreadable, or the cap is
                # spent) and a refusal is real. Same timeout rule as the legacy
                # ladder: once anything has gone unanswered, no other tee time.
                if timed_out:
                    logger.warning(
                        "DIRECT_HTTP: %s, but an earlier Reserve never answered - not trying "
                        "another tee time, as the club may be holding %s",
                        observation.reason,
                        slot_time.strftime("%I:%M %p") if slot_time else "the staged slot",
                    )
                    break

                if (
                    self._target_interleave
                    and target_slot_time is not None
                    and slot_time != target_slot_time
                ):
                    # The refusal just spent was a fallback's. Re-ask the target
                    # before the next one: on both Friday races on record the
                    # first grant to anyone came seconds after the sheet opened,
                    # so an open-sheet refusal of the target is not yet proof it
                    # is taken - and the member who got it both weeks was simply
                    # still asking at that point. One round trip of fallback
                    # delay, and it never consumes the attempt budget.
                    relocated = _relocate_reserve_in(document, response.markup, target_slot_time)
                    if relocated is not None:
                        config = relocated
                        slot_time = target_slot_time
                        body = self.session.build_body(config)
                        max_attempts += 1
                        logger.info(
                            "DIRECT_HTTP: %s; re-asking for the target %s between fallbacks",
                            observation.reason,
                            target_slot_time.strftime("%I:%M %p"),
                        )
                        continue

                next_candidate = self._next_candidate(document, response.markup, remaining)
                if next_candidate is None:
                    logger.warning(
                        "DIRECT_HTTP: %s and no fallback tee time left to try",
                        observation.reason,
                    )
                    break
                config, slot_time = next_candidate
                body = self.session.build_body(config)
                logger.info(
                    "DIRECT_HTTP: %s; falling back to %s",
                    observation.reason,
                    slot_time.strftime("%I:%M %p"),
                )
                continue

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
            if next_candidate is None and walk_cycles_left > 0:
                # The burst asked the first fallbacks inside the first seconds,
                # when a Friday's gate may still have been shut; the list is
                # walked again so each is asked at the instants both prior
                # Friday grants actually came (+4.9s, +5.3s). The target leads
                # the second pass for the same reason.
                walk_cycles_left -= 1
                remaining = [
                    candidate
                    for candidate in (target_slot_time, *self._fallback_times)
                    if candidate is not None
                ]
                logger.info(
                    "DIRECT_HTTP: %s and the list is spent; walking it again from the target "
                    "(%d more pass(es) after this)",
                    observation.reason,
                    walk_cycles_left,
                )
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

        _log_gate_summary(result.attempt_log, "at the end of the race")
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
            # The request may have reached the club, so the browser-retry door
            # is closed (the phase says so). The page's timers are left alone:
            # nothing is held yet, and they are what keeps the shared view
            # moving if the next ask is to see a live sheet.
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
                pair.reason = str(exc)
                pair.observations.append(
                    _failed_observation(
                        exc,
                        attempt=attempt_number,
                        slot_time=slot_time,
                        source=config.source,
                        view_state=view_state,
                        sent_ms_past_window=sent_ms,
                    )
                )
                if isinstance(exc, DirectHttpTimeoutError):
                    # Read or write timeout: the request may have landed, and
                    # that is the one failure that closes the fallback list.
                    never_connected = False
                    pair.timed_out = True
                elif not isinstance(exc, DirectHttpConnectionError):
                    # The club answered - a status, a dead view - and reserved
                    # nothing. Until 2026-09-04 this was filed with the
                    # timeouts, which would have closed the list on a 429.
                    never_connected = False
                continue

            never_connected = False
            assert response is not None
            # Same accounting as the single-Reserve path: this is still the
            # critical path when the opening pair is enabled, so the telemetry
            # walks wait and the post-response segment is measured as a
            # wall/CPU pair. Without this the pipelined mode was the one booking
            # mode still paying for telemetry mid-race, and the only one whose
            # ledger rows carried no timing at all.
            wall_start = time_module.perf_counter()
            cpu_start = time_module.process_time()
            container_cpu_start = _container_cpu_ms()

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
                defer_telemetry=True,
            )
            observation.post_response_wall_ms = int(
                (time_module.perf_counter() - wall_start) * 1000
            )
            observation.post_response_cpu_ms = int((time_module.process_time() - cpu_start) * 1000)
            if container_cpu_start is not None:
                container_cpu_end = _container_cpu_ms()
                if container_cpu_end is not None:
                    observation.container_cpu_ms = container_cpu_end - container_cpu_start
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

    def _burst_members(self, config: AbConfig, body: bytes) -> list["_BurstMember"]:
        """Resolve the burst plan into sendable members, in plan order."""
        members: list[_BurstMember] = []
        for index, (offset_ms, fallback_index, is_target) in enumerate(self._burst_plan_slots()):
            if is_target or fallback_index is None:
                members.append(_BurstMember(index, offset_ms, self._slot_time, config, body, True))
                continue
            slot_time, fallback_config, fallback_body = self._burst_fallback_requests[
                fallback_index
            ]
            members.append(
                _BurstMember(index, offset_ms, slot_time, fallback_config, fallback_body, False)
            )
        return members

    def _fire_opening_burst(
        self,
        config: AbConfig,
        body: bytes,
        *,
        target_timestamp_ms: int,
        window_frame_ms: int | None,
        view_state: str,
        result: DirectBookingResult,
    ) -> "_OpeningBurst":
        """Ask at every instant in the burst plan without waiting for answers.

        The ladder's cadence was one round trip plus a parse between asks -
        700-1030ms on every morning measured - and on all three Friday races on
        record the target was gone before the second ask went out. Whoever is
        taking those slots, a crowd whose retries land every second or so or a
        gate that opens somewhere inside a two-second span, one question per
        750ms is not in that race. The burst puts a request on the club every
        hundred-odd milliseconds through the first seconds of the window, so
        that whatever instant the gate opens, one of ours lands within a fraction
        of a human's refresh-and-click.

        Members are sent detached (see :meth:`PrimeFacesSession.send_detached`)
        on their own threads, each sleeping to its own instant; the first goes
        on this thread because it is the one the morning is timed around.
        Answers are absorbed as they arrive, not in plan order. The first grant
        sets ``won``, and every member that has not yet been sent when it lands
        is skipped rather than fired - which is what keeps a burst that won on
        its first ask from taking a second hold on a fallback. A fallback
        already in flight when the target is granted may still be granted too;
        the target's grant is the one adopted, and the other is reported as a
        surplus hold and left to the club's own hold timer.

        What each failure means is kept apart, because they demand different
        responses: a read timeout may have reached the club and closes the
        fallback walk (``timed_out``); a status - a 429, a 503 - is the club
        declining to reserve anything and closes nothing; a dead view is fatal
        to every later ask; a connection that never opened submitted nothing.

        Every member's answer becomes a ledger row, numbered by plan position so
        the ledger reads in send order whatever order the club answered in.
        """
        lead_ms = int(round(self._lead_ms))
        frame_ms = window_frame_ms if window_frame_ms is not None else target_timestamp_ms
        members = self._burst_members(config, body)
        won = threading.Event()
        burst = _OpeningBurst(observations=[])
        burst_started = time_module.perf_counter()

        def send(member: _BurstMember) -> _BurstExchange:
            """Sleep to the member's instant, then send unless the race is won."""
            if member.index > 0:
                sleep_until(target_timestamp_ms + member.offset_ms - lead_ms)
                if won.is_set():
                    return _BurstExchange(member, None, None, None, skipped=True)
            sent_ms = int(time_module.time() * 1000) - frame_ms
            try:
                response = self.session.send_detached(
                    member.config, body=member.body, timeout_s=_RESERVE_TIMEOUT_S
                )
            except DirectHttpError as exc:
                return _BurstExchange(member, sent_ms, None, exc)
            return _BurstExchange(member, sent_ms, response, None)

        logger.info(
            "DIRECT_HTTP: Firing opening burst of %d for %s - %s, lead %dms",
            len(members),
            self._slot_time.strftime("%I:%M %p") if self._slot_time else "the staged slot",
            ", ".join(
                f"#{m.index}@+{m.offset_ms}ms "
                f"{'T' if m.is_target else m.slot_time.strftime('%I:%M') if m.slot_time else 'F'}"
                for m in members
            ),
            lead_ms,
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(members) - 1), thread_name_prefix="reserve-burst"
        ) as pool:
            futures = [pool.submit(send, member) for member in members[1:]]
            self._absorb_burst_exchange(
                burst, send(members[0]), won=won, view_state=view_state, frame_ms=frame_ms
            )
            for future in concurrent.futures.as_completed(futures):
                self._absorb_burst_exchange(
                    burst, future.result(), won=won, view_state=view_state, frame_ms=frame_ms
                )

        burst.observations.sort(key=lambda o: o.attempt)
        if burst.accepted is None and burst.carried is None and burst.unknown is not None:
            # No grant and no refusal, but a 200 whose shape was neither. The
            # ladder treats that as progress and lets the next step say what is
            # missing; with nothing better to carry forward, so does this.
            logger.warning(
                "DIRECT_HTTP: No burst member was granted or refused; carrying an "
                "unreadable answer forward (%s)",
                burst.unknown_reason,
            )
            burst.accepted = burst.unknown
            burst.accepted_slot_time = burst.unknown_slot_time
            burst.accepted_member = burst.unknown_member
            burst.accepted_reason = burst.unknown_reason
        sent = len(members) - burst.skipped
        result.timing["burstMembers"] = len(members)
        result.timing["burstSent"] = sent
        result.timing["burstSkipped"] = burst.skipped
        result.timing["burstAccepted"] = burst.accepted_member
        result.timing["burstMs"] = int((time_module.perf_counter() - burst_started) * 1000)
        for member_index, slot_time in burst.surplus:
            logger.warning(
                "DIRECT_HTTP: Burst member #%d was also granted %s - a surplus hold, left to "
                "the club's hold timer; the ad-hoc test of this mode is what says whether "
                "the club tolerates two",
                member_index,
                slot_time.strftime("%I:%M %p") if slot_time else "a slot",
            )
        logger.info(
            "DIRECT_HTTP: Opening burst done in %dms - %d sent, %d skipped after the grant, "
            "%d refused, %d errored, %d never answered%s",
            result.timing["burstMs"],
            sent,
            burst.skipped,
            sum(1 for o in burst.observations if o.verdict == RESERVE_REFUSED),
            sum(1 for o in burst.observations if o.verdict == RESERVE_ERRORED),
            sum(1 for o in burst.observations if o.verdict == RESERVE_TIMEDOUT),
            f"; granted by member #{burst.accepted_member}"
            if burst.accepted_member is not None
            else "; nothing granted",
        )
        return burst

    def _absorb_burst_exchange(
        self,
        burst: "_OpeningBurst",
        exchange: "_BurstExchange",
        *,
        won: threading.Event,
        view_state: str,
        frame_ms: int,
    ) -> None:
        """Fold one member's answer into the burst's record, as it arrives."""
        member = exchange.member
        attempt_number = member.index + 1
        if exchange.skipped:
            burst.skipped += 1
            return
        if exchange.error is not None:
            exc = exchange.error
            burst.reason = str(exc)
            observation = _failed_observation(
                exc,
                attempt=attempt_number,
                slot_time=member.slot_time,
                source=member.config.source,
                view_state=view_state,
                sent_ms_past_window=exchange.sent_ms,
                burst_index=member.index,
            )
            burst.observations.append(observation)
            _log_reserve_observation(observation)
            if isinstance(exc, DirectHttpTimeoutError):
                burst.never_connected = False
                burst.timed_out = True
            elif isinstance(exc, ViewExpiredError):
                burst.never_connected = False
                if burst.view_expired is None:
                    burst.view_expired = exc
            elif not isinstance(exc, DirectHttpConnectionError):
                burst.never_connected = False
            return

        burst.never_connected = False
        response = exchange.response
        assert response is not None
        wall_start = time_module.perf_counter()
        cpu_start = time_module.process_time()
        container_cpu_start = _container_cpu_ms()

        document = parse_html(response.markup)
        observation = observe_reserve_response(
            attempt=attempt_number,
            slot_time=member.slot_time,
            source=member.config.source,
            view_state=view_state,
            response=response,
            document=document,
            markup=response.markup,
            target_timestamp_ms=frame_ms,
            defer_telemetry=True,
            burst_index=member.index,
        )
        observation.post_response_wall_ms = int((time_module.perf_counter() - wall_start) * 1000)
        observation.post_response_cpu_ms = int((time_module.process_time() - cpu_start) * 1000)
        if container_cpu_start is not None:
            container_cpu_end = _container_cpu_ms()
            if container_cpu_end is not None:
                observation.container_cpu_ms = container_cpu_end - container_cpu_start

        if observation.verdict == RESERVE_REFUSED:
            # Frozen-view check, in arrival order: a refusal identical to the
            # last one is the club re-rendering the same snapshot, and once it
            # has happened twice the sheet in these answers is not live.
            observation.identical_to_previous = (
                burst.last_refusal_digest is not None
                and observation.body_digest == burst.last_refusal_digest
            )
            burst.last_refusal_digest = observation.body_digest
            if observation.identical_to_previous:
                burst.identical_refusals += 1
                if burst.identical_refusals == 1:
                    logger.warning(
                        "DIRECT_HTTP: Burst member #%d's refusal is byte-identical to the "
                        "previous one - the club is re-rendering a frozen view; its "
                        "sheet-open marker and rows describe our snapshot, not the club",
                        member.index,
                    )
        burst.observations.append(observation)
        _log_reserve_observation(observation)

        if observation.verdict == RESERVE_ACCEPTED:
            won.set()
            if burst.accepted is None:
                burst.accepted = response
                burst.accepted_slot_time = member.slot_time
                burst.accepted_member = member.index
                burst.accepted_reason = observation.reason
                burst.accepted_is_target = member.is_target
            elif member.is_target and not burst.accepted_is_target:
                # The target's own grant outranks a fallback's: it is the tee
                # time the member asked for. The fallback's hold becomes the
                # surplus one.
                burst.surplus.append((burst.accepted_member or 0, burst.accepted_slot_time))
                burst.accepted = response
                burst.accepted_slot_time = member.slot_time
                burst.accepted_member = member.index
                burst.accepted_reason = observation.reason
                burst.accepted_is_target = True
            else:
                burst.surplus.append((member.index, member.slot_time))
            return

        if observation.verdict == RESERVE_REFUSED:
            burst.reason = observation.reason
            # Latest refusal by plan order is carried forward: the walk after
            # the burst relocates its fallbacks in this sheet.
            if (
                burst.carried_observation is None
                or attempt_number > burst.carried_observation.attempt
            ):
                burst.carried = response
                burst.carried_document = document
                burst.carried_observation = observation
            return

        # Neither shape. Kept aside; adopted only if nothing better comes.
        if burst.unknown is None:
            burst.unknown = response
            burst.unknown_slot_time = member.slot_time
            burst.unknown_member = member.index
            burst.unknown_reason = observation.reason

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

    def _fill_placeholder_selects(self, response: PartialResponse) -> PartialResponse | None:
        """Best-guess every player-row ``<select>`` still on its placeholder.

        Generic on purpose: this does not look for "Resource" by name, only
        for any player-row dropdown the site itself renders unset. Whatever
        the club decides to require next, its first real option is guessed
        the same way. One field at a time, like :meth:`_add_tbd_guests`,
        because setting one can change what the next response renders.

        Returns:
            The response after every unset field was set, or None if nothing
            was unset, or if any field could not be resolved to a request -
            in which case the caller has no reason to retry Book Now, because
            the form would still be short one required field.
        """
        filled_any = False
        # A handful of fields per player, not an unbounded loop; if a field
        # can't be resolved to a request the loop stops rather than spin.
        for _ in range(_MAX_PLAYERS * 2):
            document = parse_html(response.markup)
            selects = _find_unset_player_selects(document)
            if not selects:
                break
            select = selects[0]
            value = _first_real_option_value(select)
            name = select.attrs.get("name") or select.id
            config = find_ab_for_element(select, response.markup)
            if value is None or not name or config is None:
                # Filling every *other* field and retrying Book Now would
                # still be refused over this one, so there is nothing to gain
                # from handing back a partly-fixed response.
                logger.warning(
                    "DIRECT_HTTP: %s is unset but has no resolvable option/handler; "
                    "leaving it as-is",
                    select.id,
                )
                return None
            self.session.form_state.set_field(name, value)
            logger.info(
                "DIRECT_HTTP: %s still unset at Book Now; best-guessing %s and resubmitting",
                select.id,
                value,
            )
            response = self.session.post(config)
            filled_any = True

        if not filled_any:
            return None
        if _find_unset_player_selects(parse_html(response.markup)):
            # The loop ran out of iterations before clearing every field -
            # same reasoning as above: a retry now would still be short one.
            return None
        return response


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


def _is_unset_select(select: Node) -> bool:
    """Report whether a ``<select>`` is still on its own placeholder option.

    The site marks no ``<option>`` as ``selected`` when a field is unset,
    which a browser defaults to showing the first one - and every such field
    seen on this site labels that first option "--Select ...--". A select
    with an explicit ``selected`` option, or whose first option is a real
    choice rather than a placeholder, was not left unset by this chain.
    """
    options = select.find_all("option")
    if not options:
        return False
    if any(option.attrs.get("selected") is not None for option in options):
        return False
    return options[0].text_content().strip().startswith("--")


def _first_real_option_value(select: Node) -> str | None:
    """The first selectable, non-placeholder ``<option>``'s value.

    None if there is no such option - including when every real option is
    disabled, which is the club's own way of saying a choice cannot be made
    right now, not a value to submit as a best guess.
    """
    for option in select.find_all("option"):
        if "disabled" in option.attrs:
            continue
        label = option.text_content().strip()
        if label and not label.startswith("--"):
            return option.attrs.get("value")
    return None


def _find_unset_player_selects(document: Node) -> list[Node]:
    """Every ``<select>`` in a rendered player row still on its placeholder.

    Scoped to player rows, not the whole document, so this cannot pick up an
    unrelated dropdown elsewhere on the page (the course/date pickers, the
    time-period filter) that happens to share the same "--Select ...--"
    convention.
    """
    selects = []
    for row in _find_player_rows(document):
        for node in row.descendants():
            if node.tag == "select" and _is_unset_select(node):
                selects.append(node)
    return selects


def _book_now_still_pending(document: Node) -> bool:
    """Report whether a Book Now response left the booking form in place.

    A genuine submission moves the view past this form; one that still
    renders the Book Now action itself, alongside a player-row field the
    chain never set, is the club silently declining to advance rather than
    an accepted booking - see 2026-08-22, where this was the only structural
    sign of a refusal the club described in prose but never surfaced through
    a message container this chain recognized.
    """
    if _find_book_now(document) is None:
        return False
    return bool(_find_unset_player_selects(document))


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
# The club answered with something other than HTTP 200 - a 429, a 503, an error
# page. It received the request and reserved nothing, so unlike a timeout this
# leaves the fallback list open: there is no invisible hold to stack on. Kept
# apart from RESERVE_UNKNOWN too, because that one is a 200 whose body could
# not be read, and a burst being throttled has to be visible as exactly that.
RESERVE_ERRORED = "errored"

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


# Cumulative CPU consumed by this cgroup, which on Cloud Run is the container.
# v2 reports microseconds in a key/value file; v1 reports nanoseconds in a bare
# one, and the controller can be mounted under either name.
_CGROUP_V2_CPU_STAT = "/sys/fs/cgroup/cpu.stat"
_CGROUP_V1_CPU_USAGE = (
    "/sys/fs/cgroup/cpuacct/cpuacct.usage",
    "/sys/fs/cgroup/cpu/cpuacct.usage",
)


def _container_cpu_ms() -> int | None:
    """CPU milliseconds burned by *every* process in this container, or None.

    Read alongside this process's own CPU time it answers the second half of the
    2026-08-21 question. If the container burned far more than we did over the
    same span, another process took the CPU - and Chrome is the standing
    suspect, since the driver stays open on the tee sheet page, with its own JS
    timers running, until well after the race is decided.

    Read from the cgroup rather than from /proc/stat, which was the first
    attempt and was wrong twice over. /proc/stat is host-wide, so its delta
    counts processes this container has nothing to do with; and its columns
    include *idle*, so summing them advances by roughly wall-clock x CPU-count
    no matter how little work is done. On a 4-CPU box that turns a 564ms span
    into ~2256ms of apparent CPU - which would have read as heavy contention on
    an idle machine, in the one field built to tell contention from a slow
    vCPU. A diagnostic that manufactures its own answer is worse than none.

    Best-effort by design: cgroup layouts vary and this must never be the reason
    a booking fails, so every failure reads as "no measurement" rather than
    raising into the race.
    """
    try:
        with open(_CGROUP_V2_CPU_STAT, encoding="ascii") as handle:
            for line in handle:
                key, _, value = line.partition(" ")
                if key == "usage_usec":
                    return int(value) // 1000
    except (OSError, ValueError):
        pass

    for path in _CGROUP_V1_CPU_USAGE:
        try:
            with open(path, encoding="ascii") as handle:
                return int(handle.read().strip()) // 1_000_000
        except (OSError, ValueError):
            continue
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
    # Transport facts, kept since 2026-09-04 so that a throttled or erroring
    # burst is legible from the ledger alone: the status, the handful of
    # headers _LEDGER_HEADERS names (Retry-After above all), and for a non-200
    # the start of the body, where an error page's own words are.
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    error_body: str | None = None
    # Position in the opening burst's plan, or None for a serial ask.
    burst_index: int | None = None
    # A short digest of the markup, and whether it matched the refusal before
    # it. The frozen-view signature of 2026-09-04 was fourteen identical bodies
    # that took a post-mortem to notice; this makes it a field.
    body_digest: str | None = None
    identical_to_previous: bool | None = None

    def as_row(self) -> dict[str, Any]:
        """Flatten for the JSONL ledger."""
        return {
            "attempt": self.attempt,
            "burstIndex": self.burst_index,
            "statusCode": self.status_code,
            "responseHeaders": self.response_headers,
            "errorBody": self.error_body,
            "bodyDigest": self.body_digest,
            "identicalToPrevious": self.identical_to_previous,
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
            # True means countdownS and popupPresent were never read, not that
            # the sheet had neither. Without it a failed backfill is
            # indistinguishable in the ledger from a clean response.
            "telemetryDeferred": self.telemetry_deferred,
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


@dataclass
class _BurstMember:
    """One planned send of the opening burst: when, for which slot, with what."""

    index: int
    offset_ms: int
    slot_time: time | None
    config: AbConfig
    body: bytes
    is_target: bool


@dataclass
class _BurstExchange:
    """What one burst member came back with - an answer, a failure, or nothing.

    ``skipped`` means the member reached its instant after a grant had already
    landed and was deliberately not sent.
    """

    member: _BurstMember
    sent_ms: int | None
    response: PartialResponse | None
    error: Exception | None
    skipped: bool = False


@dataclass
class _OpeningBurst:
    """What the opening burst came back with, as absorbed in arrival order.

    ``accepted`` is the grant adopted - the target's if it had one, else the
    first fallback's; ``surplus`` names any other grant, which is a hold the
    club will release on its own timer. ``carried`` is the latest refusal by
    plan order, the sheet the fallback walk continues against when nothing was
    granted. Exactly one response is adopted before the chain goes on.
    """

    observations: list[ReserveObservation]
    accepted: PartialResponse | None = None
    accepted_slot_time: time | None = None
    accepted_member: int | None = None
    accepted_reason: str | None = None
    accepted_is_target: bool = False
    surplus: list[tuple[int, time | None]] = field(default_factory=list)
    carried: PartialResponse | None = None
    carried_document: Node | None = None
    carried_observation: ReserveObservation | None = None
    # A 200 whose body was neither a grant nor a refusal, kept aside.
    unknown: PartialResponse | None = None
    unknown_slot_time: time | None = None
    unknown_member: int | None = None
    unknown_reason: str | None = None
    # A read or write timeout on any member: it may have reached the club, and
    # closes the fallback walk for the rest of the run.
    timed_out: bool = False
    # Starts True and is cleared by the first member that reached the socket.
    never_connected: bool = True
    view_expired: ViewExpiredError | None = None
    skipped: int = 0
    reason: str | None = None
    # Frozen-view bookkeeping, in arrival order.
    last_refusal_digest: str | None = None
    identical_refusals: int = 0


def _body_digest(markup: str) -> str:
    """A short, stable digest of a response's markup, for the frozen-view check."""
    return hashlib.sha1(markup.encode("utf-8", errors="replace")).hexdigest()[:12]  # noqa: S324


def _failed_observation(
    exc: Exception,
    *,
    attempt: int,
    slot_time: time | None,
    source: str,
    view_state: str,
    sent_ms_past_window: int | None,
    burst_index: int | None = None,
) -> ReserveObservation:
    """The ledger row for a Reserve that raised instead of answering.

    A timeout is ``RESERVE_TIMEDOUT`` and carries the budget as its round trip,
    as it always has. Everything else - a status, a dead view, a connection that
    never opened - is ``RESERVE_ERRORED``, and a status error brings its status,
    headers and body along, which is what tells a rate limit from an outage.
    """
    if isinstance(exc, DirectHttpTimeoutError):
        return ReserveObservation(
            attempt=attempt,
            slot_time=slot_time,
            source=source,
            view_state=view_state,
            verdict=RESERVE_TIMEDOUT,
            reason=str(exc),
            sent_ms_past_window=sent_ms_past_window,
            round_trip_ms=int(_RESERVE_TIMEOUT_S * 1000),
            burst_index=burst_index,
        )
    observation = ReserveObservation(
        attempt=attempt,
        slot_time=slot_time,
        source=source,
        view_state=view_state,
        verdict=RESERVE_ERRORED,
        reason=str(exc),
        sent_ms_past_window=sent_ms_past_window,
        burst_index=burst_index,
    )
    if isinstance(exc, DirectHttpStatusError):
        observation.status_code = exc.status_code
        observation.response_headers = dict(exc.headers)
        observation.error_body = exc.body_snippet
    return observation


def _log_gate_summary(observations: list[ReserveObservation], when: str) -> None:
    """Say, per club-second, what was asked and what was granted.

    This is the only way the two refusals the club words identically can be
    told apart, and it needs more than one slot in play: a grant for *any* slot
    inside a club-second proves the gate was open in that second, which turns
    every refusal in the same second or later into a slot that was taken. No
    grant across several different slots in a second is strong evidence the
    gate was still shut. Nothing in a single response can say which - the
    sheet-open marker and the rows in a refusal describe our own view as of its
    last refresh, not the club (2026-09-04).
    """
    answered = [o for o in observations if o.server_ms_past_window is not None]
    if not answered:
        return
    by_second: dict[int, list[ReserveObservation]] = {}
    for observation in answered:
        by_second.setdefault(observation.server_ms_past_window // 1000, []).append(observation)  # type: ignore[operator]
    first_grant_second: int | None = None
    for second in sorted(by_second):
        rows = by_second[second]
        slots = sorted({o.slot_time.strftime("%I:%M %p") for o in rows if o.slot_time is not None})
        grants = [o for o in rows if o.verdict == RESERVE_ACCEPTED]
        if grants and first_grant_second is None:
            first_grant_second = second
        logger.info(
            "GATE: club :%02d - asked %s (%d ask(s)) - %s",
            second,
            ", ".join(slots) or "?",
            len(rows),
            (
                "GRANTED "
                + ", ".join(
                    o.slot_time.strftime("%I:%M %p") if o.slot_time else "?" for o in grants
                )
            )
            if grants
            else "granted none"
            + (
                f" ({sum(1 for o in rows if o.verdict == RESERVE_ERRORED)} errored)"
                if any(o.verdict == RESERVE_ERRORED for o in rows)
                else ""
            ),
        )
    distinct_slots = {o.slot_time for o in answered if o.slot_time is not None}
    if first_grant_second is not None:
        logger.info(
            "GATE: %s - gate was open by club :%02d; refusals from that second on were taken "
            "slots, refusals before it are ambiguous (gate or taken)",
            when,
            first_grant_second,
        )
    else:
        seconds = sorted(by_second)
        logger.warning(
            "GATE: %s - no grant in any club-second asked (:%02d..:%02d) across %d distinct "
            "slot(s) - the gate was still shut, or every slot asked was already gone",
            when,
            seconds[0],
            seconds[-1],
            len(distinct_slots),
        )


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
    burst_index: int | None = None,
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
        status_code=response.status_code,
        response_headers=dict(response.headers),
        burst_index=burst_index,
        body_digest=_body_digest(markup),
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
    extras = ""
    if observation.burst_index is not None:
        extras += f", burst=#{observation.burst_index}"
    if observation.status_code is not None and observation.status_code != 200:
        extras += f", status={observation.status_code}"
        if "retry-after" in observation.response_headers:
            extras += f", retry-after={observation.response_headers['retry-after']}"
    if observation.identical_to_previous:
        extras += ", frozen=True"
    logger.info(
        "DIRECT_HTTP: Reserve %d -> %s (%s) - sent %sms past the window, club clock %sms "
        "past, round trip %sms, %d bytes, %d sheet row(s), %d Reserve button(s), "
        "form=%s, countdown=%s, popup=%s, sheet=%s, post-response %s%s",
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
        extras,
    )
    if observation.error_body:
        logger.warning(
            "DIRECT_HTTP: Reserve %d error body: %r",
            observation.attempt,
            observation.error_body[:_ERROR_BODY_LOG_CHARS],
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
