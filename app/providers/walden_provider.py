import asyncio
import functools
import json
import logging
import os
import re
import time as time_module
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, TypeVar

import google.auth
import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from app.config import settings
from app.providers.base import (
    BatchBookingItemResult,
    BatchBookingRequest,
    BatchBookingResult,
    BookingResult,
    ReservationProvider,
)
from app.providers.wait_helper import WaitStrategy
from app.providers.walden_dom_schema import DOM
from app.providers.walden_http import PrimeFacesSession, visible_text
from app.providers.walden_http_booker import (
    DIRECT_HTTP_PATH,
    PRE_SUBMIT_PHASES,
    RESERVE_ACCEPTED,
    RESERVE_REFUSED,
    RESERVE_TIMEDOUT,
    DirectHttpBooker,
    backfill_reserve_telemetry,
    container_message_text,
)
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_EXCEPTIONS = (
    StaleElementReferenceException,
    ElementClickInterceptedException,
    TimeoutException,
)

# Raw partial-responses stored per race, out of up to a full sweep's worth. Each
# refusal is a whole tee sheet at 500-680KB and consecutive ones are the same
# sheet, so the ends are what get read. Ledger rows are kept for every attempt.
_RACE_LEDGER_MAX_PAYLOADS = 4


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    exceptions: tuple[type[Exception], ...] = TRANSIENT_EXCEPTIONS,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for retrying operations that may fail due to transient Selenium issues.

    Uses exponential backoff between attempts. Only retries on specified exception types.

    Args:
        max_attempts: Maximum number of attempts (default 3)
        backoff_base: Base delay in seconds, doubled each attempt (default 0.5)
        exceptions: Tuple of exception types to retry on
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        """Wrap a function with retry logic."""

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            """Execute the wrapped function with exponential-backoff retries."""
            last_exception: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = backoff_base * (2**attempt)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time_module.sleep(delay)
                    else:
                        logger.error(f"All {max_attempts} attempts failed for {func.__name__}: {e}")
            raise last_exception  # type: ignore[misc]

        return wrapper

    return decorator


# Shared JavaScript helper for blocked popup detection and dismissal.
# Used by both _execute_fast_booking_chain_js and _stage_timed_booking_chain_js
# to avoid code duplication across the six popup-check callsites.
_JS_BLOCKED_POPUP_HELPERS = """
// Helper: Check if popup matches blocked patterns and return classification
function classifyBlockedPopup(popup, blockedPatterns) {
    var text = (popup.textContent || '').toLowerCase();
    for (var i = 0; i < blockedPatterns.length; i++) {
        if (text.indexOf(blockedPatterns[i].toLowerCase()) !== -1) {
            return {blocked: true, text: text};
        }
    }
    return {blocked: false, text: text};
}

// Helper: Dismiss popup by clicking OK button
function dismissPopup(popup) {
    var ok = popup.querySelector('a.dialogOKBtn, a[id*="j_idt1076"]');
    if (ok) ok.click();
}

// Helper: Find visible blocked popup
function findVisibleBlockedPopup() {
    var popup = document.querySelector("div[id*='teeSheetValidationErrorPopup']");
    if (popup && popup.getAttribute('aria-hidden') === 'false') {
        return popup;
    }
    return null;
}
"""


# Shared async booking chain used by both the fast path (subsequent batch
# bookings) and the timed path (the 6:30:00 race). Runs via
# execute_async_script: every wait is setTimeout-based so the page event loop
# keeps running between polls. This is essential - the things the chain waits
# for (the site's own timer removing the 'disable-div' overlay at 6:30, the
# PrimeFaces AJAX response that renders the player page, the blocked-slot
# popup) are all delivered BY that event loop. The previous synchronous
# spin-wait implementation blocked the loop and starved every one of them.
# The only remaining busy-wait is the final <=25ms before the target
# timestamp, for sub-millisecond click precision.
#
# Arguments (Selenium appends the async callback as the last argument):
#   0: slotIndex          index into li.ui-datascroller-item
#   1: numPlayers         1-4
#   2: targetTimestampMs  epoch ms to click Reserve at, or null for "now"
#   3: maxWaitMs          player-selector wait budget after Reserve click
#   4: pollIntervalMs     polling cadence for element waits
#   5: blockedPatterns    substrings identifying the blocked-slot popup
#   6: enabledMaxWaitMs   wait budget for disable-div removal
_JS_ASYNC_BOOKING_CHAIN = """
        var slotIndex = arguments[0];
        var numPlayers = arguments[1];
        var targetTimestampMs = arguments[2];
        var maxWaitMs = arguments[3];
        var pollIntervalMs = arguments[4];
        var blockedPatterns = arguments[5];
        var enabledMaxWaitMs = arguments[6];
        var done = arguments[arguments.length - 1];

        var result = {
            success: false,
            error: null,
            blocked: false,
            phase: 'init',
            timing: {}
        };
        var startTime = null;  // click-time reference; set in waitEnabled()

        function finish() { done(result); }

        // Any exception inside a scheduled callback would otherwise leave the
        // async script hanging (done never called) until Selenium's script
        // timeout - minutes, on the timed path. Route every continuation
        // through this guard so failures surface immediately instead.
        function guard(fn) {
            return function() {
                try { return fn.apply(null, arguments); }
                catch (e) {
                    result.error = 'JS chain error in phase ' + result.phase + ': ' + e.message;
                    finish();
                }
            };
        }

        // Async poll: checkFn is evaluated now and then every intervalMs via
        // setTimeout, yielding to the event loop between checks so page JS
        // (AJAX handlers, site timers) can run. Truthy return -> onFound(value).
        // The tick (including onFound/onTimeout) is guarded: a throw completes
        // the script with an error rather than hanging it.
        function pollUntil(checkFn, timeoutMs, intervalMs, onFound, onTimeout) {
            var deadline = Date.now() + timeoutMs;
            var tick = guard(function() {
                var val = null;
                try { val = checkFn(); } catch (e) { /* keep polling */ }
                if (val) { onFound(val); return; }
                if (Date.now() >= deadline) { onTimeout(); return; }
                setTimeout(tick, intervalMs);
            });
            tick();
        }

        function handlePopup(popup, timingKey, suffix) {
            var classification = classifyBlockedPopup(popup, blockedPatterns);
            if (classification.blocked) {
                result.blocked = true;
                result.error = 'Slot blocked by another user' + (suffix || '');
            } else {
                result.error = 'Validation error: ' + classification.text.substring(0, 100);
            }
            result.timing[timingKey] = Date.now() - startTime;
            dismissPopup(popup);
            finish();
        }

        // Phase 0: Pre-locate the Reserve button BEFORE any waiting
        result.phase = 'pre_locate';
        var items = document.querySelectorAll('li.ui-datascroller-item');
        var item = items[slotIndex];
        if (!item) {
            result.error = 'Slot item not found at index ' + slotIndex;
            return finish();
        }

        var reserveBtn = item.querySelector("a[id*='reserve_button']");
        if (!reserveBtn) {
            var spans = item.querySelectorAll('span.custom-free-slot-span');
            reserveBtn = spans.length > 0 ? spans[0] : null;
        }
        if (!reserveBtn) reserveBtn = item.querySelector('a.slot-link');
        if (!reserveBtn) {
            result.error = 'Reserve button not found in slot';
            return finish();
        }

        // Scroll into view NOW (before waiting) to eliminate scroll latency at 6:30
        reserveBtn.scrollIntoView({block: 'center'});
        result.timing.preLocatedAt = Date.now();

        // Phase 1: Wait until the target timestamp (timed mode only).
        // Coarse wait via setTimeout (event loop stays free for the site's own
        // JS), then spin only the final ~25ms for sub-millisecond precision.
        function beginPrecisionWait() {
            if (targetTimestampMs === null || targetTimestampMs === undefined) {
                waitEnabled();
                return;
            }
            result.phase = 'precision_wait';
            result.timing.msUntilTarget = targetTimestampMs - Date.now();
            var coarseMs = targetTimestampMs - Date.now() - 25;
            if (coarseMs > 0) {
                setTimeout(guard(finalSpin), coarseMs);
            } else {
                finalSpin();
            }
        }

        function finalSpin() {
            while (Date.now() < targetTimestampMs) { /* spin */ }
            waitEnabled();
        }

        // Phase 1.5: Wait for the site's JS to remove the 'disable-div' class
        // that gates the tee sheet until booking opens. Clicking while it is
        // present yields a server-side rejection. Because pollUntil yields to
        // the event loop, the site's removal timer can actually fire here.
        function waitEnabled() {
            startTime = Date.now();
            if (targetTimestampMs !== null && targetTimestampMs !== undefined) {
                // Drift of the wait start vs target; the true click drift is
                // recorded in clickReserve() (it additionally includes any
                // disable-div wait).
                result.timing.waitStartDriftMs = startTime - targetTimestampMs;
            }
            // The coarse wait yields to the page event loop, so a PrimeFaces
            // partial update may have replaced the pre-located nodes. A
            // detached node reads stale classes and swallows click() silently.
            if (!item.isConnected) {
                var freshItems = document.querySelectorAll('li.ui-datascroller-item');
                var freshItem = freshItems[slotIndex];
                if (!freshItem) {
                    result.error = 'Slot item disappeared before click at index ' + slotIndex;
                    return finish();
                }
                item = freshItem;
                reserveBtn = item.querySelector("a[id*='reserve_button']") ||
                    item.querySelector('span.custom-free-slot-span') ||
                    item.querySelector('a.slot-link');
                if (!reserveBtn) {
                    result.error = 'Reserve button disappeared before click';
                    return finish();
                }
                result.timing.slotReQueried = true;
            }
            result.phase = 'wait_enabled';
            var slotContainer = item.closest('.ui-datascroller');
            var hasDisableDiv = function() {
                return !!(slotContainer && slotContainer.classList.contains('disable-div'));
            };
            result.timing.disableDivPresentAtStart = hasDisableDiv();
            if (!result.timing.disableDivPresentAtStart) {
                result.timing.disableDivWaitMs = 0;
                result.timing.slotsEnabledAfterWait = true;
                clickReserve();
                return;
            }
            var enabledWaitStart = Date.now();
            pollUntil(
                function() { return hasDisableDiv() ? null : true; },
                enabledMaxWaitMs,
                2,  // tight 2ms cadence: click within ~2ms of the overlay coming off
                function() {
                    result.timing.disableDivWaitMs = Date.now() - enabledWaitStart;
                    result.timing.slotsEnabledAfterWait = true;
                    clickReserve();
                },
                function() {
                    result.timing.disableDivWaitMs = Date.now() - enabledWaitStart;
                    result.timing.slotsEnabledAfterWait = false;
                    result.timing.slotsStillDisabled = true;
                    result.timing.containerClasses =
                        slotContainer ? slotContainer.className : 'container_not_found';
                    result.error = 'Slots still disabled (disable-div present) after ' +
                        enabledMaxWaitMs + 'ms';
                    finish();
                }
            );
        }

        // Phase 2: Click Reserve
        function clickReserve() {
            result.phase = 'reserve_click';
            result.timing.clickAfterEnabledMs = Date.now() - startTime;
            reserveBtn.click();
            result.timing.actualClickTime = Date.now();
            result.timing.reserveClicked = result.timing.actualClickTime - startTime;
            if (targetTimestampMs !== null && targetTimestampMs !== undefined) {
                result.timing.clickDriftMs = result.timing.actualClickTime - targetTimestampMs;
            }
            waitPlayerSelector();
        }

        // Phase 3: Wait for the player-count selector to render, watching for
        // the blocked-slot popup the whole time (single merged poll).
        function waitPlayerSelector() {
            result.phase = 'player_count_wait';
            pollUntil(
                function() {
                    var popup = findVisibleBlockedPopup();
                    if (popup) return {popup: popup};
                    var groups = document.querySelectorAll('.ui-selectonebutton');
                    for (var i = 0; i < groups.length; i++) {
                        var radio = groups[i].querySelector(
                            'input[type="radio"][value="' + numPlayers + '"]');
                        if (radio) {
                            // Skip the time filter group (it has a value="0" option)
                            var hasValue0 = groups[i].querySelector(
                                'input[type="radio"][value="0"]');
                            if (!hasValue0) {
                                return {selector: {group: groups[i], radio: radio}};
                            }
                        }
                    }
                    return null;
                },
                maxWaitMs,
                pollIntervalMs,
                function(found) {
                    if (found.popup) {
                        handlePopup(found.popup, 'blockedDetectedDuringWait');
                        return;
                    }
                    result.timing.playerSelectorFound = Date.now() - startTime;
                    clickPlayerCount(found.selector);
                },
                function() {
                    var finalPopup = findVisibleBlockedPopup();
                    if (finalPopup) {
                        handlePopup(finalPopup, 'blockedDetectedAfterTimeout',
                            ' (detected after timeout)');
                        return;
                    }
                    result.error = 'Player count selector not found within ' + maxWaitMs + 'ms';
                    result.timing.playerSelectorTimeout = Date.now() - startTime;
                    finish();
                }
            );
        }

        // Phase 4: Select player count. The page may restore the last-used
        // count (e.g. via "Use Last Play"); clicking an already-active
        // PrimeFaces button toggles it off, so skip the click in that case.
        function clickPlayerCount(sel) {
            result.phase = 'player_count_click';
            var playerButton = sel.radio.parentElement;
            if (playerButton.classList.contains('ui-state-disabled')) {
                result.error = 'Player count ' + numPlayers + ' button is disabled';
                return finish();
            }
            if (sel.radio.checked || playerButton.classList.contains('ui-state-active')) {
                result.timing.playerCountAlreadySelected = true;
            } else {
                playerButton.click();
            }
            result.timing.playerCountClicked = Date.now() - startTime;
            tbdGuests();
        }

        function queryPlayerRows() {
            var rows = document.querySelectorAll('[id*="playersTable"] tbody tr[data-ri]');
            if (rows.length === 0) {
                rows = document.querySelectorAll('table[id*="player"] tbody tr');
            }
            return rows;
        }

        // Phase 5: Add TBD guests (rows re-queried each step; 200ms yields let
        // the PrimeFaces AJAX row updates actually process between clicks)
        function tbdGuests() {
            if (numPlayers <= 1) {
                bookNowPhase();
                return;
            }
            result.phase = 'tbd_guests';
            pollUntil(
                function() {
                    var rows = queryPlayerRows();
                    return rows.length >= numPlayers ? rows : null;
                },
                3000,
                pollIntervalMs,
                function() {
                    result.timing.playerRowsFound = Date.now() - startTime;
                    clickNextTbd(0, numPlayers - 1);
                },
                function() {
                    result.error = 'Player rows did not appear after selecting ' +
                        numPlayers + ' players';
                    finish();
                }
            );
        }

        function clickNextTbd(tbdClicked, numTbd) {
            if (tbdClicked >= numTbd) {
                result.timing.tbdGuestsAdded = Date.now() - startTime;
                bookNowPhase();
                return;
            }
            var currentRows = queryPlayerRows();
            if (currentRows.length <= 1) {
                result.error = 'Player rows disappeared while clicking TBD buttons';
                return finish();
            }
            var guestIndex = tbdClicked + 1;  // 1-indexed: row 0 is the member
            if (guestIndex >= currentRows.length) {
                result.error = 'Not enough player rows for TBD guests';
                return finish();
            }
            var row = currentRows[guestIndex];
            var tbd = row.querySelector('a[id*="tbd"], span[id*="tbd"], a.ui-commandlink');
            if (!tbd) {
                var links = row.querySelectorAll('a');
                for (var l = 0; l < links.length; l++) {
                    if (links[l].textContent &&
                        links[l].textContent.toUpperCase().indexOf('TBD') !== -1) {
                        tbd = links[l];
                        break;
                    }
                }
            }
            if (!tbd) {
                result.error = 'TBD button not found on guest row ' + guestIndex;
                return finish();
            }
            tbd.click();
            setTimeout(guard(function() { clickNextTbd(tbdClicked + 1, numTbd); }), 200);
        }

        // Phase 6: Click Book Now
        function bookNowPhase() {
            result.phase = 'book_now';
            pollUntil(
                function() {
                    var btn = document.querySelector('a[id*="bookTeeTimeAction"]');
                    if (btn) return btn;
                    // Exact-text fallback only: substring matching ('Book')
                    // would hit nav links like 'Booked' or 'Book a Tee Time'.
                    var links = document.querySelectorAll('a');
                    for (var i = 0; i < links.length; i++) {
                        var txt = (links[i].textContent || '').trim().toLowerCase();
                        if (txt === 'book now' || txt === 'book') {
                            return links[i];
                        }
                    }
                    return null;
                },
                5000,
                pollIntervalMs,
                function(bookNow) {
                    result.timing.bookNowFound = Date.now() - startTime;
                    bookNow.scrollIntoView({block: 'center'});
                    bookNow.click();
                    result.timing.bookNowClicked = Date.now() - startTime;
                    result.phase = 'complete';
                    result.success = true;
                    result.timing.totalMs = Date.now() - startTime;
                    finish();
                },
                function() {
                    result.error = 'Book Now button not found';
                    result.timing.bookNowTimeout = Date.now() - startTime;
                    finish();
                }
            );
        }

        try {
            beginPrecisionWait();
        } catch (e) {
            result.error = 'JS chain error in phase ' + result.phase + ': ' + e.message;
            finish();
        }
"""

# Timing budgets for the shared booking chain
_CHAIN_MAX_WAIT_MS = 5000  # player-selector wait after Reserve click
_CHAIN_POLL_INTERVAL_MS = 10  # element polling cadence
# Wait budget for the site's JS to remove the disable-div overlay at 6:30.
# Generous on purpose: the wait starts at the local-clock target, so this must
# absorb clock offset plus any lag in the site's own enable timer. Waiting
# costs nothing when the overlay comes off early - the 2ms poll clicks
# immediately - but timing out here forfeits the attempt.
_CHAIN_ENABLED_MAX_WAIT_MS = 5000

# Timed mode only: the tee sheet is rendered before the booking window opens,
# but its slots report no availability until the site enables them at 6:30, and
# a PrimeFaces partial update can blank them mid-scan. An empty scan before the
# target therefore says nothing about 6:30 and must not end the attempt - keep
# re-scanning instead.
_SLOT_RESCAN_INTERVAL_S = 0.25
# Near the target the poll interval turns into click latency: a slot that only
# becomes bookable at 6:30 gets clicked however long it takes us to notice it,
# because the JS precision wait no-ops on a target already past. So tighten the
# cadence for the final approach - but only there, and not further. Each scan is
# a ~20ms synchronous DOM traversal of the whole tee sheet, and starving the
# page's event loop at exactly the moment it has to process the disable-div
# removal is the failure #116 had to undo.
_SLOT_RESCAN_FINAL_APPROACH_MS = 3000
_SLOT_RESCAN_FINAL_INTERVAL_S = 0.1
# How long past the target to keep re-scanning, for slots that only become
# bookable as the overlay comes off. Shares the disable-div budget because it
# absorbs the same uncertainty - our clock's offset from the site's enable
# timer - and because overrunning it delays every later booking in the batch.
_SLOT_RESCAN_GRACE_MS = _CHAIN_ENABLED_MAX_WAIT_MS


# The slot scan, as a module constant so a test can run the exact string the
# browser is handed. Its ranking is load-bearing - grid-aligned first, then
# nearest the requested time - and a test that re-sorts its own fixture proves
# nothing about the comparator below.
_SLOT_FINDER_JS = """
var targetHour = arguments[0];
var targetMinute = arguments[1];
var minPlayers = arguments[2];
var fallbackMinutes = arguments[3];
var intervalMinutes = arguments[4];
var excludeTimes = arguments[5];
var northgateIndex = arguments[6];
var maxPlayers = arguments[7];

var targetMinutes = targetHour * 60 + targetMinute;
var items = document.querySelectorAll('li.ui-datascroller-item');
var candidates = [];

// Why slots were dropped, not just how many survived. "0 fallback(s)"
// on 2026-08-08 could not be read as "the sheet was full" or "we
// rejected bookable slots", and those call for opposite fixes.
var rejected = {course: 0, unparsed: 0, window: 0, capacity: 0, excluded: 0};

// Build exclude set for O(1) lookup
var excludeSet = {};
for (var e = 0; e < excludeTimes.length; e++) {
    excludeSet[excludeTimes[e].h * 60 + excludeTimes[e].m] = true;
}

for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var itemHtml = item.innerHTML;

    // Check course via element ID pattern: teeTimeCourses:X
    // Northgate uses index "0", Walden uses index "1"
    var courseMatch = itemHtml.match(/teeTimeCourses:(\\d+)/);
    if (!courseMatch || courseMatch[1] !== northgateIndex) {
        rejected.course++;
        continue; // Skip slots without a course index or non-Northgate slots
    }

    // Extract time from label or text content
    var label = item.querySelector('label');
    var timeText = label ? label.textContent.trim() : '';
    if (!timeText) {
        var allText = item.textContent;
        var timeMatch = allText.match(/(\\d{1,2}):(\\d{2})\\s*([AaPp][Mm])/);
        if (timeMatch) {
            timeText = timeMatch[0];
        }
    }
    if (!timeText) { rejected.unparsed++; continue; }

    // Parse time
    var tmatch = timeText.match(/(\\d{1,2}):(\\d{2})\\s*([AaPp][Mm])/i);
    if (!tmatch) { rejected.unparsed++; continue; }
    var h = parseInt(tmatch[1]);
    var m = parseInt(tmatch[2]);
    var ampm = tmatch[3].toUpperCase();
    if (ampm === 'PM' && h !== 12) h += 12;
    if (ampm === 'AM' && h === 12) h = 0;

    var slotMinutes = h * 60 + m;
    var diff = Math.abs(slotMinutes - targetMinutes);

    // Check fallback window
    if (diff > fallbackMinutes) { rejected.window++; continue; }

    // Grid alignment. This used to drop the slot, which assumes the
    // requested time sits on the sheet's own grid - ask for 8:00 when
    // the day runs 7:56/8:04 and every slot is a non-multiple of 8 away,
    // so the whole list empties and the morning has no fallback at all.
    // Kept as a ranking key instead: aligned slots sort first, so the
    // slot actually reserved is the same one as before, and misaligned
    // ones line up behind it rather than vanishing.
    // Not counted as a rejection: the slot is still a candidate, and a
    // tally that called a fallback we went on to reserve "dropped" would
    // be the same kind of misleading line this whole scan exists to fix.
    // Retained off-grid slots are counted off the candidate list; ones
    // that do get dropped are counted by whichever check drops them.
    var aligned = (diff % intervalMinutes === 0);

    // Check availability
    var emptyDivs = item.querySelectorAll('div.Empty');
    var availableSpans = item.querySelectorAll('span.custom-free-slot-span');
    var isAvailable = false;
    var availableCount = 0;

    if (emptyDivs.length > 0) {
        availableCount = maxPlayers;
        isAvailable = (minPlayers <= maxPlayers);
    } else if (availableSpans.length >= minPlayers) {
        availableCount = availableSpans.length;
        isAvailable = true;
    }

    if (!isAvailable) { rejected.capacity++; continue; }

    // Component id of the slot's Reserve link. The direct-HTTP path
    // replays that component's PrimeFaces request, so it needs the id
    // rather than the NodeList index the JS chain clicks by.
    var reserveEl = item.querySelector("a[id*='reserve_button']") ||
        item.querySelector('a.slot-link');

    var slotInfo = {
        timeStr: h + ':' + (m < 10 ? '0' : '') + m,
        hours: h,
        minutes: m,
        index: i,
        diff: diff,
        available: availableCount,
        isExact: (diff === 0),
        aligned: aligned,
        reserveId: reserveEl ? reserveEl.id : null
    };

    // For fallback slots, skip excluded times. Never for the exact time
    // asked for: that one is the booking, not a stand-in chosen for it.
    if (diff !== 0 && excludeSet[slotMinutes]) { rejected.excluded++; continue; }

    candidates.push(slotInfo);
}

// Grid-aligned first, so the slot reserved is the one this finder would
// have picked when alignment was a hard filter. Then nearest to the
// requested time, and on a tie the earlier tee time - the order the
// single-slot search reached by scanning rows in time order and keeping
// the first strictly-closer one.
candidates.sort(function (a, b) {
    if (a.aligned !== b.aligned) return a.aligned ? -1 : 1;
    if (a.diff !== b.diff) return a.diff - b.diff;
    return (a.hours * 60 + a.minutes) - (b.hours * 60 + b.minutes);
});

return {candidates: candidates, rejected: rejected, scanned: items.length};
"""


class WaldenGolfProvider(ReservationProvider):
    """
    Selenium-based provider for booking tee times at Walden Golf / Northgate Country Club.

    The booking system uses Liferay Portal with Northstar Technologies' club management
    software. This provider automates the browser-based booking flow:
    1. Login with member credentials
    2. Navigate to tee time booking page
    3. Select course (Northgate 18)
    4. Select date and find available time slots
    5. Click Reserve on the desired time slot
    6. Confirm the booking

    Time slots are in 8-minute intervals for Northgate (e.g., 07:30, 07:38, 07:46).

    Implementation Note:
        All public async methods use asyncio.to_thread() to run blocking Selenium
        operations in a background thread. Each operation manages its own WebDriver
        lifecycle (create -> use -> quit) to avoid thread-affinity issues.
    """

    BASE_URL = "https://www.waldengolf.com"
    LOGIN_URL = f"{BASE_URL}/web/pages/login"
    DASHBOARD_URL = f"{BASE_URL}/group/pages/home"
    TEE_TIME_URL = f"{BASE_URL}/group/pages/book-a-tee-time"

    NORTHGATE_COURSE_NAME = "Northgate"
    TEE_TIME_INTERVAL_MINUTES = 8
    MAX_PLAYERS = 4  # Maximum players per tee time slot

    # Course index constants for element ID parsing
    # The Walden Golf website uses teeTimeCourses:0 for Northgate and teeTimeCourses:1 for Walden
    NORTHGATE_COURSE_INDEX = "0"
    WALDEN_COURSE_INDEX = "1"

    def __init__(self) -> None:
        """
        Initialize the WaldenGolfProvider.

        Validates that required credentials are configured. Logs a warning if
        credentials are missing - operations will fail at login time.
        """
        self.wait_strategy = WaitStrategy()
        # The tee sheet the slot finder judged from, kept from staging so a
        # morning that ends with no fallbacks can be read against the sheet that
        # produced them. Only the post-failure DOM was ever captured, and by then
        # the window has opened and other members have taken slots - which is
        # exactly the difference in question. Uploaded only if the booking fails.
        self._pre_window_sheet: str | None = None
        if not settings.walden_member_number or not settings.walden_password:
            logger.warning(
                "Walden Golf credentials not configured. "
                "Set WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD environment variables."
            )

    async def __aenter__(self) -> "WaldenGolfProvider":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    def _create_driver(self) -> webdriver.Chrome:
        """Create a headless Chrome WebDriver instance."""
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # Check for ChromeDriver path from environment variable first,
        # then fall back to ChromeDriverManager for automatic version management
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(chromedriver_path)
        else:
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
            },
        )

        return driver

    async def login(self) -> bool:
        """
        Log in to the Walden Golf member portal.

        This method creates a temporary driver, logs in, and closes it.
        It is primarily useful for testing credentials.

        Returns:
            True if login was successful, False otherwise.
        """
        return await asyncio.to_thread(self._login_sync)

    def _login_sync(self) -> bool:
        """Synchronous login implementation with full driver lifecycle."""
        driver = self._create_driver()
        try:
            return self._perform_login(driver)
        finally:
            driver.quit()

    def _perform_login(self, driver: webdriver.Chrome) -> bool:
        """
        Perform the login flow on an existing driver.

        Args:
            driver: The WebDriver instance to use

        Returns:
            True if login was successful, False otherwise.
        """
        try:
            logger.info("Navigating to login page...")
            driver.get(self.LOGIN_URL)

            wait = WebDriverWait(driver, 15)
            member_input = wait.until(
                expected_conditions.presence_of_element_located(
                    (By.NAME, DOM.LOGIN.member_input_name)
                )
            )

            password_input = driver.find_element(By.NAME, DOM.LOGIN.password_input_name)

            logger.info("Entering credentials...")
            member_input.clear()
            member_input.send_keys(settings.walden_member_number)

            password_input.clear()
            password_input.send_keys(settings.walden_password)

            submit_button = driver.find_element(By.CSS_SELECTOR, DOM.LOGIN.submit_button)
            current_url = driver.current_url
            submit_button.click()

            try:
                wait.until(expected_conditions.url_changes(current_url))
            except TimeoutException:
                pass

            if "login" not in driver.current_url.lower() or "home" in driver.current_url.lower():
                logger.info(f"Login successful. Current URL: {driver.current_url}")
                return True

            logger.error(f"Login failed. Still on URL: {driver.current_url}")
            return False

        except TimeoutException as e:
            logger.error(f"Login timeout: {e}")
            return False
        except WebDriverException as e:
            logger.error(f"Login WebDriver error: {e}")
            return False

    async def book_tee_time(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int = 32,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        """
        Book a tee time at Northgate Country Club.

        This method runs the entire booking workflow in a background thread:
        1. Creates a new WebDriver instance
        2. Logs in to the member portal
        3. Navigates to the tee time booking page
        4. Selects the Northgate course and target date
        5. Finds the requested time slot (or nearest available within fallback window)
        6. Clicks Reserve, selects player count, and confirms the booking
        7. Closes the WebDriver

        The async interface is genuinely non-blocking - all Selenium operations
        run in a dedicated thread via asyncio.to_thread().

        Args:
            target_date: The date to book (should be 7 days in advance for new bookings)
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: If exact time unavailable, try times within this window

        Returns:
            BookingResult with success status, booked time, and confirmation details
        """
        return await asyncio.to_thread(
            self._book_tee_time_sync,
            target_date,
            target_time,
            num_players,
            fallback_window_minutes,
            tee_time_interval_minutes,
        )

    def _book_tee_time_sync(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        """
        Synchronous booking implementation with full driver lifecycle.

        Creates driver, performs booking, and ensures cleanup in finally block.
        """
        # Calculate time range for logging
        target_minutes = target_time.hour * 60 + target_time.minute
        earliest_minutes = max(0, target_minutes - fallback_window_minutes)
        latest_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)
        earliest_time = time(earliest_minutes // 60, earliest_minutes % 60)
        latest_time = time(latest_minutes // 60, latest_minutes % 60)

        # An ad-hoc booking is untimed, but untimed is not the same as slow. The
        # fast chain is opt-in here (issue #124) so this path can exercise the
        # JS/direct-HTTP chain off-race; with the flag off it runs exactly the
        # Selenium flow it always has.
        use_fast_js = settings.walden_fast_booking_immediate

        logger.info(
            f"BOOKING_DEBUG: === STARTING BOOKING ATTEMPT === "
            f"date={target_date} ({target_date.strftime('%A')}), "
            f"requested_time={target_time.strftime('%H:%M')}, "
            f"time_range={earliest_time.strftime('%H:%M')}-{latest_time.strftime('%H:%M')}, "
            f"players={num_players}, fallback_window={fallback_window_minutes}min, "
            f"mode={'fast chain' if use_fast_js else 'Selenium'}"
            f"{' (direct HTTP enabled)' if use_fast_js and settings.walden_direct_http_booking else ''}"
        )
        driver = self._create_driver()
        try:
            logger.debug("BOOKING_DEBUG: Step 1/5 - Logging in to Walden Golf")
            if not self._perform_login(driver):
                logger.error("BOOKING_DEBUG: Login failed")
                return BookingResult(
                    success=False,
                    error_message="Failed to log in to Walden Golf",
                )
            logger.debug("BOOKING_DEBUG: Login successful")

            logger.debug("BOOKING_DEBUG: Step 2/5 - Navigating to tee time booking page")
            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))
            logger.debug(f"BOOKING_DEBUG: Tee time page loaded. URL: {driver.current_url}")

            logger.debug("BOOKING_DEBUG: Step 3/5 - Selecting course and date")
            if not self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME):
                logger.error("BOOKING_DEBUG: Course selection/verification failed")
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Failed to select or verify {self.NORTHGATE_COURSE_NAME} course. "
                        f"The booking may have been attempted on the wrong course. "
                        f"Please verify the course selection manually."
                    ),
                )
            if not self._select_date_sync(driver, target_date):
                logger.error("BOOKING_DEBUG: Date selection failed")
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Failed to select date {target_date.strftime('%m/%d/%Y')}. "
                        f"Cannot proceed with booking - would search wrong date."
                    ),
                )

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        DOM.SLOT_DISCOVERY.page_loaded,
                    )
                )
            )
            logger.debug("BOOKING_DEBUG: Course and date selection complete")

            logger.debug("BOOKING_DEBUG: Step 4/5 - Finding and booking time slot")
            result = self._find_and_book_time_slot_sync(
                driver,
                target_time,
                num_players,
                fallback_window_minutes,
                tee_time_interval_minutes=tee_time_interval_minutes,
                use_fast_js=use_fast_js,
                target_date=target_date,
            )

            logger.info(
                f"BOOKING_DEBUG: Step 5/5 - Booking result: success={result.success}, "
                f"booked_time={result.booked_time}, confirmation={result.confirmation_number}, "
                f"error={result.error_message}"
            )
            return result

        except TimeoutException as e:
            logger.error(f"BOOKING_DEBUG: Booking timeout exception: {e}")
            self._capture_diagnostic_info(driver, "booking_timeout")
            return BookingResult(
                success=False,
                error_message=f"Booking timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"BOOKING_DEBUG: Booking WebDriver exception: {e}")
            self._capture_diagnostic_info(driver, "booking_webdriver_error")
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )
        finally:
            logger.debug("BOOKING_DEBUG: === BOOKING ATTEMPT COMPLETE - Closing driver ===")
            driver.quit()

    async def book_multiple_tee_times(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None = None,
    ) -> BatchBookingResult:
        """
        Book multiple tee times in a single session for efficiency.

        This method is optimized for booking multiple tee times on the same date:
        1. Creates a single WebDriver session
        2. Logs in once
        3. If execute_at is provided, waits until that time before booking
        4. Books all requested times in sequence
        5. Returns results for all bookings

        Args:
            target_date: The date to book (all requests must be for this date)
            requests: List of booking requests to execute
            execute_at: Optional datetime to wait until before starting bookings.
                       If provided, the method will log in early and wait until
                       this time before refreshing the page and booking.

        Returns:
            BatchBookingResult with results for each booking request
        """
        return await asyncio.to_thread(
            self._book_multiple_tee_times_sync,
            target_date,
            requests,
            execute_at,
        )

    def _book_multiple_tee_times_sync(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None,
    ) -> BatchBookingResult:
        """
        Synchronous batch booking implementation with single driver lifecycle.

        Creates driver once, logs in once, then books all requested times in sequence.
        If execute_at is provided, waits until that time before refreshing and booking.

        Requests are sorted by target_time to process earlier times first, which helps
        avoid conflicts where a fallback slot for an earlier booking takes a slot needed
        by a later booking.
        """
        # Dropped at the door, before the early return below, so a run can never
        # flush the *previous* run's sheet. A batch that fails before it reaches
        # slot pre-location would otherwise upload whatever the last one staged
        # and label it with this morning's failure - a diagnostic describing the
        # wrong day is worse than none. Also releases the last run's member data
        # when the provider is reused.
        self._pre_window_sheet = None

        if not requests:
            return BatchBookingResult()

        # Sort requests by target_time to process earlier times first
        # This helps avoid conflicts where fallback slots overlap with later bookings
        sorted_requests = sorted(requests, key=lambda r: r.target_time)

        # Build list of all requested times and their fallback windows for conflict detection
        # Each entry is (target_time, fallback_window_minutes, booking_id)
        pending_booking_times: list[tuple[time, int, str]] = [
            (req.target_time, req.fallback_window_minutes, req.booking_id)
            for req in sorted_requests
        ]

        logger.info(
            f"BATCH_BOOKING: === STARTING BATCH BOOKING === "
            f"date={target_date} ({target_date.strftime('%A')}), "
            f"num_requests={len(sorted_requests)}, "
            f"execute_at={execute_at.strftime('%H:%M:%S') if execute_at else 'immediate'}, "
            f"sorted_times={[r.target_time.strftime('%H:%M') for r in sorted_requests]}"
        )

        results: list[BatchBookingItemResult] = []
        total_succeeded = 0
        total_failed = 0

        driver = self._create_driver()
        try:
            logger.info("BATCH_BOOKING: Step 1 - Logging in to Walden Golf")
            if not self._perform_login(driver):
                logger.error("BATCH_BOOKING: Login failed")
                for req in sorted_requests:
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message="Failed to log in to Walden Golf",
                            ),
                        )
                    )
                    total_failed += 1
                return BatchBookingResult(
                    results=results,
                    total_succeeded=total_succeeded,
                    total_failed=total_failed,
                )
            logger.info("BATCH_BOOKING: Login successful")

            logger.info("BATCH_BOOKING: Step 2 - Navigating to tee time booking page")
            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))
            logger.info(f"BATCH_BOOKING: Tee time page loaded. URL: {driver.current_url}")

            logger.info("BATCH_BOOKING: Step 3 - Selecting course")
            if not self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME):
                logger.error("BATCH_BOOKING: Course selection/verification failed")
                for req in sorted_requests:
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=(
                                    f"Failed to select or verify {self.NORTHGATE_COURSE_NAME} course."
                                ),
                            ),
                        )
                    )
                    total_failed += 1
                return BatchBookingResult(
                    results=results,
                    total_succeeded=total_succeeded,
                    total_failed=total_failed,
                )

            # Step 4 - Select date BEFORE waiting for booking window
            # Slot availability is already visible on the page before 6:30 AM.
            # We do all preparation (date selection, scrolling, slot pre-location)
            # before the wait so that at 6:30 AM the ONLY work is clicking Reserve.
            logger.info("BATCH_BOOKING: Step 4 - Selecting date")
            if not self._select_date_sync(driver, target_date):
                logger.error("BATCH_BOOKING: Date selection failed")
                for req in sorted_requests:
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=(
                                    f"Failed to select date {target_date.strftime('%m/%d/%Y')}. "
                                    f"Cannot proceed with booking - would search wrong date."
                                ),
                            ),
                        )
                    )
                    total_failed += 1
                return BatchBookingResult(
                    results=results,
                    total_succeeded=total_succeeded,
                    total_failed=total_failed,
                )

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        DOM.SLOT_DISCOVERY.page_loaded,
                    )
                )
            )
            logger.info("BATCH_BOOKING: Date selection complete")

            # Step 5 - Pre-scroll tee sheet to load all needed slot items
            max_needed_minutes = None
            for req in sorted_requests:
                req_minutes = req.target_time.hour * 60 + req.target_time.minute
                req_end_minutes = min(24 * 60 - 1, req_minutes + req.fallback_window_minutes)
                if max_needed_minutes is None or req_end_minutes > max_needed_minutes:
                    max_needed_minutes = req_end_minutes

            if max_needed_minutes is not None:
                logger.info(
                    "BATCH_BOOKING: Step 5 - Pre-scrolling tee sheet to latest needed time "
                    f"{time(max_needed_minutes // 60, max_needed_minutes % 60).strftime('%I:%M %p')}"
                )
                self._scroll_to_load_all_slots(
                    driver,
                    target_time=sorted_requests[-1].target_time,
                    fallback_window_minutes=sorted_requests[-1].fallback_window_minutes,
                    max_time_minutes_override=max_needed_minutes,
                )

            # Step 6 - Pre-locate target slots using JavaScript
            # When execute_at is set, we scan the DOM NOW (before the booking
            # window opens) so that at 6:30 AM we only need to click, not search.
            # Each slot is cached by booking_id; at click time the prelocated slot
            # is reused unless a dynamic times_to_exclude conflict invalidates it,
            # in which case a fresh DOM scan occurs as a fallback.
            #
            # Fast-ness is a setting, not a consequence of being timed. Deriving
            # it from execute_at made the fast/direct chain reachable only from
            # the 6:30 job, which meant the least-verified code could only ever
            # be exercised on the one morning that matters (issue #124).
            # Pre-location, on the other hand, is purely about beating the
            # window open, so it stays keyed to execute_at.
            use_fast_booking = settings.walden_fast_booking_batch
            prelocated_slots: dict[str, dict[str, Any]] = {}
            if use_fast_booking and execute_at is not None:
                logger.info(
                    "BATCH_BOOKING: Step 6 - Pre-locating target slots via JavaScript "
                    f"for {len(sorted_requests)} request(s)"
                )
                self._capture_pre_window_sheet(driver)
                for req in sorted_requests:
                    # Build a preliminary times_to_exclude using only the known
                    # target times of other requests (booked_times is empty here).
                    prelim_exclude: set[time] = set()
                    req_minutes = req.target_time.hour * 60 + req.target_time.minute
                    for later_time, _later_window, later_id in pending_booking_times:
                        if later_id == req.booking_id:
                            continue
                        later_minutes = later_time.hour * 60 + later_time.minute
                        if later_minutes > req_minutes:
                            prelim_exclude.add(later_time)

                    slot = self._find_target_slot_js(
                        driver,
                        req.target_time,
                        req.num_players,
                        req.fallback_window_minutes,
                        req.tee_time_interval_minutes,
                        prelim_exclude,
                    )
                    if slot is not None:
                        prelocated_slots[req.booking_id] = slot
                        slot_time = time(slot["hours"], slot["minutes"])
                        logger.info(
                            f"BATCH_BOOKING: Pre-located slot for booking_id={req.booking_id}: "
                            f"{slot_time.strftime('%I:%M %p')} (index={slot['index']}, "
                            f"exact={slot['isExact']})"
                        )
                    else:
                        logger.warning(
                            f"BATCH_BOOKING: No slot found during pre-location for "
                            f"booking_id={req.booking_id} "
                            f"(target={req.target_time.strftime('%I:%M %p')}); "
                            f"will re-scan at click time"
                        )

            # Step 7 - Calculate execute_at timestamp for timed booking
            # Instead of Python waiting then calling JS, we inject JS that self-triggers
            # at the exact timestamp. This eliminates Python→Selenium→JS handoff latency.
            execute_at_timestamp_ms: int | None = None
            # The club's stated window, kept unshifted. Every offset in the race
            # ledger is measured from here, so the numbers stay on one scale
            # across the aim moving - ten mornings of evidence are in this frame.
            window_timestamp_ms: int | None = None
            if execute_at:
                # Normalize to naive CT first (execute_at may be aware or naive),
                # then convert via relative delta to avoid host-timezone interpretation.
                # This mirrors the approach in _precision_wait_until.
                execute_at_ct = CTDateTime.to_naive_ct(execute_at)
                now_ct = CTDateTime.to_naive_ct(CTDateTime.now())
                delay_ms = max(0, int((execute_at_ct - now_ct).total_seconds() * 1000))
                window_timestamp_ms = int(time_module.time() * 1000) + delay_ms
                # The sheet does not open when the club says it does. Applied
                # here rather than inside the direct booker so both chains agree
                # on when the window opens - the JS chain reads the same target
                # and would otherwise click into the one arrival that has never
                # been granted.
                aim_offset_ms = max(
                    0,
                    settings.walden_window_opens_offset_ms + settings.walden_reserve_aim_margin_ms,
                )
                execute_at_timestamp_ms = window_timestamp_ms + aim_offset_ms
                logger.info(
                    f"BATCH_BOOKING: Step 7 - Timed booking mode, "
                    f"target timestamp: {execute_at_timestamp_ms} "
                    f"({execute_at_ct.strftime('%H:%M:%S.%f')} CT +{aim_offset_ms}ms, "
                    f"delay={delay_ms + aim_offset_ms}ms)"
                )
                if aim_offset_ms:
                    logger.info(
                        "BATCH_BOOKING: Aiming %dms past the stated window - the club opens "
                        "at +%dms and %dms is margin on the clock probe. Ledger offsets "
                        "stay measured from the stated window.",
                        aim_offset_ms,
                        settings.walden_window_opens_offset_ms,
                        settings.walden_reserve_aim_margin_ms,
                    )

                if not use_fast_booking:
                    # The 6:30 gate lives inside the fast chain - the JS
                    # precision wait, or the direct booker's. The Selenium flow
                    # ignores execute_at_timestamp_ms entirely, so with the fast
                    # chain switched off there is nothing left holding the
                    # batch back and it would race a still-locked tee sheet.
                    # Wait in Python instead: coarser, but a kill switch that
                    # silently drops the window gate is worse than a slow one.
                    #
                    # Shifted by the same offset as the other two chains. This
                    # path reads execute_at rather than execute_at_timestamp_ms,
                    # so without this it would be the one route still firing at
                    # the stated window - the arrival the offset exists to avoid,
                    # and a silent disagreement about when the window opens
                    # sitting behind a kill switch nobody exercises.
                    logger.info(
                        "BATCH_BOOKING: Fast chain disabled - waiting for the booking "
                        "window (+%dms) in Python before the Selenium flow",
                        aim_offset_ms,
                    )
                    self._precision_wait_until(execute_at + timedelta(milliseconds=aim_offset_ms))

            # Track times that have been successfully booked to avoid conflicts
            # When a booking succeeds, we add its booked_time to this set
            booked_times: set[time] = set()

            logger.info(
                f"BATCH_BOOKING: Step 8 - Booking {len(sorted_requests)} tee times"
                f"{f' (timed JS mode, {len(prelocated_slots)} pre-located)' if use_fast_booking and execute_at_timestamp_ms else ''}"
                f"{f' (fast JS mode, {len(prelocated_slots)} pre-located)' if use_fast_booking and not execute_at_timestamp_ms else ''}"
                f"{' (Selenium mode, fast chain disabled)' if not use_fast_booking else ''}"
            )
            for i, req in enumerate(sorted_requests, 1):
                # Calculate times to exclude: times already booked + times needed by later bookings
                # This prevents a fallback slot from taking a time needed by a later booking
                times_to_exclude = booked_times.copy()

                # Add times that are within the fallback window of later bookings
                for later_time, later_window, later_id in pending_booking_times:
                    if later_id == req.booking_id:
                        continue  # Skip current booking
                    # Check if this later booking's target time could conflict
                    later_minutes = later_time.hour * 60 + later_time.minute
                    current_minutes = req.target_time.hour * 60 + req.target_time.minute
                    # Only protect times for bookings that haven't been processed yet
                    if later_minutes > current_minutes:
                        times_to_exclude.add(later_time)

                # Every booking carries the target timestamp, not just the first.
                # Giving it only to booking 1 made the whole batch's 6:30 gate
                # depend on booking 1 getting far enough to reach its wait: when
                # it failed early, the rest raced against a still-locked tee
                # sheet. Handing the timestamp to all of them costs nothing once
                # the window is genuinely open, because the JS precision wait
                # no-ops on a target already in the past.
                use_timed_for_this_booking = execute_at_timestamp_ms

                logger.info(
                    f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} - "
                    f"time={req.target_time.strftime('%H:%M')}, "
                    f"players={req.num_players}, booking_id={req.booking_id}, "
                    f"timed={use_timed_for_this_booking is not None}, "
                    f"excluding_times={[t.strftime('%H:%M') for t in sorted(times_to_exclude)]}"
                )

                try:
                    result = self._find_and_book_time_slot_sync(
                        driver,
                        req.target_time,
                        req.num_players,
                        req.fallback_window_minutes,
                        times_to_exclude=times_to_exclude,
                        tee_time_interval_minutes=req.tee_time_interval_minutes,
                        skip_scroll=True,
                        use_fast_js=use_fast_booking,
                        prelocated_slot=prelocated_slots.get(req.booking_id),
                        execute_at_timestamp_ms=use_timed_for_this_booking,
                        window_timestamp_ms=window_timestamp_ms,
                        target_date=target_date,
                    )

                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=result,
                        )
                    )

                    if result.success:
                        total_succeeded += 1
                        # Track the booked time to avoid conflicts with later bookings
                        if result.booked_time:
                            booked_times.add(result.booked_time)
                        logger.info(
                            f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} SUCCESS - "
                            f"booked_time={result.booked_time}, "
                            f"confirmation={result.confirmation_number}"
                        )
                    else:
                        total_failed += 1
                        logger.warning(
                            f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} FAILED - "
                            f"error={result.error_message}"
                        )

                    if i < len(sorted_requests):
                        logger.info(
                            "BATCH_BOOKING: Navigating back to tee time page for next booking"
                        )
                        driver.get(self.TEE_TIME_URL)
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (By.CSS_SELECTOR, "form")
                            )
                        )
                        if not self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME):
                            logger.warning("BATCH_BOOKING: Course re-selection failed")
                        if not self._select_date_sync(driver, target_date):
                            logger.error("BATCH_BOOKING: Date re-selection failed for next booking")
                            # Continue with remaining bookings but they will likely fail
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (
                                    By.CSS_SELECTOR,
                                    DOM.SLOT_DISCOVERY.page_loaded,
                                )
                            )
                        )

                        remaining_needed_minutes = None
                        for remaining_req in sorted_requests[i:]:
                            remaining_minutes = (
                                remaining_req.target_time.hour * 60
                                + remaining_req.target_time.minute
                            )
                            remaining_end_minutes = min(
                                24 * 60 - 1,
                                remaining_minutes + remaining_req.fallback_window_minutes,
                            )
                            if (
                                remaining_needed_minutes is None
                                or remaining_end_minutes > remaining_needed_minutes
                            ):
                                remaining_needed_minutes = remaining_end_minutes

                        if remaining_needed_minutes is not None:
                            logger.info(
                                "BATCH_BOOKING: Pre-scrolling tee sheet for remaining bookings to "
                                f"{time(remaining_needed_minutes // 60, remaining_needed_minutes % 60).strftime('%I:%M %p')}"
                            )
                            self._scroll_to_load_all_slots(
                                driver,
                                target_time=sorted_requests[-1].target_time,
                                fallback_window_minutes=sorted_requests[-1].fallback_window_minutes,
                                max_time_minutes_override=remaining_needed_minutes,
                            )

                except Exception as e:
                    logger.error(f"BATCH_BOOKING: Booking {i}/{len(sorted_requests)} ERROR - {e}")
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=f"Booking error: {str(e)}",
                            ),
                        )
                    )
                    total_failed += 1

            logger.info(
                f"BATCH_BOOKING: === BATCH COMPLETE === "
                f"succeeded={total_succeeded}, failed={total_failed}"
            )

            return BatchBookingResult(
                results=results,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
            )

        except TimeoutException as e:
            logger.error(f"BATCH_BOOKING: Timeout exception: {e}")
            self._capture_diagnostic_info(driver, "batch_booking_timeout")
            for req in sorted_requests:
                if not any(r.booking_id == req.booking_id for r in results):
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=f"Batch booking timeout: {str(e)}",
                            ),
                        )
                    )
                    total_failed += 1
            return BatchBookingResult(
                results=results,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
            )
        except WebDriverException as e:
            logger.error(f"BATCH_BOOKING: WebDriver exception: {e}")
            self._capture_diagnostic_info(driver, "batch_booking_webdriver_error")
            for req in sorted_requests:
                if not any(r.booking_id == req.booking_id for r in results):
                    results.append(
                        BatchBookingItemResult(
                            booking_id=req.booking_id,
                            result=BookingResult(
                                success=False,
                                error_message=f"Batch booking error: {str(e)}",
                            ),
                        )
                    )
                    total_failed += 1
            return BatchBookingResult(
                results=results,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
            )
        finally:
            logger.info("BATCH_BOOKING: === BATCH BOOKING COMPLETE - Closing driver ===")
            driver.quit()

    def _select_course_sync(self, driver: webdriver.Chrome, course_name: str) -> bool:
        """
        Select the course from the multi-select checkbox dropdown.

        The Walden Golf tee time page uses a multi-select dropdown with checkboxes
        for course selection. By default, both Northgate and Walden on Lake Conroe
        are selected, showing tee times for both courses in separate columns.

        To prevent accidental bookings at the wrong course, this method:
        1. Opens the course selection dropdown
        2. Ensures the target course (Northgate) is checked
        3. Unchecks other courses (Walden on Lake Conroe) to show only Northgate times
        4. Closes the dropdown and verifies the selection

        Args:
            driver: The WebDriver instance
            course_name: The name of the course to select (e.g., "Northgate")

        Returns:
            True if the correct course is selected/verified, False otherwise
        """
        walden_course_name = "Walden on Lake Conroe"

        try:
            if self._select_course_via_checkbox_dropdown(driver, course_name, walden_course_name):
                logger.info(f"Successfully configured course selection for {course_name} only")
            else:
                if self._select_course_via_standard_dropdown(driver, course_name):
                    logger.info(f"Selected course via standard dropdown: {course_name}")
                else:
                    logger.warning(
                        f"No course dropdown found - attempting to verify "
                        f"current course is {course_name}"
                    )
        except Exception as e:
            logger.warning(f"Error during course selection: {e}")

        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

        if self._verify_course_selection(driver, course_name):
            logger.info(f"Verified: Currently on {course_name} course page")
            return True
        else:
            logger.error(
                f"BOOKING_DEBUG: Failed to verify {course_name} course selection. "
                f"May be on wrong course page."
            )
            return False

    def _select_course_via_checkbox_dropdown(
        self, driver: webdriver.Chrome, target_course: str, course_to_deselect: str
    ) -> bool:
        """
        Handle multi-select checkbox dropdown for course selection.

        The Walden Golf site uses a custom dropdown with checkboxes where multiple
        courses can be selected simultaneously. This method ensures only the target
        course is selected by checking it and unchecking others.

        Args:
            driver: The WebDriver instance
            target_course: Course to ensure is checked (e.g., "Northgate")
            course_to_deselect: Course to uncheck (e.g., "Walden on Lake Conroe")

        Returns:
            True if checkbox dropdown was found and configured, False otherwise
        """
        try:
            dropdown_trigger_selectors = [
                "[class*='select'][class*='course']",
                "div[class*='multiselect']",
                "button[class*='dropdown']",
                ".course-dropdown",
                "[aria-label*='course' i]",
                "[placeholder*='course' i]",
            ]

            dropdown_trigger = None
            for selector in dropdown_trigger_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        if elem.is_displayed():
                            dropdown_trigger = elem
                            break
                    if dropdown_trigger:
                        break
                except NoSuchElementException:
                    continue

            if not dropdown_trigger:
                try:
                    dropdown_trigger = driver.find_element(
                        By.XPATH,
                        "//*[contains(text(), 'Select Course') or contains(text(), 'Course')]"
                        "[contains(@class, 'select') or contains(@class, 'dropdown') or "
                        "self::button or self::div[contains(@class, 'trigger')]]",
                    )
                except NoSuchElementException:
                    pass

            if not dropdown_trigger:
                logger.debug("No checkbox dropdown trigger found for course selection")
                return False

            dropdown_trigger.click()
            logger.info("Opened course selection dropdown")
            self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

            checkbox_items = driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='checkbox'], "
                "li[class*='option'], "
                "div[class*='option'], "
                "label[class*='checkbox']",
            )

            if not checkbox_items:
                checkbox_items = driver.find_elements(
                    By.XPATH,
                    "//li[.//input[@type='checkbox']] | "
                    "//div[contains(@class, 'option')] | "
                    "//label[contains(@class, 'check')]",
                )

            target_found = False
            deselect_found = False

            for item in checkbox_items:
                item_text = item.text.lower() if item.text else ""
                if not item_text:
                    try:
                        item_text = (item.get_attribute("textContent") or "").lower()
                    except Exception:
                        continue

                if target_course.lower() in item_text:
                    target_found = True
                    checkbox = self._find_checkbox_in_element(driver, item, target_course)
                    if checkbox and not checkbox.is_selected():
                        self._click_checkbox_or_label(driver, item, checkbox)
                        logger.info(f"Checked '{target_course}' in course dropdown")
                    elif checkbox and checkbox.is_selected():
                        logger.info(f"'{target_course}' already checked")

                elif course_to_deselect.lower() in item_text:
                    deselect_found = True
                    checkbox = self._find_checkbox_in_element(driver, item, course_to_deselect)
                    if checkbox and checkbox.is_selected():
                        self._click_checkbox_or_label(driver, item, checkbox)
                        logger.info(f"Unchecked '{course_to_deselect}' in course dropdown")
                    elif checkbox and not checkbox.is_selected():
                        logger.info(f"'{course_to_deselect}' already unchecked")

            try:
                close_button = driver.find_element(
                    By.CSS_SELECTOR, "[class*='close'], .x, button[aria-label='close']"
                )
                close_button.click()
            except NoSuchElementException:
                try:
                    dropdown_trigger.click()
                except Exception:
                    driver.find_element(By.TAG_NAME, "body").click()

            self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

            if target_found:
                logger.info(
                    f"Course dropdown configured: {target_course}=checked, "
                    f"{course_to_deselect}={'unchecked' if deselect_found else 'not found'}"
                )
                return True

            logger.warning(f"Target course '{target_course}' not found in dropdown options")
            return False

        except Exception as e:
            logger.debug(f"Checkbox dropdown selection failed: {e}")
            return False

    def _find_checkbox_in_element(
        self, driver: webdriver.Chrome, container: Any, course_name: str
    ) -> Any | None:
        """Find the checkbox input within a container element."""
        try:
            return container.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
        except NoSuchElementException:
            pass

        try:
            return container.find_element(By.TAG_NAME, "input")
        except NoSuchElementException:
            pass

        try:
            return driver.find_element(
                By.XPATH,
                f"//input[@type='checkbox'][following-sibling::*[contains(text(), '{course_name}')] "
                f"or preceding-sibling::*[contains(text(), '{course_name}')]]",
            )
        except NoSuchElementException:
            pass

        return None

    def _get_visible_page_text(self, driver: webdriver.Chrome) -> str:
        """Get visible text from the page (prefer <body>.text over raw HTML source)."""
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body_text = getattr(body, "text", "")
            if isinstance(body_text, str) and body_text.strip():
                return body_text
        except Exception:
            pass

        page_source = getattr(driver, "page_source", "")
        return page_source if isinstance(page_source, str) else ""

    def _container_message_text(self, element: Any) -> str:
        """Read one message container the way the direct-HTTP path reads one.

        ``WebElement.text`` returns everything the container renders, including
        its own controls, so the club's refusal dialog would reach the member as
        "... restricted for 1 round(s) on Northgate per Day Ok". The markup goes
        through the same pruner the HTTP path uses instead - one definition of
        message text for both paths, and the one already tested against a real
        captured response.

        Falls back to ``.text`` if the markup cannot be read: a message with a
        stray button label in it beats no message at all.
        """
        try:
            markup = element.get_attribute("outerHTML")
            if isinstance(markup, str) and markup:
                pruned = container_message_text(markup)
                if pruned:
                    return pruned
        except Exception as e:  # noqa: BLE001 - diagnostics must not raise
            logger.debug(f"Could not read container markup, falling back to .text: {e}")

        return (getattr(element, "text", "") or "").strip()

    def _extract_booking_error_message(self, driver: webdriver.Chrome) -> str | None:
        """Extract user-visible booking error text from common alert/message containers."""
        selectors = DOM.ERROR_MESSAGES.containers

        try:
            messages: list[str] = []
            for sel in selectors:
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel)[:10]:
                        try:
                            if (el.get_attribute("aria-hidden") or "").lower() == "true":
                                continue
                        except Exception:
                            pass

                        try:
                            if not el.is_displayed():
                                continue
                        except Exception:
                            pass

                        text = self._container_message_text(el)
                        if text:
                            messages.append(text)
                except Exception:
                    continue

            if messages:
                unique: list[str] = []
                for msg in messages:
                    if msg not in unique:
                        unique.append(msg)
                joined = " | ".join(unique)
                return joined[:500]
        except Exception:
            pass

        # Fallback: provide a short snippet of visible text if it contains likely failure words.
        visible_text = self._get_visible_page_text(driver)
        visible_lower = visible_text.lower()
        if any(word in visible_lower for word in ("error", "unable", "failed", "unavailable")):
            snippet = " ".join(visible_text.split())
            return snippet[:500]

        return None

    def _click_checkbox_or_label(
        self, driver: webdriver.Chrome, container: Any, checkbox: Any
    ) -> None:
        """Click the checkbox or its label to toggle selection."""
        try:
            checkbox.click()
            return
        except Exception:
            pass

        try:
            label = container.find_element(By.TAG_NAME, "label")
            label.click()
            return
        except Exception:
            pass

        try:
            container.click()
            return
        except Exception:
            pass

        try:
            driver.execute_script("arguments[0].click();", checkbox)
        except Exception as e:
            logger.warning(f"Failed to click checkbox: {e}")

    def _select_course_via_standard_dropdown(
        self, driver: webdriver.Chrome, course_name: str
    ) -> bool:
        """
        Fallback: Select course using standard HTML select dropdown.

        Args:
            driver: The WebDriver instance
            course_name: The name of the course to select

        Returns:
            True if course was selected, False otherwise
        """
        course_dropdown_selectors = [
            "select[id*='course']",
            "select[name*='course']",
            "select[id*='Course']",
            "select[name*='Course']",
            "select.course-select",
            "#courseSelect",
        ]

        for selector in course_dropdown_selectors:
            try:
                course_select = driver.find_element(By.CSS_SELECTOR, selector)
                select = Select(course_select)

                for option in select.options:
                    if course_name.lower() in option.text.lower():
                        select.select_by_visible_text(option.text)
                        logger.info(f"Selected course: {option.text} using selector: {selector}")
                        wait = WebDriverWait(driver, 10)
                        try:
                            wait.until(expected_conditions.staleness_of(course_select))
                        except TimeoutException:
                            pass
                        return True

            except NoSuchElementException:
                continue

        return False

    def _verify_course_selection(self, driver: webdriver.Chrome, course_name: str) -> bool:
        """
        Verify that the correct course is currently selected/displayed.

        Checks multiple indicators on the page to confirm we're viewing
        the correct course's tee times.

        Args:
            driver: The WebDriver instance
            course_name: The expected course name (e.g., "Northgate")

        Returns:
            True if the correct course is verified, False otherwise
        """
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            course_name_lower = course_name.lower()

            if course_name_lower in page_text:
                logger.debug(f"Found '{course_name}' in page text")
                return True

            course_indicators = [
                f"h1:contains('{course_name}')",
                f"h2:contains('{course_name}')",
                f".course-name:contains('{course_name}')",
                f"[class*='course']:contains('{course_name}')",
            ]

            for indicator in course_indicators:
                try:
                    elements = driver.find_elements(
                        By.XPATH,
                        f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                        f"'abcdefghijklmnopqrstuvwxyz'), '{course_name_lower}')]",
                    )
                    if elements:
                        logger.debug(f"Found course indicator element for '{course_name}'")
                        return True
                except NoSuchElementException:
                    continue

            try:
                selected_options = driver.find_elements(
                    By.CSS_SELECTOR, "select option:checked, select option[selected]"
                )
                for option in selected_options:
                    if course_name_lower in option.text.lower():
                        logger.debug(f"Found '{course_name}' in selected dropdown option")
                        return True
            except NoSuchElementException:
                pass

            logger.warning(
                f"Could not verify course '{course_name}' on page. "
                f"Page may be on a different course."
            )
            return False

        except Exception as e:
            logger.error(f"Error verifying course selection: {e}")
            return False

    def _select_date_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select the target date using various date selection mechanisms.

        The Northstar Technologies tee sheet may use different date selection methods:
        1. Date input field (various selectors)
        2. Date picker widget
        3. Day-of-week tabs
        4. Calendar navigation

        This method tries multiple approaches in order of likelihood.

        Returns:
            True if date was successfully selected, False otherwise.
        """
        day_name = target_date.strftime("%A")
        date_str = target_date.strftime("%m/%d/%Y")
        date_str_alt = target_date.strftime("%Y-%m-%d")
        logger.info(f"BOOKING_DEBUG: Selecting date {target_date} ({day_name})")

        date_input_selectors = [
            "input[type='text'][id*='date']",
            "input[type='date']",
            "input[id*='date']",
            "input[name*='date']",
            "input[class*='date']",
            "input[placeholder*='date' i]",
            "input[placeholder*='mm/dd' i]",
            ".datepicker input",
            "[data-date] input",
        ]

        for selector in date_input_selectors:
            try:
                date_input = driver.find_element(By.CSS_SELECTOR, selector)
                input_type = date_input.get_attribute("type")

                date_input.clear()
                if input_type == "date":
                    date_input.send_keys(date_str_alt)
                else:
                    date_input.send_keys(date_str)
                logger.info(f"BOOKING_DEBUG: Entered date {date_str} using selector: {selector}")

                wait = WebDriverWait(driver, 5)
                try:
                    search_button = wait.until(
                        expected_conditions.element_to_be_clickable(
                            (
                                By.CSS_SELECTOR,
                                "button[type='submit'], input[type='submit'], button.search, .btn-search",
                            )
                        )
                    )
                    search_button.click()
                    logger.info("BOOKING_DEBUG: Clicked search/submit button after date entry")
                except TimeoutException:
                    pass

                return True

            except NoSuchElementException:
                continue

        # Skip day tab lookup - go directly to calendar picker for faster date selection
        logger.info("BOOKING_DEBUG: No date input found, using calendar picker...")
        if self._select_date_via_calendar_sync(driver, target_date):
            logger.info("BOOKING_DEBUG: Date selection via calendar successful")
            return True
        else:
            logger.error(
                f"BOOKING_DEBUG: Calendar date selection failed for {target_date}. "
                f"Cannot proceed with booking on wrong date."
            )
            return False

    def _select_date_via_calendar_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select date using a calendar picker widget if available.

        Handles month navigation when the target date is in a different month than
        the currently displayed month. Uses the month/year dropdowns or navigation
        arrows to reach the correct month before selecting the day.

        Returns:
            True if date was selected successfully, False otherwise.
        """
        try:
            calendar_triggers = driver.find_elements(
                By.CSS_SELECTOR,
                ".calendar-trigger, .datepicker-trigger, [class*='calendar'], "
                "button[aria-label*='calendar' i], .ui-datepicker-trigger, "
                "span.icon-calendar, i.fa-calendar",
            )

            if calendar_triggers:
                calendar_triggers[0].click()
                logger.info("BOOKING_DEBUG: Clicked calendar trigger")

                wait = WebDriverWait(driver, 5)
                try:
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                ".ui-datepicker, .datepicker, [class*='calendar-popup'], "
                                ".ui-datepicker-calendar, select[class*='month'], select[class*='year']",
                            )
                        )
                    )
                    logger.info("BOOKING_DEBUG: Calendar popup appeared")

                    # Navigate to the correct month/year if needed
                    if not self._navigate_calendar_to_month(driver, target_date):
                        logger.warning(
                            f"BOOKING_DEBUG: Failed to navigate calendar to {target_date.strftime('%B %Y')}"
                        )
                        return False

                    # Now select the day
                    day_str = str(target_date.day)
                    day_elements = driver.find_elements(
                        By.XPATH,
                        f"//td[@data-date='{target_date.day}'] | "
                        f"//a[text()='{day_str}'] | "
                        f"//td[contains(@class, 'day') and text()='{day_str}'] | "
                        f"//td[normalize-space(text())='{day_str}']",
                    )

                    logger.info(
                        f"BOOKING_DEBUG: Found {len(day_elements)} day elements for day {day_str}"
                    )

                    for day_el in day_elements:
                        if day_el.is_displayed() and day_el.is_enabled():
                            # Avoid clicking on days from adjacent months (often grayed out)
                            day_class = day_el.get_attribute("class") or ""
                            if "ui-datepicker-other-month" in day_class or "disabled" in day_class:
                                logger.debug(
                                    f"BOOKING_DEBUG: Skipping day element with class: {day_class}"
                                )
                                continue

                            day_el.click()
                            logger.info(
                                f"BOOKING_DEBUG: Selected day {day_str} from calendar for date {target_date}"
                            )
                            # Wait for page to reload after date selection
                            self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)
                            # Wait for tee time slots to appear
                            try:
                                WebDriverWait(driver, 10).until(
                                    expected_conditions.presence_of_element_located(
                                        (
                                            By.CSS_SELECTOR,
                                            DOM.DATE_SELECTION.tee_time_presence,
                                        )
                                    )
                                )
                            except TimeoutException:
                                logger.debug(
                                    "BOOKING_DEBUG: Tee time slots not found after calendar selection"
                                )
                            return True

                    logger.warning(
                        f"BOOKING_DEBUG: No clickable day element found for day {day_str}"
                    )

                except TimeoutException:
                    logger.warning("BOOKING_DEBUG: Calendar popup did not appear")

        except Exception as e:
            logger.warning(f"BOOKING_DEBUG: Calendar selection failed: {e}")

        return False

    def _navigate_calendar_to_month(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Navigate the calendar to the correct month and year.

        Tries multiple strategies:
        1. Use month/year dropdown selects if available
        2. Use next/prev navigation arrows

        Args:
            driver: The WebDriver instance
            target_date: The target date to navigate to

        Returns:
            True if navigation succeeded or no navigation needed, False otherwise
        """
        target_month = target_date.month
        target_year = target_date.year
        target_month_name = target_date.strftime("%B")  # e.g., "February"
        target_month_abbr = target_date.strftime("%b")  # e.g., "Feb"

        logger.info(f"BOOKING_DEBUG: Navigating calendar to {target_month_name} {target_year}")

        # Strategy 1: Try month/year dropdown selects
        try:
            # Look for month dropdown - try various selectors
            month_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-month, select[class*='month'], "
                "select[data-handler='selectMonth'], select[name*='month']",
            )
            year_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-year, select[class*='year'], "
                "select[data-handler='selectYear'], select[name*='year']",
            )

            if month_selects and year_selects:
                logger.info("BOOKING_DEBUG: Found month/year dropdowns, using select strategy")

                # Select year first
                year_select = Select(year_selects[0])
                try:
                    year_select.select_by_value(str(target_year))
                    logger.info(f"BOOKING_DEBUG: Selected year {target_year} from dropdown")
                except Exception:
                    try:
                        year_select.select_by_visible_text(str(target_year))
                        logger.info(f"BOOKING_DEBUG: Selected year {target_year} by text")
                    except Exception as e:
                        logger.warning(f"BOOKING_DEBUG: Could not select year: {e}")

                self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)

                # Select month (0-indexed in some implementations, 1-indexed in others)
                month_select = Select(month_selects[0])
                try:
                    # Try 0-indexed first (JavaScript Date style)
                    month_select.select_by_value(str(target_month - 1))
                    logger.info(
                        f"BOOKING_DEBUG: Selected month {target_month_name} (value={target_month - 1})"
                    )
                except Exception:
                    try:
                        # Try 1-indexed
                        month_select.select_by_value(str(target_month))
                        logger.info(
                            f"BOOKING_DEBUG: Selected month {target_month_name} (value={target_month})"
                        )
                    except Exception:
                        try:
                            # Try by visible text
                            month_select.select_by_visible_text(target_month_name)
                            logger.info(
                                f"BOOKING_DEBUG: Selected month {target_month_name} by text"
                            )
                        except Exception:
                            try:
                                month_select.select_by_visible_text(target_month_abbr)
                                logger.info(
                                    f"BOOKING_DEBUG: Selected month {target_month_abbr} by abbr"
                                )
                            except Exception as e:
                                logger.warning(f"BOOKING_DEBUG: Could not select month: {e}")

                self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.2)
                return True

        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Dropdown strategy failed: {e}")

        # Strategy 2: Use navigation arrows to move month by month
        try:
            # Determine current month/year displayed
            current_month, current_year = self._get_calendar_current_month(driver)

            if current_month is None or current_year is None:
                logger.warning("BOOKING_DEBUG: Could not determine current calendar month")
                # Assume we need to navigate - try clicking next
                current_month = datetime.now().month
                current_year = datetime.now().year

            logger.info(
                f"BOOKING_DEBUG: Calendar currently showing {current_month}/{current_year}, "
                f"need {target_month}/{target_year}"
            )

            # Calculate months to navigate
            months_diff = (target_year - current_year) * 12 + (target_month - current_month)

            if months_diff == 0:
                logger.info("BOOKING_DEBUG: Already on correct month")
                return True

            # Find navigation buttons
            if months_diff > 0:
                # Need to go forward
                nav_selectors = [
                    "a.ui-datepicker-next",
                    "button.ui-datepicker-next",
                    "[data-handler='next']",
                    ".ui-datepicker-next",
                    "a[title='Next']",
                    "button[title='Next']",
                    "span.ui-icon-circle-triangle-e",
                    "[class*='next']",
                    "a[class*='next']",
                    "button[class*='next']",
                ]
                direction = "next"
            else:
                # Need to go backward
                nav_selectors = [
                    "a.ui-datepicker-prev",
                    "button.ui-datepicker-prev",
                    "[data-handler='prev']",
                    ".ui-datepicker-prev",
                    "a[title='Prev']",
                    "button[title='Prev']",
                    "span.ui-icon-circle-triangle-w",
                    "[class*='prev']",
                    "a[class*='prev']",
                    "button[class*='prev']",
                ]
                direction = "prev"
                months_diff = abs(months_diff)

            nav_button = None
            for selector in nav_selectors:
                try:
                    buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                    for btn in buttons:
                        if btn.is_displayed() and btn.is_enabled():
                            nav_button = btn
                            logger.info(
                                f"BOOKING_DEBUG: Found {direction} nav button with selector: {selector}"
                            )
                            break
                    if nav_button:
                        break
                except Exception:
                    continue

            if not nav_button:
                logger.warning(f"BOOKING_DEBUG: Could not find {direction} navigation button")
                return False

            # Click navigation button for each month we need to move
            for i in range(months_diff):
                try:
                    # Re-find the button each time as DOM may update
                    for selector in nav_selectors:
                        try:
                            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                            for btn in buttons:
                                if btn.is_displayed() and btn.is_enabled():
                                    nav_button = btn
                                    break
                            if nav_button:
                                break
                        except Exception:
                            continue

                    nav_button.click()
                    logger.debug(
                        f"BOOKING_DEBUG: Clicked {direction} button ({i + 1}/{months_diff})"
                    )
                    self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)
                except Exception as e:
                    logger.warning(f"BOOKING_DEBUG: Error clicking nav button: {e}")
                    return False

            logger.info(
                f"BOOKING_DEBUG: Navigated {months_diff} months {direction} to reach "
                f"{target_month_name} {target_year}"
            )
            return True

        except Exception as e:
            logger.warning(f"BOOKING_DEBUG: Navigation arrow strategy failed: {e}")

        return False

    def _get_calendar_current_month(
        self, driver: webdriver.Chrome
    ) -> tuple[int | None, int | None]:
        """
        Determine the currently displayed month and year in the calendar.

        Returns:
            Tuple of (month, year) as integers, or (None, None) if cannot determine
        """
        try:
            # Try to read from month/year dropdowns
            month_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-month, select[class*='month']",
            )
            year_selects = driver.find_elements(
                By.CSS_SELECTOR,
                "select.ui-datepicker-year, select[class*='year']",
            )

            if month_selects and year_selects:
                month_select = Select(month_selects[0])
                year_select = Select(year_selects[0])

                # Get selected values
                selected_month = month_select.first_selected_option
                selected_year = year_select.first_selected_option

                month_val = selected_month.get_attribute("value")
                year_val = selected_year.get_attribute("value")

                if month_val is not None and year_val is not None:
                    # Month might be 0-indexed
                    month_int = int(month_val)
                    if month_int < 12:  # Likely 0-indexed
                        month_int += 1
                    return month_int, int(year_val)

            # Try to read from header text (e.g., "January 2026" or "Jan 2026")
            header_selectors = [
                ".ui-datepicker-title",
                ".datepicker-title",
                "[class*='calendar-header']",
                "[class*='datepicker-header']",
            ]

            for selector in header_selectors:
                try:
                    headers = driver.find_elements(By.CSS_SELECTOR, selector)
                    for header in headers:
                        text = header.text.strip()
                        if text:
                            # Try to parse "January 2026" or "Jan 2026"
                            for fmt in ["%B %Y", "%b %Y"]:
                                try:
                                    parsed = datetime.strptime(text, fmt)
                                    return parsed.month, parsed.year
                                except ValueError:
                                    continue
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Error getting current calendar month: {e}")

        return None, None

    def _select_date_via_tabs_sync(self, driver: webdriver.Chrome, target_date: date) -> bool:
        """
        Select date using the day-of-week tabs if date picker not available.

        Returns:
            True if date was selected successfully, False otherwise.
        """
        day_name = target_date.strftime("%A")
        date_str = target_date.strftime("%m/%d")
        logger.debug(f"BOOKING_DEBUG: Looking for day tab for {day_name} ({date_str})")

        try:
            day_tabs = driver.find_elements(
                By.CSS_SELECTOR,
                ".day-tab, [class*='day-tab'], a[href*='day'], "
                "[data-day], .teetime-day-tab, .nav-tabs a",
            )
            logger.debug(f"BOOKING_DEBUG: Found {len(day_tabs)} potential day tabs")

            for i, tab in enumerate(day_tabs):
                tab_text = tab.text.lower()
                logger.debug(f"BOOKING_DEBUG: Tab {i}: text='{tab_text}'")
                if day_name.lower() in tab_text or date_str in tab.text:
                    wait = WebDriverWait(driver, 10)
                    try:
                        wait.until(expected_conditions.element_to_be_clickable(tab))
                        tab.click()
                        logger.debug(f"BOOKING_DEBUG: Clicked day tab: {day_name}")
                        wait.until(expected_conditions.staleness_of(tab))
                    except TimeoutException:
                        tab.click()
                        logger.info(
                            f"BOOKING_DEBUG: Clicked day tab (no staleness wait): {day_name}"
                        )
                    return True

            logger.info(
                f"BOOKING_DEBUG: Could not find day tab for {day_name}. Available tabs: {[t.text for t in day_tabs[:5]]}"
            )
            return False

        except NoSuchElementException:
            logger.info("BOOKING_DEBUG: No day tabs found on page")
            return False

    def _select_player_count_sync(
        self,
        driver: webdriver.Chrome,
        num_players: int,
        search_context: Any = None,
    ) -> bool:
        """
        Select the number of players in the booking dialog.

        The player count selector on Walden Golf is a button group (ui-selectonebutton)
        with buttons for 1, 2, 3, 4 players. This method clicks the appropriate button.

        IMPORTANT: search_context should be the booking modal element when available,
        NOT the full page driver. The generic .ui-selectonebutton class is shared by
        the time period filter on the main page (values 0-3) and the player count
        button group in the modal (values 1-4). Searching the full page matches the
        wrong element. See Issue #105.

        Args:
            driver: The WebDriver instance (needed for execute_script calls)
            num_players: Number of players (1-4)
            search_context: Element to search within (modal element or driver).
                           Defaults to driver if not provided.

        Returns:
            True if player count was successfully selected, False otherwise
        """
        if search_context is None:
            search_context = driver

        try:
            logger.debug(
                f"BOOKING_DEBUG: Starting player count selection for {num_players} players"
            )
            # Wait for the player count button group to appear within the search context
            self.wait_strategy.wait_for_element(
                search_context,
                (By.CSS_SELECTOR, ", ".join(DOM.PLAYER_COUNT.button_group)),
                fixed_duration=1.0,
                timeout=5.0,
            )

            # The Walden Golf site uses a button group with class "reservation-players"
            # Each button contains a radio input with value 1, 2, 3, or 4
            # The button div has class "ui-button" and we need to click the one with the correct value

            # First try to find the button group (scoped to search_context).
            # Prefer a group that actually offers the requested player count:
            # when the search context is the full page, .ui-selectonebutton also
            # matches the tee sheet's time period filter (ALL/MORNING/AFTERNOON/
            # AVAILABLE), which comes first in the DOM. Taking that first match
            # is how a live booking failed with "Could not find radio input".
            button_group = None
            decoy_group = None
            radio_selector = DOM.PLAYER_COUNT.radio_input_template.format(value=num_players)
            for selector in DOM.PLAYER_COUNT.button_group:
                candidates = search_context.find_elements(By.CSS_SELECTOR, selector)
                if not candidates:
                    logger.debug(f"BOOKING_DEBUG: Button group not found with selector: {selector}")
                    continue
                for candidate in candidates:
                    if candidate.find_elements(By.CSS_SELECTOR, radio_selector):
                        button_group = candidate
                        logger.info(
                            f"BOOKING_DEBUG: Found player button group with selector: {selector}"
                        )
                        break
                if button_group is not None:
                    break
                if decoy_group is None:
                    decoy_group = candidates[0]
                    logger.debug(
                        f"BOOKING_DEBUG: {len(candidates)} group(s) matched {selector}, none with a "
                        f"radio input for {num_players} players"
                    )

            # No group offered the requested count - fall through to the label and
            # dropdown strategies below with the best candidate we saw.
            if button_group is None:
                button_group = decoy_group

            if button_group:
                # Find the button with the correct value
                # The button contains a radio input with the value we want
                try:
                    # Find the radio input with the correct value
                    radio_input = button_group.find_element(
                        By.CSS_SELECTOR,
                        DOM.PLAYER_COUNT.radio_input_template.format(value=num_players),
                    )
                    # Get the parent div (the clickable button)
                    button_div = radio_input.find_element(
                        By.XPATH, DOM.PLAYER_COUNT.button_parent_xpath
                    )

                    # Check if the button is disabled
                    button_classes = button_div.get_attribute("class") or ""
                    logger.info(
                        f"BOOKING_DEBUG: Player {num_players} button classes: {button_classes}"
                    )
                    if DOM.PLAYER_COUNT.disabled_class in button_classes:
                        logger.error(
                            f"BOOKING_DEBUG: Player count {num_players} button is disabled"
                        )
                        return False

                    # Click the button (execute_script requires the driver, not search_context)
                    driver.execute_script("arguments[0].click();", button_div)
                    logger.info(
                        f"BOOKING_DEBUG: Clicked player count button for {num_players} players"
                    )
                    self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

                    # Verify the selection took effect by checking for player rows
                    if not self._verify_player_rows_appeared(driver, num_players):
                        logger.error(
                            f"BOOKING_DEBUG: Player rows did not appear after selecting {num_players} players"
                        )
                        return False

                    logger.debug(f"BOOKING_DEBUG: Successfully selected {num_players} players")
                    return True
                except NoSuchElementException:
                    logger.warning(
                        f"BOOKING_DEBUG: Could not find radio input for {num_players} players"
                    )

                # Alternative strategy: some PrimeFaces/JSF variants render the select-one-button
                # without a visible/usable radio input. In that case, click the button by label.
                try:
                    candidate_buttons = button_group.find_elements(
                        By.CSS_SELECTOR, DOM.PLAYER_COUNT.candidate_buttons
                    )
                    for candidate in candidate_buttons:
                        try:
                            candidate_text = (candidate.text or "").strip()
                            if candidate_text != str(num_players):
                                continue

                            candidate_classes = candidate.get_attribute("class") or ""
                            logger.info(
                                f"BOOKING_DEBUG: Player {num_players} button classes: {candidate_classes}"
                            )
                            if DOM.PLAYER_COUNT.disabled_class in candidate_classes:
                                logger.error(
                                    f"BOOKING_DEBUG: Player count {num_players} button is disabled"
                                )
                                return False

                            driver.execute_script("arguments[0].click();", candidate)
                            logger.info(
                                f"BOOKING_DEBUG: Clicked player count button for {num_players} players"
                            )
                            self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

                            if not self._verify_player_rows_appeared(driver, num_players):
                                logger.error(
                                    f"BOOKING_DEBUG: Player rows did not appear after selecting {num_players} players"
                                )
                                return False

                            logger.debug(
                                f"BOOKING_DEBUG: Successfully selected {num_players} players"
                            )
                            return True
                        except Exception:
                            continue
                except Exception:
                    pass

                try:
                    group_html = button_group.get_attribute("outerHTML")
                    if group_html and len(group_html) > 2000:
                        group_html = group_html[:2000] + "... [truncated]"
                    logger.debug(f"BOOKING_DEBUG: Player button group HTML: {group_html}")
                except Exception:
                    pass

            # Fallback: try dropdown selectors (scoped to search_context)
            for selector in DOM.PLAYER_COUNT.dropdown_fallbacks:
                try:
                    player_select = search_context.find_element(By.CSS_SELECTOR, selector)
                    select = Select(player_select)
                    select.select_by_value(str(num_players))
                    logger.info(f"Selected {num_players} players using selector: {selector}")
                    self.wait_strategy.wait_after_action(driver, fixed_duration=0.5)
                    return True
                except NoSuchElementException:
                    continue
                except Exception as e:
                    logger.debug(f"Unexpected error trying selector {selector}: {e}")
                    continue

            logger.warning(
                f"Could not find player count selector - site may auto-fill or use different control. "
                f"Requested {num_players} players."
            )
            return False

        except Exception as e:
            logger.warning(f"Error selecting player count: {e}")
            return False

    def _verify_player_rows_appeared(self, driver: webdriver.Chrome, expected_players: int) -> bool:
        """
        Verify that the expected number of player rows appeared after selecting player count.

        This is a critical verification step to ensure the booking form properly
        transitioned to show all player slots before attempting to add TBD guests.

        Args:
            driver: The WebDriver instance
            expected_players: Number of player rows expected (including primary player)

        Returns:
            True if expected number of rows found, False otherwise
        """
        logger.debug(f"BOOKING_DEBUG: Verifying {expected_players} player rows appeared")

        # Wait a bit for the DOM to update after player count selection
        self.wait_strategy.wait_for_element(
            driver,
            (By.CSS_SELECTOR, DOM.PLAYER_COUNT.player_rows_wait),
            fixed_duration=2.0,
            timeout=5.0,
        )

        for selector in DOM.PLAYER_COUNT.player_rows:
            try:
                player_rows = driver.find_elements(By.CSS_SELECTOR, selector)
                if len(player_rows) >= expected_players:
                    logger.info(
                        f"BOOKING_DEBUG: Found {len(player_rows)} player rows using selector: {selector}"
                    )
                    return True
                elif len(player_rows) > 0:
                    logger.info(
                        f"BOOKING_DEBUG: Found {len(player_rows)} rows (need {expected_players}) "
                        f"using selector: {selector}"
                    )
            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Error checking selector {selector}: {e}")

        # Log diagnostic info about what we found
        try:
            tables = driver.find_elements(By.TAG_NAME, "table")
            logger.debug(f"BOOKING_DEBUG: Page has {len(tables)} tables total")
            for i, table in enumerate(tables[:5]):
                table_id = table.get_attribute("id") or "no-id"
                table_class = table.get_attribute("class") or "no-class"
                rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                logger.info(
                    f"BOOKING_DEBUG: Table {i}: id='{table_id}', class='{table_class}', rows={len(rows)}"
                )
        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Error logging table info: {e}")

        logger.error(
            f"BOOKING_DEBUG: Could not find {expected_players} player rows. "
            f"The player count selection may not have taken effect."
        )
        return False

    def _add_tbd_registered_guests_sync(
        self,
        driver: webdriver.Chrome,
        num_tbd_guests: int,
        search_context: Any = None,
    ) -> bool:
        """
        Add TBD Registered Guests for additional player slots.

        After selecting the player count, the booking form shows player rows.
        For players 2, 3, 4, we need to click the "TBD" button to register them
        as TBD Registered Guests.

        Note: After clicking a TBD button, the DOM updates and element references
        become stale. We re-find the player rows after each click to avoid
        stale element reference errors.

        Args:
            driver: The WebDriver instance (needed for execute_script calls)
            num_tbd_guests: Number of TBD guests to add (1-3)
            search_context: Element to search within (modal element or driver).
                           Defaults to driver if not provided.

        Returns:
            True if TBD guests were successfully added, False otherwise
        """
        if search_context is None:
            search_context = driver

        try:
            logger.info(
                f"BOOKING_DEBUG: Starting TBD guest registration for {num_tbd_guests} guests"
            )
            # Wait for the player table to update after selecting player count
            self.wait_strategy.wait_for_element(
                search_context,
                (By.CSS_SELECTOR, DOM.TBD_GUESTS.player_rows_wait),
                fixed_duration=2.0,
                timeout=5.0,
            )

            tbd_buttons_added = 0

            # Process each guest slot one at a time, re-finding rows after each click
            # to avoid stale element references
            for guest_index in range(num_tbd_guests):
                player_num = guest_index + 2  # Players 2, 3, 4
                logger.info(
                    f"BOOKING_DEBUG: Processing TBD guest {guest_index + 1}/{num_tbd_guests} (player {player_num})"
                )

                # Re-find player rows each iteration to avoid stale references
                # Try multiple selectors for player rows as the DOM structure may vary
                player_rows = []
                for row_selector in DOM.TBD_GUESTS.player_rows:
                    player_rows = search_context.find_elements(By.CSS_SELECTOR, row_selector)
                    if len(player_rows) > 1:  # Need at least 2 rows (primary + guests)
                        logger.info(
                            f"BOOKING_DEBUG: Found {len(player_rows)} player rows using: {row_selector}"
                        )
                        break

                if guest_index == 0:
                    logger.debug(f"BOOKING_DEBUG: Initial player row count: {len(player_rows)}")
                    if len(player_rows) == 0:
                        # Log page structure for debugging
                        try:
                            tables = driver.find_elements(By.TAG_NAME, "table")
                            logger.error(
                                f"BOOKING_DEBUG: No player rows found. Page has {len(tables)} tables"
                            )
                            for i, table in enumerate(tables[:3]):
                                table_id = table.get_attribute("id") or "no-id"
                                table_class = table.get_attribute("class") or "no-class"
                                logger.info(
                                    f"BOOKING_DEBUG: Table {i}: id={table_id}, class={table_class}"
                                )
                        except Exception:
                            pass

                # Check if we have enough rows
                if len(player_rows) <= guest_index + 1:
                    logger.error(
                        f"BOOKING_DEBUG: Not enough player rows for player {player_num}. Have {len(player_rows)} rows, need at least {guest_index + 2}"
                    )
                    break

                row = player_rows[guest_index + 1]  # Skip first row (primary player)

                try:
                    # Look for the TBD button in this row using multiple strategies
                    tbd_button = None

                    # Strategy 1: CSS selectors for TBD button/link
                    tbd_selectors = [
                        "a[id*='tbd']",
                        "span[id*='tbd']",
                        "button[id*='tbd']",
                        "[class*='btn-tbd']",
                        "a[class*='tbd']",
                        "span[class*='tbd']",
                        "button[class*='tbd']",
                        "a[id*='TBD']",
                        "span[id*='TBD']",
                        "button[id*='TBD']",
                        "[class*='TBD']",
                        # Common button patterns
                        "a.ui-commandlink",
                        "button.ui-button",
                    ]

                    for selector in tbd_selectors:
                        try:
                            tbd_button = row.find_element(By.CSS_SELECTOR, selector)
                            if tbd_button and tbd_button.is_displayed():
                                logger.info(f"Found TBD button using CSS: {selector}")
                                break
                            tbd_button = None
                        except NoSuchElementException:
                            continue

                    # Strategy 2: XPath text matching for "TBD" text
                    if not tbd_button:
                        try:
                            # Look for any clickable element containing "TBD" text
                            tbd_button = row.find_element(
                                By.XPATH,
                                ".//a[contains(text(), 'TBD')] | "
                                ".//span[contains(text(), 'TBD')] | "
                                ".//button[contains(text(), 'TBD')] | "
                                ".//*[contains(@title, 'TBD')] | "
                                ".//*[contains(@aria-label, 'TBD')]",
                            )
                            if tbd_button and tbd_button.is_displayed():
                                logger.info("Found TBD button using XPath text match")
                        except NoSuchElementException:
                            pass

                    # Strategy 3: Look for any link/button that might be the TBD action
                    if not tbd_button:
                        try:
                            # Find all clickable elements in the row
                            clickables = row.find_elements(
                                By.CSS_SELECTOR, "a, button, span[onclick], div[onclick]"
                            )
                            for elem in clickables:
                                elem_text = elem.text.strip().lower()
                                elem_id = (elem.get_attribute("id") or "").lower()
                                elem_class = (elem.get_attribute("class") or "").lower()
                                if (
                                    "tbd" in elem_text
                                    or "tbd" in elem_id
                                    or "tbd" in elem_class
                                    or "guest" in elem_text
                                ):
                                    if elem.is_displayed():
                                        tbd_button = elem
                                        logger.info(
                                            f"Found TBD button via clickable scan: "
                                            f"text='{elem_text}', id='{elem_id}'"
                                        )
                                        break
                        except Exception as e:
                            logger.debug(f"Clickable scan failed: {e}")

                    if tbd_button:
                        # Click the TBD button
                        driver.execute_script("arguments[0].click();", tbd_button)
                        logger.info(f"Clicked TBD button for player {player_num}")
                        tbd_buttons_added += 1
                        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                    else:
                        # If no TBD button, try to find the player name input and type "TBD"
                        player_input = None
                        input_selectors = [
                            "input[id*='player_input']",
                            "input[id*='player']",
                            "input[name*='player']",
                            "input[type='text']",
                            "input.ui-autocomplete-input",
                        ]

                        for input_selector in input_selectors:
                            try:
                                player_input = row.find_element(By.CSS_SELECTOR, input_selector)
                                if player_input and player_input.is_displayed():
                                    break
                                player_input = None
                            except NoSuchElementException:
                                continue

                        if player_input and not player_input.get_attribute("disabled"):
                            player_input.clear()
                            player_input.send_keys("TBD Registered Guest")
                            logger.info(f"Entered TBD Registered Guest for player {player_num}")
                            tbd_buttons_added += 1
                            self.wait_strategy.wait_after_action(driver, fixed_duration=0.5)
                        else:
                            logger.warning(
                                f"BOOKING_DEBUG: Could not find TBD button or input for player {player_num}"
                            )
                            # Log detailed element state for debugging
                            self._log_row_element_state(driver, row, player_num)

                except Exception as e:
                    logger.warning(
                        f"BOOKING_DEBUG: Error adding TBD guest for player {player_num}: {e}"
                    )

            if tbd_buttons_added == num_tbd_guests:
                logger.info(f"Successfully added {tbd_buttons_added} TBD Registered Guests")
                return True
            else:
                logger.error(
                    f"BOOKING_DEBUG: Failed to add all TBD guests. "
                    f"Added {tbd_buttons_added} of {num_tbd_guests} required. "
                    f"This will cause the booking to fail."
                )
                return False

        except Exception as e:
            logger.error(f"Error adding TBD Registered Guests: {e}")
            return False

    def _find_and_book_time_slot_sync(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        times_to_exclude: set[time] | None = None,
        tee_time_interval_minutes: int = 8,
        skip_scroll: bool = False,
        use_fast_js: bool = False,
        prelocated_slot: dict[str, Any] | None = None,
        execute_at_timestamp_ms: int | None = None,
        window_timestamp_ms: int | None = None,
        target_date: date | None = None,
    ) -> BookingResult:
        """
        Find an available time slot and book it.

        First scrolls through the datascroller to load all relevant time slots,
        then searches for the requested time within the fallback window.

        Uses _find_empty_slots for all bookings (both single and multi-player)
        to ensure both completely empty slots (with Reserve button) and partially
        filled slots (with Available spans) are found.

        When times_to_exclude is provided (typically during batch booking), the
        method will avoid selecting those times as fallback slots to prevent
        conflicts with other bookings in the batch.

        When use_fast_js is True, uses a single JavaScript execution to find and
        click the target slot, reducing slot finding from ~17s to ~100ms.

        When prelocated_slot is provided (a dict returned by _find_target_slot_js),
        the fast-JS path will reuse it instead of re-scanning the DOM, unless
        the slot's time is in times_to_exclude, in which case it falls back to
        a fresh _find_target_slot_js call.

        Args:
            driver: The WebDriver instance
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: Window to search for alternatives
            times_to_exclude: Optional set of times to avoid when selecting fallback slots.
                             Used during batch booking to prevent conflicts.
            tee_time_interval_minutes: Spacing between tee times (e.g., 8 for Northgate, 10 for Walden).
                             Fallback times must be multiples of this interval from the requested time.
            use_fast_js: If True, use JavaScript-based slot finding and clicking for speed.
            prelocated_slot: Optional pre-computed slot dict from _find_target_slot_js.
                            Used to skip DOM re-scanning when the slot was located before
                            the booking window opened.
            execute_at_timestamp_ms: Optional Unix timestamp in milliseconds for timed booking.
            window_timestamp_ms: The club's stated 06:30:00, when it differs from
                the instant above. Reporting only: every offset in the race
                ledger is measured from here, so the aim can move without
                starting a second scale alongside the mornings already recorded.
                            When provided, uses _stage_timed_booking_chain_js which busy-waits
                            in JavaScript until the exact timestamp before clicking Reserve.
                            This eliminates Python→Selenium→JS handoff latency at 6:30 AM.
            target_date: The date being booked. Only needed to resolve a
                            direct-HTTP outcome against the member's reservations
                            page; without it an unreadable response stays unresolved.

        Returns:
            BookingResult with booking outcome
        """
        if times_to_exclude is None:
            times_to_exclude = set()
        target_minutes = target_time.hour * 60 + target_time.minute

        if not skip_scroll:
            self._scroll_to_load_all_slots(driver, target_time, fallback_window_minutes)

        # === FAST PATH: JavaScript-based slot finding and clicking ===
        if use_fast_js:
            # Use prelocated slot if available and its time is not excluded
            slot_info = None
            if prelocated_slot is not None:
                prelocated_time = time(prelocated_slot["hours"], prelocated_slot["minutes"])
                if prelocated_time not in times_to_exclude:
                    slot_info = prelocated_slot
                    logger.info(
                        f"FAST_JS: Using prelocated slot at {prelocated_time.strftime('%I:%M %p')} "
                        f"(index={prelocated_slot['index']})"
                    )
                else:
                    logger.info(
                        f"FAST_JS: Prelocated slot at {prelocated_time.strftime('%I:%M %p')} "
                        f"is now excluded, re-scanning DOM"
                    )

            if slot_info is None:
                slot_info = self._find_target_slot_js(
                    driver,
                    target_time,
                    num_players,
                    fallback_window_minutes,
                    tee_time_interval_minutes,
                    times_to_exclude,
                )

            # In timed mode an empty scan is not an answer about slot
            # availability at the target - it usually just means the window has
            # not opened yet - so keep looking rather than reporting no slots.
            if slot_info is None and execute_at_timestamp_ms is not None:
                slot_info = self._rescan_for_slot_until_window_open(
                    driver,
                    target_time,
                    num_players,
                    fallback_window_minutes,
                    tee_time_interval_minutes,
                    times_to_exclude,
                    execute_at_timestamp_ms,
                )

            if slot_info is None:
                error_message = (
                    f"No time slots with {num_players} available spots within "
                    f"{fallback_window_minutes} minutes of {target_time.strftime('%I:%M %p')}"
                )
                detail = self._build_unavailability_detail(
                    driver, target_time, fallback_window_minutes
                )
                if detail:
                    error_message += f". {detail}"
                # The case the sheet was staged for. "Nothing was bookable" is
                # the finder's account of a sheet nobody else can see, and this
                # is the one path where the sheet is the whole answer.
                self._flush_pre_window_sheet("no_candidate")
                return BookingResult(
                    success=False,
                    course_name=self.NORTHGATE_COURSE_NAME,
                    error_message=error_message,
                )

            booked_time = time(slot_info["hours"], slot_info["minutes"])
            is_exact = slot_info["isExact"]

            fallback_reason = None
            if not is_exact:
                fallback_reason = (
                    f"Exact time {target_time.strftime('%I:%M %p')} not available, "
                    f"using {booked_time.strftime('%I:%M %p')}"
                )

            # Ranked now, while there is time to spend on a DOM scan, and only
            # for the timed race. The slot picked above is a guess about a sheet
            # that renders held slots as free; these are the tee times the chain
            # is allowed to fall back to when the club refuses that guess.
            fallback_times: list[time] = []
            if execute_at_timestamp_ms is not None:
                try:
                    fallback_times = [
                        candidate_time
                        for candidate in self._rank_candidate_slots_js(
                            driver,
                            target_time,
                            num_players,
                            fallback_window_minutes,
                            tee_time_interval_minutes,
                            times_to_exclude,
                        )
                        if (candidate_time := time(candidate["hours"], candidate["minutes"]))
                        != booked_time
                    ]
                except Exception as e:  # noqa: BLE001 - a bonus must not cost the booking
                    # Fallbacks improve a losing morning; they are not what makes
                    # a winning one work. A scan that cannot be read leaves the
                    # chain firing one Reserve, which is what it did before.
                    logger.warning(
                        "BOOKING_DEBUG: Could not rank fallback tee times (%s: %s); "
                        "the chain will get one Reserve attempt",
                        type(e).__name__,
                        e,
                    )

            # === ULTRA-FAST PATH: Execute entire booking flow in rapid JS sequence ===
            # This replaces: _click_slot_by_index_js + _complete_booking_sync
            # with a single JS execution that does Reserve → player count → TBD → Book Now
            # in under 2 seconds instead of 7-15 seconds.
            #
            # When execute_at_timestamp_ms is provided, use the timed chain which
            # busy-waits in JS until the exact target timestamp before clicking.
            # This eliminates Python→Selenium→JS handoff latency at the critical moment.
            # The direct-HTTP path replays the same PrimeFaces requests without
            # the browser on the critical path. It returns None when it is
            # disabled or could not be staged, and a result whose phase says
            # the booking was never submitted when it failed early - both mean
            # the JS chain below is still free to run.
            chain_result = self._try_direct_http_booking(
                driver,
                slot_info,
                num_players,
                execute_at_timestamp_ms,
                fallback_times=fallback_times,
                window_timestamp_ms=window_timestamp_ms,
            )

            if chain_result is None:
                if execute_at_timestamp_ms is not None:
                    chain_result = self._stage_timed_booking_chain_js(
                        driver,
                        slot_info["index"],
                        num_players,
                        execute_at_timestamp_ms,
                    )
                else:
                    chain_result = self._execute_fast_booking_chain_js(
                        driver,
                        slot_info["index"],
                        num_players,
                    )

            # The chain may have settled on a tee time other than the one picked
            # by row index above: a refused slot sends it down the fallback
            # list. Everything after this names the slot actually held - the
            # reservations check that decides whether the booking is real, and
            # the time the member is told they have.
            held_time = chain_result.get("bookedSlotTime")
            if held_time is not None and held_time != booked_time:
                logger.info(
                    "DIRECT_HTTP: Booked %s rather than %s after %d refusal(s) - tried %s",
                    held_time.strftime("%I:%M %p"),
                    booked_time.strftime("%I:%M %p"),
                    len(chain_result.get("attemptedTimes", [])) - 1,
                    # Distinct, because a sweep asks for the same tee time on
                    # several rungs and listing it once per attempt reads as
                    # several slots refused when it was one slot asked twice.
                    # The count above stays per-attempt: that is the refusals.
                    ", ".join(
                        t.strftime("%I:%M %p")
                        for t in chain_result.get(
                            "distinctAttemptedTimes", chain_result.get("attemptedTimes", [])
                        )
                    ),
                )
                booked_time = held_time
                is_exact = held_time == target_time
                fallback_reason = (
                    None
                    if is_exact
                    else (
                        f"Exact time {target_time.strftime('%I:%M %p')} was taken, "
                        f"booked {held_time.strftime('%I:%M %p')}"
                    )
                )

            if chain_result.get("blocked"):
                # Another member took the slot at the same moment - or the chain
                # read a popup as saying so. On the direct-HTTP path the verdict
                # comes from a response the browser never received, so the
                # browser photograph below cannot show the popup that produced
                # it: the response is the only account of it there is.
                blocked_phase = chain_result.get("phase", "unknown")
                blocked_direct = chain_result.get("path") == DIRECT_HTTP_PATH
                blocked_held: bool | None = None
                if blocked_direct:
                    blocked_markup = chain_result.get("finalMarkup")
                    if blocked_markup:
                        self._capture_response_artifact(
                            f"direct_http_blocked_{blocked_phase}", blocked_markup
                        )
                    # And the sheet the Reserve was fired against. A blocked
                    # verdict is only readable next to the view that produced
                    # it: whether the club was still counting down in it, and
                    # whether the slot was open, is what separates "a member
                    # beat us" from "we reserved against a view the club had
                    # not opened yet" - the two this path exists to tell apart.
                    self._capture_refresh_artifact(chain_result, blocked_phase)
                # The sheet the fallback list was built from. A blocked morning
                # that walked no fallbacks is the case this exists for: it says
                # whether the finder had anything to walk.
                self._flush_pre_window_sheet(f"blocked_{blocked_phase}")
                # Before the reservations check, not after: that check navigates
                # to the dashboard, and a screenshot taken on the way out would
                # show that page instead of the state that failed.
                self._capture_diagnostic_info(driver, "slot_blocked_by_other_user")
                # A blocked verdict past the Reserve POST is a post-submit
                # failure like any other - the request was accepted and the
                # browser never saw what became of it. Ask the reservations page
                # before writing the tee time off, or a member holding a slot is
                # told they have none.
                if blocked_direct and blocked_phase not in PRE_SUBMIT_PHASES:
                    blocked_held = self._reservation_exists(driver, target_date, booked_time)
                if blocked_held:
                    logger.warning(
                        "DIRECT_HTTP: Chain reported the slot blocked at %s but the "
                        "reservation is on the member's reservations page; reporting success",
                        blocked_phase,
                    )
                    return BookingResult(
                        success=True,
                        booked_time=booked_time,
                        fallback_reason=fallback_reason,
                        course_name=self.NORTHGATE_COURSE_NAME,
                    )

                return BookingResult(
                    success=False,
                    error_message=self._member_facing_failure(
                        site_message=None,
                        technical="Slot blocked by another user",
                        unchecked=blocked_held is None
                        and blocked_direct
                        and blocked_phase not in PRE_SUBMIT_PHASES,
                    ),
                    booked_time=booked_time,
                    course_name=self.NORTHGATE_COURSE_NAME,
                    # Two ways to know this refusal left nothing behind: it
                    # stopped before anything reached the server, or it did not
                    # and the reservations page was read and came back empty.
                    # `blocked_held is None` is the third case - asked and could
                    # not tell - and stays unsafe.
                    verified_not_reserved=(
                        blocked_phase in PRE_SUBMIT_PHASES or blocked_held is False
                    ),
                )

            if not chain_result.get("success"):
                phase = chain_result.get("phase", "unknown")
                error = chain_result.get("error", "Unknown error in fast booking chain")
                held: bool | None = None
                # Outside the path check below on purpose. A chain that raised
                # builds its result by hand and carries no `path`, so anything
                # gated on the direct-HTTP branch would skip the very failure
                # nobody has an account of.
                self._flush_pre_window_sheet(f"failed_{phase}")
                if chain_result.get("path") == DIRECT_HTTP_PATH:
                    partial_markup = chain_result.get("finalMarkup")
                    if partial_markup:
                        self._capture_response_artifact(
                            f"direct_http_failed_{phase}", partial_markup
                        )
                    self._capture_refresh_artifact(chain_result, phase)
                    # Before the reservations check, not after: that check
                    # navigates to the dashboard, and a screenshot taken on the
                    # way out would show that page instead of the state that
                    # failed.
                    self._capture_diagnostic_info(driver, f"fast_chain_failed_{phase}")
                    # Past the Reserve POST the chain's own failure says nothing
                    # about the reservation: the browser never saw the booking,
                    # and the step that would have confirmed it is the one that
                    # broke. Ask the reservations page before writing the tee
                    # time off - reporting a booking we hold as failed sends the
                    # member to a course thinking they have no slot.
                    if phase not in PRE_SUBMIT_PHASES:
                        held = self._reservation_exists(driver, target_date, booked_time)
                    if held:
                        logger.warning(
                            "DIRECT_HTTP: Chain failed at %s but the reservation is on the "
                            "member's reservations page; reporting success",
                            phase,
                        )
                        return BookingResult(
                            success=True,
                            booked_time=booked_time,
                            fallback_reason=fallback_reason,
                            course_name=self.NORTHGATE_COURSE_NAME,
                        )
                else:
                    self._capture_diagnostic_info(driver, f"fast_chain_failed_{phase}")

                technical = f"Fast booking failed at {phase}: {error}"
                site_message = chain_result.get("responseMessage")
                # "Could not check" is not "not booked". Saying so keeps this
                # message honest in the same way the verification branch below is.
                unchecked = (
                    held is None
                    and chain_result.get("path") == DIRECT_HTTP_PATH
                    and phase not in PRE_SUBMIT_PHASES
                )

                return BookingResult(
                    success=False,
                    error_message=self._member_facing_failure(
                        site_message=site_message,
                        technical=technical,
                        unchecked=unchecked,
                    ),
                    booked_time=booked_time,
                    course_name=self.NORTHGATE_COURSE_NAME,
                )

            # The direct-HTTP chain never touches the browser, so the DOM still
            # shows the pre-booking tee sheet. Verifying against it would time
            # out on every wait and then report a completed booking as failed;
            # the last partial response is the only record of the outcome.
            direct_markup = chain_result.get("finalMarkup")
            if direct_markup:
                page_text = visible_text(direct_markup)
                confirmation_number = self._extract_confirmation_number_from_text(page_text)
                confirmed, verdict_detail = self._booking_text_verdict(
                    page_text, "direct HTTP response"
                )
                if confirmed:
                    # Checked even though the response said yes. "Confirmed" here
                    # is a phrase match on a PrimeFaces partial update - 08-15
                    # returned success on the word "thank you" - and the one
                    # failure class that looks exactly like a win in the logs is
                    # a chain that completed against no reservation. The check
                    # costs a page load after the race is over, and its answer is
                    # the only thing that separates the two.
                    #
                    # Reported, not enforced: a text-confirmed booking is not
                    # thrown away because the reservations page was slow to load,
                    # and _reservation_exists returns None rather than False when
                    # it could not read the page at all.
                    held = self._reservation_exists(driver, target_date, booked_time)
                    if held is False:
                        logger.error(
                            "RESERVATION_CHECK: The response confirmed the booking (%s) but "
                            "the tee time is not on the member's reservations page",
                            verdict_detail,
                        )
                        self._capture_response_artifact(
                            "direct_http_confirmed_not_listed", direct_markup
                        )
                    return BookingResult(
                        success=True,
                        booked_time=booked_time,
                        confirmation_number=confirmation_number,
                        fallback_reason=fallback_reason,
                        course_name=self.NORTHGATE_COURSE_NAME,
                    )

                # The Book Now response is a PrimeFaces partial update, not a
                # confirmation page: it can carry the reservation without saying
                # so in words the phrase check recognizes. Treating that silence
                # as a failure is how a booked tee time gets reported as lost, so
                # the response is kept for diagnosis and the outcome is settled
                # against the member's reservations page instead.
                self._capture_response_artifact("direct_http_verify_failed", direct_markup)
                held = self._reservation_exists(driver, target_date, booked_time)
                if held:
                    logger.info(
                        "DIRECT_HTTP: Response was unreadable (%s) but the reservation is on "
                        "the member's reservations page; reporting success",
                        verdict_detail,
                    )
                    return BookingResult(
                        success=True,
                        booked_time=booked_time,
                        confirmation_number=confirmation_number,
                        fallback_reason=fallback_reason,
                        course_name=self.NORTHGATE_COURSE_NAME,
                    )

                self._capture_diagnostic_info(driver, "direct_http_verify_failed")
                technical = (
                    "Direct-HTTP booking chain completed but the response did not "
                    f"confirm the reservation ({verdict_detail})"
                )
                if held is False:
                    technical += " and the tee time is not on the member's reservations page"
                # The response's own message containers are the only place a
                # refusal we have no phrase for can still be read from.
                site_message = chain_result.get("responseMessage")
                self._flush_pre_window_sheet("unverified")
                return BookingResult(
                    success=False,
                    error_message=self._member_facing_failure(
                        site_message=site_message,
                        technical=technical,
                        unchecked=held is None,
                    ),
                    booked_time=booked_time,
                    course_name=self.NORTHGATE_COURSE_NAME,
                )

            # Fast chain succeeded - wait briefly for page to settle after Book Now click
            # The JS chain clicked Book Now but the page may still be processing
            try:
                # Wait for either URL change or success indicators
                wait = WebDriverWait(driver, 5)
                try:
                    # First check if URL changed (common for successful bookings)
                    wait.until(
                        lambda d: "confirmation" in d.current_url.lower()
                        or "success" in d.current_url.lower()
                        or "thank" in d.current_url.lower()
                    )
                except TimeoutException:
                    # URL didn't change, try waiting for success text on page
                    try:
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (
                                    By.XPATH,
                                    "//*[contains(text(), 'success') or "
                                    "contains(text(), 'confirm') or "
                                    "contains(text(), 'thank')]",
                                )
                            )
                        )
                    except TimeoutException:
                        # No explicit success indicator, proceed with verification
                        pass
            except Exception as e:
                logger.debug(f"FAST_BOOKING: Post-chain wait exception (non-fatal): {e}")

            confirmation_number = self._extract_confirmation_number(driver)
            if self._verify_booking_success(driver):
                return BookingResult(
                    success=True,
                    booked_time=booked_time,
                    confirmation_number=confirmation_number,
                    fallback_reason=fallback_reason,
                    course_name=self.NORTHGATE_COURSE_NAME,
                )
            else:
                # Fast chain reported success but verification failed
                self._capture_diagnostic_info(driver, "fast_chain_verify_failed")
                error_details = self._extract_booking_error_message(driver)
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Fast booking chain completed but verification failed"
                        f"{': ' + error_details if error_details else ''}"
                    ),
                    booked_time=booked_time,
                    course_name=self.NORTHGATE_COURSE_NAME,
                )

        # === EXISTING SLOW PATH (Python-based Selenium iteration) ===

        northgate_section = None
        try:
            sections = driver.find_elements(By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.course_section)
            for section in sections:
                if self.NORTHGATE_COURSE_NAME.lower() in section.text.lower():
                    northgate_section = section
                    logger.info("BOOKING_DEBUG: Found Northgate course section for slot search")
                    break
        except NoSuchElementException:
            pass

        search_context: Any
        if northgate_section:
            search_context = northgate_section
        else:
            logger.warning(
                "BOOKING_DEBUG: Could not find dedicated Northgate section. "
                "Will search entire page and filter slots by course name."
            )
            search_context = driver

        slots_with_capacity = self._find_empty_slots(
            search_context, min_available_spots=num_players
        )

        if not slots_with_capacity:
            # Extract event blocks that may be causing the lack of availability
            event_blocks = self._extract_event_blocks(
                search_context, target_time, fallback_window_minutes
            )

            error_message = (
                f"No time slots with {num_players} available spots found on this date. "
                f"All slots are either fully booked or have fewer than {num_players} spots available."
            )

            event_message = self._format_event_block_message(event_blocks)
            if event_message:
                error_message = (
                    f"No time slots with {num_players} available spots found. {event_message}"
                )

            blocked_message = self._format_blocked_slot_message(
                self._extract_blocked_slot_reasons(
                    search_context, target_time, fallback_window_minutes
                )
            )
            if blocked_message:
                error_message += f" {blocked_message}"

            return BookingResult(
                success=False,
                error_message=error_message,
            )

        min_time_minutes = max(0, target_minutes - fallback_window_minutes)
        max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)

        eligible_slots: list[tuple[time, Any]] = []
        for slot_time, slot_element in slots_with_capacity:
            slot_minutes = slot_time.hour * 60 + slot_time.minute
            diff = abs(slot_minutes - target_minutes)
            if diff > fallback_window_minutes:
                continue
            if diff % tee_time_interval_minutes != 0:
                continue
            eligible_slots.append((slot_time, slot_element))

        if eligible_slots:
            walden_course_name = "walden on lake conroe"
            course_filtered_slots: list[tuple[time, Any]] = []
            filtered_out_count = 0

            # Even when we find a "Northgate" section, the DOM may still contain
            # Walden slots. For safety, always reject slots that look like Walden.
            # If the Northgate section is present, we use a non-strict filter that
            # only rejects slots with explicit Walden indicators.
            strict_course_check = northgate_section is None

            for slot_time, slot_element in eligible_slots:
                is_northgate = False
                try:
                    is_northgate = self._is_northgate_slot(
                        slot_element,
                        walden_course_name,
                        strict=strict_course_check,
                    )
                except TypeError:
                    is_northgate = self._is_northgate_slot(slot_element, walden_course_name)

                if is_northgate:
                    course_filtered_slots.append((slot_time, slot_element))
                else:
                    filtered_out_count += 1

            if filtered_out_count:
                logger.info(
                    f"BOOKING_DEBUG: Filtered {filtered_out_count} non-Northgate slots. "
                    f"{len(course_filtered_slots)} Northgate slots remain."
                )
            eligible_slots = course_filtered_slots

        logger.info(
            f"Found {len(slots_with_capacity)} slots with {num_players}+ available spots, "
            f"{len(eligible_slots)} eligible within "
            f"{time(min_time_minutes // 60, min_time_minutes % 60).strftime('%I:%M %p')}-"
            f"{time(max_time_minutes // 60, max_time_minutes % 60).strftime('%I:%M %p')} "
            f"at {tee_time_interval_minutes}-minute intervals"
        )

        all_available_times = [t for t, _ in eligible_slots]
        logger.info(
            f"BOOKING_DEBUG: Available times with {num_players}+ spots: "
            f"{[t.strftime('%I:%M %p') for t in all_available_times[:10]]}"
            f"{'...' if len(all_available_times) > 10 else ''}"
        )

        exact_match = None
        best_slot = None
        best_diff = float("inf")

        # Log excluded times if any
        if times_to_exclude:
            logger.info(
                f"BOOKING_DEBUG: Excluding times from fallback selection: "
                f"{[t.strftime('%I:%M %p') for t in sorted(times_to_exclude)]}"
            )

        for slot_time, slot_element in eligible_slots:
            slot_minutes = slot_time.hour * 60 + slot_time.minute
            diff = abs(slot_minutes - target_minutes)

            if diff == 0:
                exact_match = (slot_time, slot_element)
                logger.info(
                    f"BOOKING_DEBUG: Found exact match for requested time "
                    f"{target_time.strftime('%I:%M %p')}"
                )

            # When selecting fallback slots, skip times that are excluded
            # (e.g., times needed by other bookings in a batch)
            if slot_time in times_to_exclude and diff != 0:
                logger.debug(
                    f"BOOKING_DEBUG: Skipping {slot_time.strftime('%I:%M %p')} - "
                    f"excluded to avoid conflict with another booking"
                )
                continue

            # eligible_slots already enforces fallback window and interval alignment
            if diff < best_diff:
                best_diff = diff
                best_slot = (slot_time, slot_element)

        if exact_match:
            booked_time, reserve_element = exact_match
            logger.info(
                f"Attempting to book exact requested time at "
                f"{booked_time.strftime('%I:%M %p')} for {num_players} players"
            )
            result = self._complete_booking_sync(driver, reserve_element, booked_time, num_players)
            result.course_name = self.NORTHGATE_COURSE_NAME
            return result

        if best_slot:
            booked_time, reserve_element = best_slot
            time_diff_minutes = int(best_diff)
            logger.warning(
                f"BOOKING_DEBUG: Exact requested time {target_time.strftime('%I:%M %p')} "
                f"not available with {num_players} spots. "
                f"Using fallback time {booked_time.strftime('%I:%M %p')} "
                f"({time_diff_minutes} minutes {'earlier' if booked_time < target_time else 'later'})"
            )

            fallback_reason = None
            requested_slot = self._find_slot_by_time(search_context, target_time)
            if requested_slot:
                bookers = self._extract_bookers_from_slot(requested_slot)
                if bookers:
                    booker_names = ", ".join(bookers[:2])
                    if len(bookers) > 2:
                        booker_names += f" and {len(bookers) - 2} others"
                    fallback_reason = f"Tee time {target_time.strftime('%I:%M %p')} was already booked by {booker_names}"
                    logger.info(f"BOOKING_DEBUG: Fallback reason: {fallback_reason}")
                else:
                    fallback_reason = (
                        f"Tee time {target_time.strftime('%I:%M %p')} did not have "
                        f"{num_players} available spots"
                    )
            else:
                fallback_reason = f"Tee time {target_time.strftime('%I:%M %p')} was not available"

            logger.info(
                f"Attempting to book fallback slot at {booked_time.strftime('%I:%M %p')} "
                f"for {num_players} players (requested: {target_time.strftime('%I:%M %p')})"
            )
            result = self._complete_booking_sync(
                driver, reserve_element, booked_time, num_players, fallback_reason
            )
            result.course_name = self.NORTHGATE_COURSE_NAME
            return result
        else:
            all_times = [t.strftime("%I:%M %p") for t, _ in eligible_slots[:5]]

            # Extract event blocks that may be blocking the requested time window
            event_blocks = self._extract_event_blocks(
                search_context, target_time, fallback_window_minutes
            )

            error_message = (
                f"No time slots with {num_players} available spots within "
                f"{fallback_window_minutes} minutes of {target_time.strftime('%I:%M %p')}"
            )

            event_message = self._format_event_block_message(event_blocks)
            if event_message:
                error_message += f". {event_message}"

            blocked_message = self._format_blocked_slot_message(
                self._extract_blocked_slot_reasons(
                    search_context, target_time, fallback_window_minutes
                )
            )
            if blocked_message:
                error_message += f" {blocked_message}"

            return BookingResult(
                success=False,
                course_name=self.NORTHGATE_COURSE_NAME,
                error_message=error_message,
                alternatives=f"Slots with {num_players}+ spots: {', '.join(all_times)}"
                if all_times
                else None,
            )

    def _rescan_for_slot_until_window_open(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        tee_time_interval_minutes: int,
        times_to_exclude: set[time],
        execute_at_timestamp_ms: int,
    ) -> dict[str, Any] | None:
        """
        Re-scan the tee sheet until a slot appears or the window has been open
        for the grace period.

        Only used in timed mode, after an initial scan found nothing. Before the
        booking window opens the sheet renders its rows but reports no
        availability, and a PrimeFaces partial update can briefly blank rows that
        do have it, so a single empty scan cannot distinguish "nothing at 6:30"
        from "not 6:30 yet". Returning the first slot found also keeps the
        precision wait intact: whatever is found before the target is handed to
        the JS chain, which still waits and clicks at the target itself.

        Args:
            driver: The WebDriver instance
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: Window to search for alternatives
            tee_time_interval_minutes: Spacing between tee times
            times_to_exclude: Times to skip when selecting fallback slots
            execute_at_timestamp_ms: Unix ms timestamp when the window opens

        Returns:
            Slot dict from _find_target_slot_js, or None if none appeared
        """
        deadline_ms = execute_at_timestamp_ms + _SLOT_RESCAN_GRACE_MS
        start_ms = int(time_module.time() * 1000)
        logger.info(
            f"SLOT_RESCAN: No slot yet for {target_time.strftime('%I:%M %p')}; "
            f"re-scanning until {_SLOT_RESCAN_GRACE_MS}ms past the window "
            f"({deadline_ms - start_ms}ms budget)"
        )

        attempts = 0
        while True:
            now_ms = int(time_module.time() * 1000)
            if now_ms >= deadline_ms:
                break
            # Poll coarsely while the target is far off - there the wait is for
            # the sheet to populate and 250ms of slop costs nothing - then
            # tighten through the window opening, where it costs click latency.
            remaining_ms = execute_at_timestamp_ms - now_ms
            interval_s = (
                _SLOT_RESCAN_INTERVAL_S
                if remaining_ms > _SLOT_RESCAN_FINAL_APPROACH_MS
                else _SLOT_RESCAN_FINAL_INTERVAL_S
            )
            time_module.sleep(interval_s)
            attempts += 1
            slot_info = self._find_target_slot_js(
                driver,
                target_time,
                num_players,
                fallback_window_minutes,
                tee_time_interval_minutes,
                times_to_exclude,
            )
            if slot_info is not None:
                now_ms = int(time_module.time() * 1000)
                logger.info(
                    f"SLOT_RESCAN: Found slot at {slot_info['timeStr']} after "
                    f"{attempts} re-scan(s), {now_ms - execute_at_timestamp_ms}ms "
                    f"relative to the window opening"
                )
                return slot_info

        logger.warning(
            f"SLOT_RESCAN: Still no slot for {target_time.strftime('%I:%M %p')} "
            f"after {attempts} re-scan(s) through the window opening"
        )
        return None

    def _find_target_slot_js(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        tee_time_interval_minutes: int = 8,
        times_to_exclude: set[time] | None = None,
    ) -> dict[str, Any] | None:
        """
        Find the single best available slot. See _rank_candidate_slots_js.

        Returns:
            The best slot dict, or None if nothing in the window suits.
        """
        candidates = self._rank_candidate_slots_js(
            driver,
            target_time,
            num_players,
            fallback_window_minutes,
            tee_time_interval_minutes,
            times_to_exclude,
        )
        return candidates[0] if candidates else None

    def _rank_candidate_slots_js(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int,
        tee_time_interval_minutes: int = 8,
        times_to_exclude: set[time] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Rank every bookable slot in the fallback window, best first.

        This replaces the Python-based _find_empty_slots + _is_northgate_slot pipeline
        with a single browser-side DOM traversal, reducing slot finding from ~17s to ~100ms.

        The whole ranking is returned, not just the winner, because at 6:30 the
        winner is a guess: the sheet renders a slot another member is holding as
        Available, so the only way to learn a slot is gone is to be refused it.
        The direct-HTTP chain walks this list on refusal (see
        DirectHttpBooker._reserve_with_fallbacks); the browser chain still takes
        the head of it and stops there.

        The JavaScript iterates all slot items in the browser, checking:
        - Course membership via element ID patterns (teeTimeCourses:0 for Northgate)
        - Time extraction from labels
        - Availability via div.Empty (full slot) or span.custom-free-slot-span count
        - Fallback window, interval alignment, and time exclusions

        Args:
            driver: The WebDriver instance
            target_time: The preferred tee time
            num_players: Number of players (1-4)
            fallback_window_minutes: Window to search for alternatives
            tee_time_interval_minutes: Spacing between tee times (default 8 for Northgate)
            times_to_exclude: Times to skip when selecting fallback slots

        Returns:
            Slot dicts {index, hours, minutes, timeStr, diff, available, isExact,
            reserveId}, nearest the requested time first and the earlier tee time
            breaking a tie. Empty when nothing in the window suits.
        """
        if times_to_exclude is None:
            times_to_exclude = set()

        exclude_list = [{"h": t.hour, "m": t.minute} for t in times_to_exclude]

        js_code = _SLOT_FINDER_JS

        found = driver.execute_script(
            js_code,
            target_time.hour,
            target_time.minute,
            num_players,
            fallback_window_minutes,
            tee_time_interval_minutes,
            exclude_list,
            self.NORTHGATE_COURSE_INDEX,
            self.MAX_PLAYERS,
        )
        # A dict since the tally was added. Tolerating the old bare list keeps a
        # stubbed driver in a test - or a browser that somehow ran an older
        # script - from failing here instead of where it would say why.
        if isinstance(found, dict):
            candidates: list[dict[str, Any]] = found.get("candidates") or []
            rejected: dict[str, Any] = found.get("rejected") or {}
            scanned: Any = found.get("scanned", "?")
        else:
            candidates = found or []
            rejected, scanned = {}, "?"

        # Logged whether or not anything was found, and before the verdict: when
        # a morning comes back with no fallbacks, this line is what separates a
        # full sheet from a finder that threw bookable slots away.
        logger.info(
            "BOOKING_DEBUG: Slot scan of %s row(s) for %s, %d player(s), +/-%d min - "
            "%d candidate(s); dropped %s",
            scanned,
            target_time.strftime("%I:%M %p"),
            num_players,
            fallback_window_minutes,
            len(candidates),
            ", ".join(f"{name}={count}" for name, count in rejected.items() if count) or "nothing",
        )

        if candidates:
            best = candidates[0]
            off_grid = sum(1 for c in candidates if not c.get("aligned", True))
            logger.info(
                f"BOOKING_DEBUG: JS slot finder found slot at "
                f"{best['hours']:02d}:{best['minutes']:02d} "
                f"(index={best['index']}, exact={best['isExact']}, "
                f"available={best['available']}), "
                f"{len(candidates) - 1} fallback(s) behind it"
                f"{f' ({off_grid} off-grid)' if off_grid else ''}"
            )
        else:
            logger.warning(
                f"BOOKING_DEBUG: JS slot finder found no suitable Northgate slot "
                f"within {fallback_window_minutes} min of {target_time.strftime('%I:%M %p')} "
                f"for {num_players} players"
            )

        return candidates

    def _click_slot_by_index_js(self, driver: webdriver.Chrome, slot_index: int) -> bool:
        """
        Click the Reserve button for the slot at the given DOM index.

        Uses a single JavaScript execution to find and click the appropriate
        clickable element (Reserve button, Available span, or slot link) within
        the slot item at the specified index.

        Args:
            driver: The WebDriver instance
            slot_index: Index of the slot in the li.ui-datascroller-item NodeList

        Returns:
            True if the click was performed, False if the element was not found
        """
        js_click = """
        var items = document.querySelectorAll('li.ui-datascroller-item');
        var item = items[arguments[0]];
        if (!item) return false;

        // Find the clickable element in priority order
        var btn = item.querySelector("a[id*='reserve_button']");
        if (!btn) {
            var spans = item.querySelectorAll('span.custom-free-slot-span');
            btn = spans.length > 0 ? spans[0] : null;
        }
        if (!btn) btn = item.querySelector("a.slot-link");
        if (!btn) return false;

        btn.scrollIntoView({block: 'center'});
        btn.click();
        return true;
        """
        result = driver.execute_script(js_click, slot_index)
        if result:
            logger.info(f"BOOKING_DEBUG: JS clicked Reserve at slot index {slot_index}")
        else:
            logger.warning(f"BOOKING_DEBUG: JS failed to click Reserve at slot index {slot_index}")
        return bool(result)

    def _execute_fast_booking_chain_js(
        self,
        driver: webdriver.Chrome,
        slot_index: int,
        num_players: int,
    ) -> dict[str, Any]:
        """
        Execute the entire booking flow in a single rapid JS sequence.

        This is the speed path for bookings after the window is already open
        (e.g. second and later bookings in a batch). It runs the shared async
        chain (see _JS_ASYNC_BOOKING_CHAIN) with no target timestamp: Reserve
        is clicked immediately, then the chain polls the live DOM for the
        player page, TBD guests, and Book Now.

        Args:
            driver: The WebDriver instance
            slot_index: Index of the slot in the li.ui-datascroller-item NodeList
            num_players: Number of players (1-4)

        Returns:
            Dict with result info:
            - success: bool
            - error: str (if failed)
            - blocked: bool (if slot was grabbed by another user)
            - phase: str (which phase completed/failed)
            - timing: dict of per-phase timing metrics
        """
        logger.info(
            f"FAST_BOOKING: Starting rapid JS chain for slot {slot_index}, "
            f"{num_players} players"
        )

        result = self._run_booking_chain_js(driver, slot_index, num_players)

        timing = result.get("timing", {})
        logger.info(
            f"FAST_BOOKING: Chain completed in {timing.get('totalMs', 'N/A')}ms - "
            f"phase={result.get('phase')}, success={result.get('success')}, "
            f"blocked={result.get('blocked')}, error={result.get('error')}"
        )
        logger.debug(f"FAST_BOOKING: Timing breakdown: {timing}")

        return result

    def _stage_timed_booking_chain_js(
        self,
        driver: webdriver.Chrome,
        slot_index: int,
        num_players: int,
        target_timestamp_ms: int,
    ) -> dict[str, Any]:
        """
        Stage a booking chain that self-triggers at the exact target timestamp.

        This eliminates Python->Selenium->JS handoff latency at the critical
        moment. The JS is injected BEFORE the target time; it waits internally
        (coarse setTimeout, then a <=25ms spin for millisecond precision),
        waits for the site's own JS to remove the 'disable-div' overlay, then
        clicks Reserve and completes the booking flow.

        Args:
            driver: The WebDriver instance
            slot_index: Index of the slot in the li.ui-datascroller-item NodeList
            num_players: Number of players (1-4)
            target_timestamp_ms: Unix timestamp in milliseconds when to click Reserve

        Returns:
            Dict with result info (same as _execute_fast_booking_chain_js)
        """
        # Calculate how far in the future the target is
        now_ms = int(datetime.now().timestamp() * 1000)
        ms_until_target = target_timestamp_ms - now_ms

        logger.info(
            f"TIMED_BOOKING: Staging JS chain for slot {slot_index}, "
            f"{num_players} players, target in {ms_until_target}ms"
        )

        # Set script timeout to accommodate the wait period plus booking flow.
        # Default Selenium timeout is 30s, but we may wait 2+ minutes (6:28 -> 6:30).
        script_timeout_ms = max(ms_until_target, 0) + _CHAIN_MAX_WAIT_MS + 30000
        script_timeout_s = max(60, script_timeout_ms // 1000)

        original_timeouts = driver.timeouts
        try:
            driver.set_script_timeout(script_timeout_s)
            logger.debug(f"TIMED_BOOKING: Set script timeout to {script_timeout_s}s")

            result = self._run_booking_chain_js(
                driver, slot_index, num_players, target_timestamp_ms
            )
        finally:
            if original_timeouts and original_timeouts.script is not None:
                driver.set_script_timeout(original_timeouts.script)
            else:
                driver.set_script_timeout(30)  # Default Selenium timeout

        timing = result.get("timing", {})

        disable_div_info = ""
        if "disableDivPresentAtStart" in timing:
            if timing.get("disableDivPresentAtStart"):
                disable_div_info = (
                    f", disableDiv=present->wait={timing.get('disableDivWaitMs', '?')}ms"
                    f"->enabled={timing.get('slotsEnabledAfterWait', '?')}"
                )
            else:
                disable_div_info = ", disableDiv=absent(slots_already_enabled)"

        logger.info(
            f"TIMED_BOOKING: Chain completed - phase={result.get('phase')}, "
            f"success={result.get('success')}, blocked={result.get('blocked')}, "
            f"clickDrift={timing.get('clickDriftMs', 'N/A')}ms"
            f"{disable_div_info}, "
            f"totalMs={timing.get('totalMs', 'N/A')}"
        )
        logger.debug(f"TIMED_BOOKING: Full timing: {timing}")

        return result

    def _run_booking_chain_js(
        self,
        driver: webdriver.Chrome,
        slot_index: int,
        num_players: int,
        target_timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        Run the shared async booking chain via execute_async_script.

        The chain MUST run as an async script: everything it waits on (the
        site's timer removing 'disable-div' at 6:30, the PrimeFaces AJAX
        response that renders the player page, the blocked-slot popup) is
        delivered by the page's event loop, which a synchronous execute_script
        would block for its entire duration. See _JS_ASYNC_BOOKING_CHAIN.

        Args:
            driver: The WebDriver instance
            slot_index: Index of the slot in the li.ui-datascroller-item NodeList
            num_players: Number of players (1-4)
            target_timestamp_ms: Epoch ms to click Reserve at, or None for now

        Returns:
            The chain result dict (success/error/blocked/phase/timing).
        """
        blocked_patterns = list(DOM.SLOT_BLOCKED.blocked_text_patterns)

        raw = driver.execute_async_script(
            _JS_BLOCKED_POPUP_HELPERS + _JS_ASYNC_BOOKING_CHAIN,
            slot_index,
            num_players,
            target_timestamp_ms,
            _CHAIN_MAX_WAIT_MS,
            _CHAIN_POLL_INTERVAL_MS,
            blocked_patterns,
            _CHAIN_ENABLED_MAX_WAIT_MS,
        )
        if isinstance(raw, dict):
            return raw
        return {
            "success": False,
            "error": f"Booking chain returned unexpected result: {raw!r}",
            "blocked": False,
            "phase": "unknown",
            "timing": {},
        }

    # Chain phases the direct-HTTP path can fail in without having submitted
    # anything the server acted on. Past these, a Selenium retry would be
    # racing our own half-finished booking against a stale browser DOM, so the
    # failure is reported instead of retried. Imported rather than restated so
    # the two modules cannot drift apart on which phases are recoverable.
    _DIRECT_HTTP_FALLBACK_PHASES = PRE_SUBMIT_PHASES

    def _try_direct_http_booking(
        self,
        driver: webdriver.Chrome,
        slot_info: dict[str, Any],
        num_players: int,
        execute_at_timestamp_ms: int | None,
        fallback_times: Sequence[time] = (),
        window_timestamp_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Attempt the booking chain over direct HTTP instead of browser clicks.

        Adopts the browser's cookies and form state, stages the Reserve request
        (serialized body plus a warm TLS connection) and fires it at the target
        timestamp. See app/providers/walden_http.py for why this is faster than
        driving the same requests through Chrome.

        Args:
            driver: The WebDriver instance, already logged in and on the tee sheet
            slot_info: Slot dict from _find_target_slot_js; needs 'reserveId'
            num_players: Number of players (1-4)
            execute_at_timestamp_ms: Epoch ms to fire Reserve at, or None for now
            window_timestamp_ms: The club's stated window, for reporting offsets
                from. Defaults to the instant above when they are the same.
            fallback_times: Tee times to try, best first, when the club refuses
                the slot above as held by another member

        Returns:
            A chain-result dict (same shape as _run_booking_chain_js) when the
            direct path produced an outcome worth honoring, or None when the
            caller should fall back to the JavaScript chain.
        """
        if not settings.walden_direct_http_booking:
            return None

        reserve_id = slot_info.get("reserveId")
        if not reserve_id:
            logger.info(
                "DIRECT_HTTP: Slot at index %s exposes no Reserve component id "
                "(partially-booked slot); using the JS chain",
                slot_info.get("index"),
            )
            return None

        session: PrimeFacesSession | None = None
        try:
            session = PrimeFacesSession.from_selenium(driver)
            booker = DirectHttpBooker(session)
            booker.prepare(
                reserve_id,
                driver.page_source,
                fallback_times=fallback_times,
                # Both are for the race and only the race: an immediate booking
                # has no instant to arrive at, and is already working off a live
                # sheet rather than one staged before the window.
                measure_skew=(
                    settings.walden_measure_clock_skew and execute_at_timestamp_ms is not None
                ),
                refresh_at_window=(
                    settings.walden_refresh_view_at_window and execute_at_timestamp_ms is not None
                ),
                # The race only. An ad-hoc booking has no window to sweep around
                # - it fires into one that opened days ago - and a ladder there
                # would be nine requests for a slot the first one can have.
                sweep_offsets_ms=(
                    settings.walden_sweep_offsets_ms()
                    if execute_at_timestamp_ms is not None
                    else (0,)
                ),
                # Same reasoning: overlapping the first two rungs only means
                # anything when there is a window to bracket.
                pipeline_opening_pair=(
                    settings.walden_reserve_pipeline_opening_pair
                    and execute_at_timestamp_ms is not None
                ),
            )
        except Exception as e:  # noqa: BLE001 - opt-in path must never break booking
            # Staging parses live markup, so a malformed page can surface as
            # almost anything. Nothing has reached the server yet, so every
            # failure here is recoverable by the JS chain - and none of it may
            # escape into _book_tee_time_sync, which handles only WebDriver
            # errors and would raise instead of returning a BookingResult.
            logger.warning(
                "DIRECT_HTTP: Could not stage direct booking (%s: %s); using the JS chain",
                type(e).__name__,
                e,
            )
            if session is not None:
                session.close()
            return None

        try:
            result = booker.book(
                num_players,
                target_timestamp_ms=execute_at_timestamp_ms,
                window_timestamp_ms=window_timestamp_ms,
            )
        except Exception as e:  # noqa: BLE001 - see above
            # book() turns its own failures into results, so anything raised
            # here is a bug. Reserve may already have been sent, so report it
            # rather than handing the slot to a browser retry.
            logger.exception("DIRECT_HTTP: Booking attempt raised unexpectedly")
            return {
                "success": False,
                "blocked": False,
                "phase": "unknown",
                "error": f"Direct-HTTP booking raised {type(e).__name__}: {e}",
                "timing": {},
            }
        finally:
            session.close()

        # Before the summary line and outside the failure branches: the ledger is
        # the record of what the club did, and a morning that *won* is exactly
        # as informative about where the boundary sits as one that lost.
        if settings.walden_capture_race_ledger and execute_at_timestamp_ms is not None:
            # The stated window, not the aim: the ledger's offsets have to stay
            # on the scale the mornings before this one were recorded on.
            self._capture_race_ledger(
                result,
                window_timestamp_ms if window_timestamp_ms is not None else execute_at_timestamp_ms,
            )

        # The one line a post-mortem starts from, so everything that decides a
        # morning has to be readable in it without going back through the run.
        logger.info(
            "DIRECT_HTTP: Chain finished - phase=%s, success=%s, blocked=%s, "
            "clickDrift=%sms, %s, attempts=%s, booked=%s, tried=[%s]%s, "
            "totalMs=%s, error=%s",
            result.phase,
            result.success,
            result.blocked,
            result.timing.get("clickDriftMs", "N/A"),
            # The number that says whether we were on time. Negative means the
            # Reserve left before our own 06:30:00, which is the whole point of
            # the lead - so an untimed booking says so rather than printing one.
            (
                f"lead={result.timing['arrivalLeadMs']}ms, "
                f"sent={result.timing.get('reserveSentAtMs', '?')}ms vs window"
                if "arrivalLeadMs" in result.timing
                else "untimed (no window to lead)"
            ),
            result.timing.get("reserveAttempts", "N/A"),
            result.booked_slot_time.strftime("%I:%M %p") if result.booked_slot_time else "nothing",
            ", ".join(t.strftime("%I:%M %p") for t in result.attempted_times),
            # Absent unless a refresh ran, since it is off by default now. The
            # suffix carries why it did not land, and any countdown the club was
            # still showing.
            (
                f", viewRefresh={result.timing['viewRefreshMs']}ms/"
                f"{result.timing.get('viewRefreshAttempts', '?')}x"
                f"{' ' + result.timing['viewRefreshFailed'] if result.timing.get('viewRefreshFailed') else ''}"
                f"{' countdown=' + str(result.timing['viewRefreshCountdownS']) + 's' if result.timing.get('viewRefreshCountdownS') else ''}"
                if result.timing.get("viewRefreshMs") is not None
                else ""
            ),
            result.timing.get("totalMs", "N/A"),
            result.error,
        )

        if result.success or result.blocked:
            return result.as_chain_result()

        if result.phase in self._DIRECT_HTTP_FALLBACK_PHASES:
            logger.warning(
                "DIRECT_HTTP: Failed in phase %s before any booking was submitted; "
                "falling back to the JS chain",
                result.phase,
            )
            return None

        logger.error(
            "DIRECT_HTTP: Failed in phase %s after Reserve was accepted; not retrying "
            "in the browser (the slot may be held server-side)",
            result.phase,
        )
        return result.as_chain_result()

    def _check_slot_blocked_popup(self, driver: webdriver.Chrome) -> bool:
        """
        Check if the 'slot blocked by another user' popup is visible.

        This is a fallback check used by the Python-based booking flow.
        The fast JS chain has its own inline check.

        Only returns True if the popup text matches one of the known
        blocked_text_patterns. Other validation errors are logged but
        not treated as blocked slots.

        Args:
            driver: The WebDriver instance

        Returns:
            True if the blocked popup is visible AND contains blocked-slot text,
            False otherwise
        """
        try:
            popup = driver.find_element(By.CSS_SELECTOR, DOM.SLOT_BLOCKED.popup_visible)
            if popup.is_displayed():
                # Extract popup text
                popup_text = popup.text.lower() if popup.text else ""
                logger.warning(
                    f"BOOKING_DEBUG: Validation popup detected, text: {popup_text[:100]}"
                )

                # Check if text matches blocked-slot patterns
                is_blocked = any(
                    pattern.lower() in popup_text
                    for pattern in DOM.SLOT_BLOCKED.blocked_text_patterns
                )

                # Try to dismiss the popup regardless
                try:
                    ok_btn = popup.find_element(By.CSS_SELECTOR, DOM.SLOT_BLOCKED.ok_button)
                    ok_btn.click()
                    logger.debug("BOOKING_DEBUG: Dismissed validation popup")
                except NoSuchElementException:
                    pass

                if is_blocked:
                    logger.warning("BOOKING_DEBUG: Popup matches blocked-slot pattern")
                    return True
                else:
                    logger.warning("BOOKING_DEBUG: Popup is generic validation error, not blocked")
                    return False
        except NoSuchElementException:
            pass
        return False

    def _precision_wait_until(self, execute_at: datetime) -> None:
        """
        Wait until the exact execute_at time with millisecond precision.

        Uses coarse sleep for most of the wait, then a tight busy-wait loop
        for the final 200ms to hit the target time as precisely as possible.

        Args:
            execute_at: Datetime in CT timezone to wait until. May be naive
                (assumed CT) or timezone-aware (converted to naive CT).
        """
        # Normalize: convert aware datetimes to naive CT so comparisons are consistent
        execute_at = CTDateTime.to_naive_ct(execute_at)
        now_ct = CTDateTime.to_naive_ct(CTDateTime.now())
        if now_ct >= execute_at:
            logger.warning(
                f"BATCH_BOOKING: Already past execute_at "
                f"{execute_at.strftime('%H:%M:%S')} - proceeding immediately"
            )
            return

        wait_seconds = (execute_at - now_ct).total_seconds()
        logger.info(
            f"BATCH_BOOKING: Precision wait {wait_seconds:.1f}s until "
            f"{execute_at.strftime('%H:%M:%S.%f')}"
        )

        # Coarse sleep until 200ms before target
        if wait_seconds > 0.2:
            time_module.sleep(wait_seconds - 0.2)

        # Precision busy-wait for the final ~200ms
        # Sub-millisecond sleep reduces CPU pressure with negligible precision loss
        while CTDateTime.to_naive_ct(CTDateTime.now()) < execute_at:
            time_module.sleep(0.0001)

        logger.info("BATCH_BOOKING: Precision wait complete - GO!")

    def _scroll_to_load_all_slots(
        self,
        driver: webdriver.Chrome,
        target_time: time,
        fallback_window_minutes: int,
        max_time_minutes_override: int | None = None,
    ) -> None:
        """
        Scroll through the datascroller to load all tee time slots.

        The Walden Golf tee sheet uses a PrimeFaces datascroller component that
        lazy-loads rows as the user scrolls. This method scrolls through the
        datascroller to ensure all relevant time slots are loaded before searching.

        The scrolling stops when either:
        1. The last visible time is past target_time + fallback_window, or
        2. No new items appear after multiple scroll attempts

        Args:
            driver: The WebDriver instance
            target_time: The target tee time being searched for
            fallback_window_minutes: The fallback window in minutes
        """
        max_scroll_attempts = 50
        no_change_threshold = 3
        no_change_count = 0
        previous_item_count = 0

        target_minutes = target_time.hour * 60 + target_time.minute
        if max_time_minutes_override is None:
            max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)
        else:
            max_time_minutes = min(24 * 60 - 1, max_time_minutes_override)

        logger.info(
            f"BOOKING_DEBUG: Starting datascroller scroll to load slots up to "
            f"{time(max_time_minutes // 60, max_time_minutes % 60).strftime('%I:%M %p')}"
        )

        for attempt in range(max_scroll_attempts):
            try:
                slot_items = driver.find_elements(By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.slot_items)
                current_item_count = len(slot_items)

                if current_item_count == previous_item_count:
                    no_change_count += 1
                    if no_change_count >= no_change_threshold:
                        logger.info(
                            f"BOOKING_DEBUG: No new items after {no_change_threshold} scrolls. "
                            f"Total items loaded: {current_item_count}"
                        )
                        break
                else:
                    no_change_count = 0
                    previous_item_count = current_item_count

                if slot_items:
                    last_slot = slot_items[-1]
                    last_time = None
                    max_candidates = 10
                    for candidate in reversed(slot_items[-max_candidates:]):
                        last_time = self._extract_time_from_slot_item(candidate)
                        if last_time:
                            break

                    if last_time:
                        last_time_minutes = last_time.hour * 60 + last_time.minute
                        logger.debug(
                            f"BOOKING_DEBUG: Scroll attempt {attempt + 1}: "
                            f"{current_item_count} items, last time: {last_time.strftime('%I:%M %p')}"
                        )

                        if last_time_minutes >= max_time_minutes:
                            logger.info(
                                f"BOOKING_DEBUG: Loaded slots past target window. "
                                f"Last time: {last_time.strftime('%I:%M %p')}, "
                                f"Total items: {current_item_count}"
                            )
                            break

                    driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", last_slot)
                    self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)

                    datascroller = driver.find_elements(
                        By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.datascroller_content
                    )
                    if datascroller:
                        driver.execute_script(
                            "arguments[0].scrollTop = arguments[0].scrollHeight;",
                            datascroller[0],
                        )
                        self.wait_strategy.simple_wait(
                            fixed_duration=0.3, event_driven_duration=0.1
                        )

            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Scroll attempt {attempt + 1} error: {e}")
                self.wait_strategy.simple_wait(fixed_duration=0.2, event_driven_duration=0.1)

        logger.info(
            f"BOOKING_DEBUG: Finished scrolling. Total slot items loaded: {previous_item_count}"
        )

    def _find_empty_slots(
        self, search_context: Any, min_available_spots: int | None = None
    ) -> list[tuple[time, Any]]:
        """
        Find time slots that have at least min_available_spots available.

        The Walden Golf tee sheet has two different slot structures:

        1. Completely empty slots (all MAX_PLAYERS spots available):
           - The slot div has class="Empty"
           - Contains a "reserve_button" element with "Reserve" text
           - Structure: <div class="Empty">...<a id="...reserve_button">Reserve</a>...</div>

        2. Partially filled slots (1 to MAX_PLAYERS-1 spots available):
           - The slot div has class="Reserved"
           - Contains <span class="custom-free-slot-span">Available</span> for each open spot
           - Count the spans to determine available spots

        Args:
            search_context: The WebDriver element to search within
            min_available_spots: Minimum number of available spots required (default MAX_PLAYERS)

        Returns:
            List of (time, clickable_element) tuples for slots with enough spots
        """
        if min_available_spots is None:
            min_available_spots = self.MAX_PLAYERS
        empty_slots: list[tuple[time, Any]] = []
        completely_empty_count = 0
        partial_slots_count = 0

        try:
            # Find all time slot list items
            slot_items = search_context.find_elements(
                By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.slot_items
            )

            logger.info(f"Found {len(slot_items)} time slot items")

            for slot_item in slot_items:
                try:
                    # First check for completely empty slots (class="Empty" with reserve_button)
                    # These have all MAX_PLAYERS spots available
                    empty_divs = slot_item.find_elements(
                        By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.empty_slot
                    )

                    if empty_divs:
                        # This is a completely empty slot - all MAX_PLAYERS spots available
                        if min_available_spots <= self.MAX_PLAYERS:
                            slot_time = self._extract_time_from_slot_item(slot_item)
                            if slot_time:
                                # Find the reserve button or the Available link
                                reserve_btn = None
                                for btn_sel in DOM.SLOT_DISCOVERY.reserve_buttons:
                                    try:
                                        reserve_btn = slot_item.find_element(
                                            By.CSS_SELECTOR, btn_sel
                                        )
                                        break
                                    except NoSuchElementException:
                                        continue
                                if reserve_btn is None:
                                    reserve_btn = slot_item

                                empty_slots.append((slot_time, reserve_btn))
                                completely_empty_count += 1
                                logger.debug(
                                    f"Found completely empty slot at {slot_time.strftime('%I:%M %p')}"
                                )
                        continue

                    # Check for partially filled slots (class="Reserved" with Available spans)
                    available_spans = slot_item.find_elements(
                        By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.available_span
                    )
                    num_available = len(available_spans)

                    if num_available >= min_available_spots:
                        # This slot has enough available spots
                        slot_time = self._extract_time_from_slot_item(slot_item)

                        if slot_time:
                            # Get the first Available span as the clickable element
                            clickable = available_spans[0] if available_spans else slot_item
                            empty_slots.append((slot_time, clickable))
                            partial_slots_count += 1
                            logger.debug(
                                f"Found partial slot at {slot_time.strftime('%I:%M %p')} "
                                f"with {num_available} available spots"
                            )

                except Exception as e:
                    logger.debug(f"Could not process slot item: {e}")
                    continue

        except NoSuchElementException:
            logger.debug("No slot items found")

        empty_slots.sort(key=lambda x: x[0])
        logger.info(
            f"Found {completely_empty_count} completely empty slots and "
            f"{partial_slots_count} partial slots with {min_available_spots}+ spots"
        )
        return empty_slots

    def _extract_time_from_slot_item(self, slot_item: Any) -> time | None:
        """
        Extract the time from a time slot list item.

        The time is typically in a <label> element or in the slot's text content.

        Args:
            slot_item: The <li> element containing the time slot

        Returns:
            The parsed time, or None if not found
        """
        try:
            # Try to find a label element with the time
            try:
                time_label = slot_item.find_element(By.TAG_NAME, "label")
                time_text = time_label.text.strip()
                if time_text:
                    parsed = self._parse_time(time_text)
                    if parsed:
                        return parsed
            except NoSuchElementException:
                pass

            # Try to find time in the slot's text content
            slot_text = slot_item.text
            # Look for time pattern like "07:46 AM" or "1:30 PM"
            time_pattern = r"\b(\d{1,2}:\d{2}\s*[AaPp][Mm])\b"
            match = re.search(time_pattern, slot_text)
            if match:
                return self._parse_time(match.group(1))

            # Try to find time in any span or div
            for tag in ["span", "div"]:
                elements = slot_item.find_elements(By.TAG_NAME, tag)
                for elem in elements:
                    text = elem.text.strip()
                    if text:
                        parsed = self._parse_time(text)
                        if parsed:
                            return parsed

        except Exception as e:
            logger.debug(f"Error extracting time from slot item: {e}")

        return None

    def _find_slot_by_time(self, search_context: Any, target_time: time) -> Any | None:
        """
        Find a specific time slot by its time.

        Args:
            search_context: The WebDriver element to search within
            target_time: The time to search for

        Returns:
            The slot item element if found, None otherwise
        """
        try:
            slot_items = search_context.find_elements(
                By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.slot_items
            )
            for slot_item in slot_items:
                slot_time = self._extract_time_from_slot_item(slot_item)
                if slot_time and slot_time == target_time:
                    return slot_item
        except Exception as e:
            logger.debug(f"Error finding slot by time: {e}")
        return None

    def _get_course_index_from_element_id(self, element_id: str) -> str | None:
        """
        Extract the course index from an element ID.

        The Walden Golf website uses a consistent naming pattern in element IDs:
        - teeTimeCourses:0 = Northgate
        - teeTimeCourses:1 = Walden on Lake Conroe

        Args:
            element_id: The element's ID attribute

        Returns:
            The course index ("0" or "1") if found, None otherwise.
        """
        match = re.search(r"teeTimeCourses:(\d+)", element_id)
        if match:
            return match.group(1)
        return None

    def _is_northgate_slot(
        self, slot_element: Any, walden_course_name: str, strict: bool = True
    ) -> bool:
        """
        Check if a slot element belongs to the Northgate course.

        This method uses element ID patterns to reliably determine which course
        a slot belongs to. The Walden Golf website uses consistent IDs:
        - teeTimeCourses:0 = Northgate
        - teeTimeCourses:1 = Walden on Lake Conroe

        This approach is more reliable than the previous DOM-walking strategy
        because the course index is embedded directly in element IDs.

        Args:
            slot_element: The slot element (button, link, span, or container) to check
            walden_course_name: The name of the other course (unused, kept for API compatibility)
            strict: If True, return False when course cannot be determined.
                   If False, return True when course cannot be determined.

        Returns:
            True if the slot belongs to Northgate, False otherwise.
        """
        try:
            # Strategy 1: Check the element's own ID for course index
            element_id = slot_element.get_attribute("id") or ""
            course_index = self._get_course_index_from_element_id(element_id)

            if course_index is not None:
                is_northgate = course_index == self.NORTHGATE_COURSE_INDEX
                logger.debug(
                    f"COURSE_CHECK: Element ID '{element_id[:80]}...' -> "
                    f"course index {course_index} -> "
                    f"{'Northgate' if is_northgate else 'Walden'}"
                )
                return is_northgate

            # Strategy 2: Walk up the DOM tree to find a parent with course info
            # This handles elements like <span> that may not have their own ID
            current = slot_element
            for level in range(10):  # Check up to 10 parent levels
                try:
                    parent = current.find_element(By.XPATH, "..")
                    if parent:
                        parent_id = parent.get_attribute("id") or ""
                        course_index = self._get_course_index_from_element_id(parent_id)

                        if course_index is not None:
                            is_northgate = course_index == self.NORTHGATE_COURSE_INDEX
                            logger.debug(
                                f"COURSE_CHECK: Parent ID at level {level} "
                                f"'{parent_id[:80]}...' -> course index {course_index} -> "
                                f"{'Northgate' if is_northgate else 'Walden'}"
                            )
                            return is_northgate

                        current = parent
                except Exception:
                    break

            # Strategy 3: Could not determine course from element IDs
            # This should be rare if the page structure is consistent
            if strict:
                logger.warning(
                    "COURSE_CHECK: Could not determine course from element IDs. "
                    "Rejecting slot for safety (strict mode)."
                )
                return False
            else:
                logger.debug(
                    "COURSE_CHECK: Could not determine course from element IDs. "
                    "Accepting slot (non-strict mode)."
                )
                return True

        except Exception as e:
            logger.warning(
                f"COURSE_CHECK: Error checking slot course: {e} - rejecting slot for safety"
            )
            return False

    def _extract_bookers_from_slot(self, slot_item: Any) -> list[str]:
        """
        Extract the names of people who have booked spots in a time slot.

        The Walden Golf tee sheet shows booked spots with the member's name.
        This method extracts those names to provide context when a requested
        time is not available.

        Args:
            slot_item: The <li> element containing the time slot

        Returns:
            List of booker names found in the slot
        """
        bookers: list[str] = []
        try:
            reserved_divs = slot_item.find_elements(
                By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.reserved_slot
            )
            for div in reserved_divs:
                div_text = div.text.strip()
                if div_text and "Available" not in div_text:
                    lines = [line.strip() for line in div_text.split("\n") if line.strip()]
                    for line in lines:
                        if line and "Available" not in line and "Reserve" not in line:
                            if re.match(r"^[A-Za-z]", line) and "," in line:
                                bookers.append(line)

            if not bookers:
                slot_text = slot_item.text
                # Match names like "O'Donnell, Deborah", "mcghee, mike", "Garrett, Steve"
                # Handles apostrophes, lowercase names, and multi-part first names
                name_pattern = r"([A-Za-z][A-Za-z']+,\s*[A-Za-z][A-Za-z' ]*)"
                matches = re.findall(name_pattern, slot_text)
                # Filter out non-name matches like "Available" or "Reserve"
                for match in matches:
                    if "Available" not in match and "Reserve" not in match:
                        bookers.append(match)

            if not bookers:
                spans = slot_item.find_elements(By.TAG_NAME, "span")
                for span in spans:
                    span_text = span.text.strip()
                    if span_text and "Available" not in span_text and "Reserve" not in span_text:
                        # Match names with apostrophes and lowercase (e.g., "O'Donnell,", "mcghee,")
                        if re.match(r"^[A-Za-z][A-Za-z']+,", span_text):
                            bookers.append(span_text)

        except Exception as e:
            logger.debug(f"Error extracting bookers from slot: {e}")

        unique_bookers = list(dict.fromkeys(bookers))
        return unique_bookers

    def _format_event_block_message(self, event_blocks: list[str]) -> str | None:
        """
        Format event block names into a human-readable message suffix.

        Args:
            event_blocks: List of event names that are blocking tee times

        Returns:
            Formatted message string, or None if no events
        """
        if not event_blocks:
            return None

        if len(event_blocks) == 1:
            return f"Time blocked by event: {event_blocks[0]}"
        else:
            event_list = ", ".join(event_blocks[:3])
            if len(event_blocks) > 3:
                event_list += f" and {len(event_blocks) - 3} more"
            return f"Times blocked by events: {event_list}"

    def _extract_event_blocks(
        self,
        search_context: Any,
        target_time: time,
        fallback_window_minutes: int,
    ) -> list[str]:
        """
        Extract event/tournament block names that may be blocking tee times.

        The Walden Golf tee sheet displays events and tournaments as blocked time ranges
        with format like "08:26 AM-10:42 AM" followed by an event name such as
        "Northgate SGA 3 Man ABC - 3318".

        This method scans slot items for these blocked time ranges and extracts
        the event names to provide more informative error messages.

        Args:
            search_context: The WebDriver element to search within
            target_time: The target tee time being searched for
            fallback_window_minutes: The fallback window in minutes

        Returns:
            List of event names that overlap with the requested time window
        """
        event_names: list[str] = []
        target_minutes = target_time.hour * 60 + target_time.minute
        min_time_minutes = max(0, target_minutes - fallback_window_minutes)
        max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)

        # Pattern to match time ranges like "08:26 AM-10:42 AM" or "9:00 AM - 11:00 AM"
        time_range_pattern = re.compile(
            r"(\d{1,2}:\d{2}\s*[AaPp][Mm])\s*-\s*(\d{1,2}:\d{2}\s*[AaPp][Mm])"
        )

        try:
            slot_items = search_context.find_elements(By.CSS_SELECTOR, "li.ui-datascroller-item")

            for slot_item in slot_items:
                try:
                    slot_text = slot_item.text.strip()
                    if not slot_text:
                        continue

                    # Check if this is an event block (contains a time range)
                    time_range_match = time_range_pattern.search(slot_text)
                    if not time_range_match:
                        continue

                    # Parse the start and end times
                    start_time_str = time_range_match.group(1).upper()
                    end_time_str = time_range_match.group(2).upper()

                    start_time = None
                    end_time = None
                    for fmt in ["%I:%M %p", "%I:%M%p"]:
                        try:
                            start_time = datetime.strptime(start_time_str.strip(), fmt).time()
                            end_time = datetime.strptime(end_time_str.strip(), fmt).time()
                            break
                        except ValueError:
                            continue

                    if not start_time or not end_time:
                        continue

                    # Check if this event block overlaps with our target window
                    start_minutes = start_time.hour * 60 + start_time.minute
                    end_minutes = end_time.hour * 60 + end_time.minute

                    # Handle events spanning midnight (e.g., 11:00 PM - 1:00 AM)
                    # If end time is before start time, the event spans midnight
                    if end_minutes < start_minutes:
                        # Event spans midnight - it overlaps if:
                        # 1. Target window overlaps with the evening portion (start to midnight)
                        # 2. Target window overlaps with the morning portion (midnight to end)
                        overlaps = (
                            start_minutes <= max_time_minutes  # Evening portion overlaps
                            or end_minutes >= min_time_minutes  # Morning portion overlaps
                        )
                    else:
                        # Normal event (doesn't span midnight)
                        # Event overlaps if: event_start <= window_end AND event_end >= window_start
                        overlaps = (
                            start_minutes <= max_time_minutes and end_minutes >= min_time_minutes
                        )

                    if overlaps:
                        # Extract the event name - it's the text after the time range
                        # Remove the time range from the text to get the event name
                        event_name = slot_text[time_range_match.end() :].strip()

                        # Clean up the event name
                        # Remove leading/trailing whitespace, newlines
                        event_name = " ".join(event_name.split())

                        if event_name and event_name not in event_names:
                            logger.debug(
                                f"Found blocking event: '{event_name}' "
                                f"({start_time_str}-{end_time_str})"
                            )
                            event_names.append(event_name)

                except Exception as e:
                    logger.debug(f"Error processing slot item for event: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error extracting event blocks: {e}")

        if event_names:
            logger.info(
                f"Found {len(event_names)} event(s) blocking times in requested window: "
                f"{event_names}"
            )

        return event_names

    def _extract_blocked_slot_reasons(
        self,
        search_context: Any,
        target_time: time,
        fallback_window_minutes: int,
    ) -> list[str]:
        """
        Extract the reasons individual tee-time slots are disabled/unbookable.

        Beyond tournament time-range blocks (see _extract_event_blocks), the
        Walden Golf tee sheet renders individually disabled slots that show the
        slot time (e.g. "07:30 AM") alongside a reason heading such as
        "Aerification" or "Weather delay" and the subheading "Cannot be
        reserved". When nothing is bookable this tells the user *why* (e.g. the
        course is closed for aerification) rather than only that no open times
        were found.

        Only slots whose time falls within the target +/- fallback window are
        considered, so the reasons reflect the times the user actually asked
        for. Slots whose time cannot be parsed are still counted, so a fully
        disabled sheet is not silently dropped. When the search context is the
        whole page (no dedicated Northgate section), slots belonging to another
        course are skipped so a Walden reason never leaks into a Northgate
        failure.

        Args:
            search_context: The WebDriver element (or driver) to search within
            target_time: The target tee time being searched for
            fallback_window_minutes: The fallback window in minutes

        Returns:
            List of distinct reason strings, ordered by first appearance.
        """
        reasons: list[str] = []
        target_minutes = target_time.hour * 60 + target_time.minute
        min_time_minutes = max(0, target_minutes - fallback_window_minutes)
        max_time_minutes = min(24 * 60 - 1, target_minutes + fallback_window_minutes)

        time_pattern = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")

        try:
            slot_items = search_context.find_elements(
                By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.slot_items
            )

            for slot_item in slot_items:
                try:
                    headings = slot_item.find_elements(
                        By.CSS_SELECTOR, DOM.DISABLED_SLOT.reason_heading
                    )
                    if not headings:
                        continue  # Not a disabled slot

                    # Skip slots that positively belong to another course. The
                    # course index is embedded in child element IDs
                    # (teeTimeCourses:0 = Northgate, :1 = Walden). When the
                    # index can't be read we keep the slot (best-effort).
                    slot_html = slot_item.get_attribute("innerHTML") or ""
                    course_index = self._get_course_index_from_element_id(slot_html)
                    if course_index is not None and course_index != self.NORTHGATE_COURSE_INDEX:
                        continue

                    # Restrict to the requested window when we can read the time.
                    slot_time = None
                    labels = slot_item.find_elements(By.CSS_SELECTOR, DOM.DISABLED_SLOT.time_label)
                    for label in labels:
                        match = time_pattern.search(label.text.strip())
                        if match:
                            hour = int(match.group(1))
                            minute = int(match.group(2))
                            ampm = match.group(3).upper()
                            if ampm == "PM" and hour != 12:
                                hour += 12
                            if ampm == "AM" and hour == 12:
                                hour = 0
                            slot_time = time(hour, minute)
                            break

                    if slot_time is not None:
                        slot_minutes = slot_time.hour * 60 + slot_time.minute
                        if not (min_time_minutes <= slot_minutes <= max_time_minutes):
                            continue

                    reason = " ".join(headings[0].text.split())
                    if not reason or reason.lower() == DOM.DISABLED_SLOT.cannot_reserve_text:
                        continue
                    if reason not in reasons:
                        logger.debug(f"Found blocked slot reason: '{reason}'")
                        reasons.append(reason)

                except Exception as e:
                    logger.debug(f"Error processing slot item for blocked reason: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error extracting blocked slot reasons: {e}")

        if reasons:
            logger.info(
                f"Found {len(reasons)} blocked-slot reason(s) in requested window: {reasons}"
            )

        return reasons

    def _format_blocked_slot_message(self, reasons: list[str]) -> str | None:
        """
        Format disabled-slot reasons into a human-readable message suffix.

        Args:
            reasons: List of reason headings (e.g. ["Aerification"]) that make
                nearby slots unbookable.

        Returns:
            Formatted message string, or None if there are no reasons.
        """
        if not reasons:
            return None

        if len(reasons) == 1:
            return f"Nearby tee times are unavailable: {reasons[0]} (cannot be reserved)."

        reason_list = ", ".join(reasons[:3])
        if len(reasons) > 3:
            reason_list += f" and {len(reasons) - 3} more"
        return f"Nearby tee times are unavailable: {reason_list} (cannot be reserved)."

    def _build_unavailability_detail(
        self,
        search_context: Any,
        target_time: time,
        fallback_window_minutes: int,
    ) -> str | None:
        """
        Build a combined explanation of why nothing in the window was bookable.

        Merges tournament/event time-range blocks (_extract_event_blocks) with
        individually disabled slot reasons (_extract_blocked_slot_reasons) into
        a single suffix for the failure message. Returns None when neither
        source has anything to report.
        """
        parts: list[str] = []

        event_message = self._format_event_block_message(
            self._extract_event_blocks(search_context, target_time, fallback_window_minutes)
        )
        if event_message:
            parts.append(event_message)

        blocked_message = self._format_blocked_slot_message(
            self._extract_blocked_slot_reasons(search_context, target_time, fallback_window_minutes)
        )
        if blocked_message:
            parts.append(blocked_message)

        return " ".join(parts) if parts else None

    @with_retry(max_attempts=3, backoff_base=0.5)
    def _find_available_slots(self, search_context: Any) -> list[tuple[time, Any]]:
        """
        Find all available time slots in the tee sheet.

        The Northstar Technologies tee sheet uses a div-based layout:
        - Available slots: <span class="custom-free-slot-span">Available</span>
        - Immediate parent: <div class="ui-bar ui-bar-a custom-free-slot-div">
        - Row container: <div class="block-available"> (ancestor level ~6)
        - Time is embedded in the row container's text content (e.g., "07:46 AM")

        Returns:
            List of (time, element) tuples for available slots
        """
        available_slots: list[tuple[time, Any]] = []

        available_spans = search_context.find_elements(
            By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.available_span
        )

        if available_spans:
            logger.info(f"Found {len(available_spans)} available slot spans (div-based layout)")
            for span in available_spans:
                try:
                    row_container = self._find_row_container(span)
                    if row_container is None:
                        logger.debug("Could not find row container for slot")
                        continue

                    slot_time = self._extract_time_from_container(row_container)
                    if slot_time:
                        # Find the clickable "Available" link inside the span
                        # The span contains an <a> link with class "custom-free-slot-link"
                        clickable_element = None
                        try:
                            # Look for the Available link inside the span
                            clickable_element = span.find_element(
                                By.CSS_SELECTOR, DOM.SLOT_DISCOVERY.available_link
                            )
                        except NoSuchElementException:
                            try:
                                # Fallback: any <a> link inside the span
                                clickable_element = span.find_element(By.TAG_NAME, "a")
                            except NoSuchElementException:
                                # Last resort: use the span itself
                                clickable_element = span

                        available_slots.append((slot_time, clickable_element))
                        logger.debug(f"Found available slot at {slot_time.strftime('%I:%M %p')}")
                    else:
                        logger.debug("Could not extract time from row container")

                except (NoSuchElementException, ValueError) as e:
                    logger.debug(f"Could not parse div-based slot: {e}")
                    continue

        if not available_slots:
            logger.info("No div-based slots found, trying table-based layout fallback")
            try:
                reserve_buttons = search_context.find_elements(
                    By.XPATH,
                    ".//a[contains(text(), 'Reserve')] | .//button[contains(text(), 'Reserve')]",
                )

                for button in reserve_buttons:
                    try:
                        row = button.find_element(By.XPATH, "./ancestor::tr")
                        time_cell = row.find_element(By.CSS_SELECTOR, "td:first-child, .time-cell")
                        time_text = time_cell.text.strip()

                        slot_time = self._parse_time(time_text)
                        if slot_time:
                            available_slots.append((slot_time, button))

                    except (NoSuchElementException, ValueError) as e:
                        logger.debug(f"Could not parse table slot: {e}")
                        continue

            except NoSuchElementException:
                pass

            try:
                available_links = search_context.find_elements(
                    By.XPATH, ".//a[contains(text(), 'Available')]"
                )

                for link in available_links:
                    try:
                        row = link.find_element(By.XPATH, "./ancestor::tr")
                        time_cell = row.find_element(By.CSS_SELECTOR, "td:first-child, .time-cell")
                        time_text = time_cell.text.strip()

                        slot_time = self._parse_time(time_text)
                        if slot_time:
                            available_slots.append((slot_time, link))

                    except (NoSuchElementException, ValueError) as e:
                        logger.debug(f"Could not parse available link: {e}")
                        continue

            except NoSuchElementException:
                pass

        available_slots.sort(key=lambda x: x[0])
        logger.info(f"Total available slots found: {len(available_slots)}")
        return available_slots

    def _find_row_container(self, span: Any) -> Any | None:
        """
        Find the row container element for an available slot span.

        The DOM structure is:
        - span.custom-free-slot-span (level 0)
        - div.ui-bar.ui-bar-a.custom-free-slot-div (level 1, immediate parent)
        - ... intermediate divs ...
        - div.block-available (level ~6, the row container with time info)

        Args:
            span: The span element with class "custom-free-slot-span"

        Returns:
            The row container element, or None if not found
        """
        row_container_selectors = [
            "./ancestor::div[contains(@class, 'block-available')][1]",
            "./ancestor::div[contains(@class, 'ui-grid-a') and contains(@class, 'full-width')][1]",
            "./ancestor::div[contains(@class, 'teetime-row')][1]",
        ]

        for selector in row_container_selectors:
            try:
                container = span.find_element(By.XPATH, selector)
                return container
            except NoSuchElementException:
                continue

        try:
            current = span
            for _ in range(10):
                current = current.find_element(By.XPATH, "./..")
                text_content = current.get_attribute("textContent") or ""

                if re.search(r"\d{1,2}:\d{2}\s*[AP]M", text_content, re.IGNORECASE):
                    return current
        except (NoSuchElementException, Exception):
            pass

        return None

    def _extract_time_from_container(self, container: Any) -> time | None:
        """
        Extract the tee time from a row container element.

        The time may be in a dedicated element or embedded in the container's text.
        Uses textContent for more reliable extraction than element.text.

        Args:
            container: The row container element

        Returns:
            The parsed time, or None if extraction fails
        """
        try:
            time_selectors = [
                ".teetime-player-col-4",
                "[class*='time']",
                ".time-cell",
            ]
            for selector in time_selectors:
                try:
                    time_element = container.find_element(By.CSS_SELECTOR, selector)
                    time_text = time_element.text.strip()
                    if time_text:
                        slot_time = self._parse_time(time_text)
                        if slot_time:
                            return slot_time
                except NoSuchElementException:
                    continue
        except Exception:
            pass

        try:
            text_content = container.get_attribute("textContent") or container.text or ""

            time_match = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", text_content, re.IGNORECASE)
            if time_match:
                slot_time = self._parse_time(time_match.group(1))
                if slot_time:
                    return slot_time

            time_match_24h = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text_content)
            if time_match_24h:
                slot_time = self._parse_time(time_match_24h.group(0))
                if slot_time:
                    return slot_time
        except Exception as e:
            logger.debug(f"Error extracting time from container text: {e}")

        return None

    def _parse_time(self, time_text: str) -> time | None:
        """
        Parse a time string like '07:30 AM' or '12:42 PM' into a time object.

        Handles time range strings (e.g., '08:26 AM-10:42 AM') by returning None
        silently, as these represent tournament blocks or maintenance windows
        that are not bookable slots.
        """
        original_text = time_text
        time_text = time_text.strip().upper()

        if not time_text:
            return None

        # Check for time range patterns (e.g., "08:26 AM-10:42 AM", "09:00 AM-09:00 AM")
        # These are tournament blocks or maintenance windows, not bookable slots
        # Skip them silently without logging a warning
        if "-" in time_text and re.search(
            r"\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M", time_text
        ):
            logger.debug(f"Skipping time range string (tournament/event block): '{original_text}'")
            return None

        formats = ["%I:%M %p", "%I:%M%p", "%H:%M"]

        for fmt in formats:
            try:
                parsed = datetime.strptime(time_text, fmt)
                return parsed.time()
            except ValueError:
                continue

        logger.warning(
            f"Failed to parse time string: '{original_text}' (normalized: '{time_text}')"
        )
        return None

    def _capture_diagnostic_info(self, driver: webdriver.Chrome, context: str) -> None:
        """
        Capture diagnostic information (screenshot and page source) on failure.

        Args:
            driver: The WebDriver instance
            context: Description of what operation failed
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bucket_name = os.getenv("DEBUG_ARTIFACTS_BUCKET")

            if bucket_name:
                screenshot_bytes = driver.get_screenshot_as_png()
                html_bytes = driver.page_source.encode("utf-8", errors="replace")

                try:
                    screenshot_object = f"walden/{context}/{timestamp}/screenshot.png"
                    html_object = f"walden/{context}/{timestamp}/page.html"

                    screenshot_uri = self._upload_bytes_to_gcs(
                        bucket_name=bucket_name,
                        object_name=screenshot_object,
                        content_type="image/png",
                        data=screenshot_bytes,
                    )
                    logger.info(f"Saved debug screenshot to {screenshot_uri}")

                    html_uri = self._upload_bytes_to_gcs(
                        bucket_name=bucket_name,
                        object_name=html_object,
                        content_type="text/html; charset=utf-8",
                        data=html_bytes,
                    )
                    logger.info(f"Saved debug HTML to {html_uri}")

                except Exception as upload_error:
                    screenshot_path = f"/tmp/walden_debug_{context}_{timestamp}.png"
                    html_path = f"/tmp/walden_debug_{context}_{timestamp}.html"

                    driver.save_screenshot(screenshot_path)
                    logger.info(f"Saved debug screenshot to {screenshot_path}")

                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(driver.page_source)
                    logger.info(f"Saved debug HTML to {html_path}")

                    logger.warning(
                        f"Failed to upload diagnostic artifacts to GCS bucket '{bucket_name}': {upload_error}"
                    )
            else:
                screenshot_path = f"/tmp/walden_debug_{context}_{timestamp}.png"
                html_path = f"/tmp/walden_debug_{context}_{timestamp}.html"

                driver.save_screenshot(screenshot_path)
                logger.info(f"Saved debug screenshot to {screenshot_path}")

                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                logger.info(f"Saved debug HTML to {html_path}")
                logger.info("DEBUG_ARTIFACTS_BUCKET not set; remote artifact upload disabled")

        except Exception as e:
            logger.warning(f"Failed to capture diagnostic info: {e}")

    def _capture_refresh_artifact(self, chain_result: dict[str, Any], phase: str) -> None:
        """Record the refreshed tee sheet a failed Reserve was fired against.

        Only the direct path produces one, and only when a refresh landed. The
        excerpt this logs is the useful half: the club's "Booking Starts In"
        counter, if it was still running, sits near the top of the sheet's
        visible text and is what says the window had not opened yet.
        """
        refresh_markup = chain_result.get("refreshMarkup")
        if not refresh_markup:
            # Says which of the two it was, because "no refresh ran" and "the
            # refresh ran and returned nothing usable" call for opposite fixes.
            logger.info(
                "DIRECT_HTTP: No refreshed sheet to capture for %s - the Reserve "
                "was fired against the view staged before the window",
                phase,
            )
            return
        self._capture_response_artifact(f"direct_http_refreshed_sheet_{phase}", refresh_markup)

    def _capture_pre_window_sheet(self, driver: webdriver.Chrome) -> None:
        """Keep the tee sheet the slot finder is about to judge.

        Read once per staging run, minutes before the window, so the cost sits
        nowhere near the race - and deliberately not on the 06:30 re-scan path,
        where reading a ~600KB DOM would be spent against the clock.

        Held rather than uploaded: most mornings end in a booking and the sheet
        is worth nothing. :meth:`_flush_pre_window_sheet` sends it if one does
        not.
        """
        try:
            self._pre_window_sheet = driver.page_source
            logger.info(
                "BOOKING_DEBUG: Held the pre-window tee sheet (%d bytes) in case "
                "this morning needs explaining",
                len(self._pre_window_sheet or ""),
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not lose a booking
            self._pre_window_sheet = None
            logger.warning("BOOKING_DEBUG: Could not read the pre-window tee sheet (%s)", exc)

    def _flush_pre_window_sheet(self, context: str) -> None:
        """Upload the held sheet, if a failed morning made it worth having.

        Stored but never echoed. :meth:`_capture_response_artifact` logs the
        first 1000 characters of its markup's visible text, which on a tee sheet
        is other members' names - the same ones
        :meth:`_extract_bookers_from_slot` reads deliberately. Application logs
        are a far wider audience than the artifact bucket, so this path logs the
        size and the URI and puts the sheet itself only in storage.
        """
        sheet = self._pre_window_sheet
        if not sheet:
            logger.info(
                "BOOKING_DEBUG: No pre-window tee sheet held for %s - staging never "
                "reached slot pre-location",
                context,
            )
            return
        self._pre_window_sheet = None

        bucket_name = os.getenv("DEBUG_ARTIFACTS_BUCKET")
        if not bucket_name:
            logger.info(
                "BOOKING_DEBUG: Pre-window tee sheet for %s is %d bytes, but "
                "DEBUG_ARTIFACTS_BUCKET is unset so there is nowhere to put it",
                context,
                len(sheet),
            )
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            uri = self._upload_bytes_to_gcs(
                bucket_name=bucket_name,
                object_name=f"walden/pre_window_sheet_{context}/{timestamp}/tee_sheet.html",
                content_type="text/html; charset=utf-8",
                data=sheet.encode("utf-8", errors="replace"),
            )
            logger.info(
                "BOOKING_DEBUG: Saved the pre-window tee sheet for %s (%d bytes) to %s",
                context,
                len(sheet),
                uri,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not fail a booking
            logger.warning("BOOKING_DEBUG: Failed to upload the pre-window tee sheet: %s", exc)

    def _ledger_payloads_worth_storing(self, attempts: list[Any]) -> list[Any]:
        """The attempts whose raw payload earns its upload.

        Every refusal carries the whole re-rendered tee sheet - 500-680KB in the
        saved mornings - and a full ladder would be several megabytes uploaded
        one object at a time, each paying its own token refresh, while the next
        booking in the batch waits on it.

        Storing all of them buys little, because consecutive refusals are the
        same sheet. What a post-mortem actually reads is the shape of the two
        ends: the first refusal, the last one before the club changed its mind,
        and whatever ended the loop. Every attempt still gets a ledger row -
        those are small, and they carry the timings and the scripts.
        """
        if len(attempts) <= _RACE_LEDGER_MAX_PAYLOADS:
            return list(attempts)

        refusals = [o for o in attempts if o.verdict == RESERVE_REFUSED]
        # dict.fromkeys over attempt numbers: the ends can coincide, and the
        # order of the ledger must be preserved for the reader.
        wanted = {attempts[0].attempt, attempts[-1].attempt}
        if refusals:
            wanted.add(refusals[0].attempt)
            wanted.add(refusals[-1].attempt)
        chosen = [o for o in attempts if o.attempt in wanted][:_RACE_LEDGER_MAX_PAYLOADS]
        logger.info(
            "RACE_LEDGER: storing %d of %d raw payload(s) - attempts %s; "
            "the rest are repeats of the same sheet",
            len(chosen),
            len(attempts),
            ", ".join(str(o.attempt) for o in chosen),
        )
        return chosen

    def _capture_race_ledger(self, result: Any, target_timestamp_ms: int) -> None:
        """Store every Reserve exchange of a timed run, refused ones included.

        Five mornings were diagnosed from the *last* response alone, because it
        was the only one kept. Twice the answer was in an earlier one: the club
        had granted a slot on attempt 3 and the run reported itself blocked. The
        ledger removes that blind spot - one JSONL row per attempt, plus each
        attempt's unparsed partial-response, which carries the <eval> scripts and
        callback parameters the parser discards and no saved morning contains.

        Never raises: this is a record of a booking, not part of making one.
        """
        attempts = getattr(result, "attempt_log", None)
        if not attempts:
            return

        # The fields the race skipped, read now that nothing is racing a clock.
        backfill_reserve_telemetry(attempts)

        bucket_name = os.getenv("DEBUG_ARTIFACTS_BUCKET")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary = " | ".join(
            f"#{o.attempt} {o.slot_time.strftime('%I:%M %p') if o.slot_time else '?'} "
            f"@{o.sent_ms_past_window if o.sent_ms_past_window is not None else '?'}ms "
            f"-> {o.verdict}"
            for o in attempts
        )
        logger.info("RACE_LEDGER: %d attempt(s) - %s", len(attempts), summary)

        # The boundary, stated outright, so it is in the log even if the bucket
        # write fails. This is the number the sweep exists to produce.
        refused = [o for o in attempts if o.verdict == RESERVE_REFUSED]
        granted = next((o for o in attempts if o.verdict == RESERVE_ACCEPTED), None)
        if granted is not None and granted.sent_ms_past_window is not None:
            last_refused = max(
                (o.sent_ms_past_window for o in refused if o.sent_ms_past_window is not None),
                default=None,
            )
            logger.info(
                "RACE_LEDGER: club granted %s at +%dms past the window%s",
                granted.slot_time.strftime("%I:%M %p") if granted.slot_time else "a slot",
                granted.sent_ms_past_window,
                f"; last refusal was +{last_refused}ms" if last_refused is not None else "",
            )
        elif refused:
            # Timeouts are counted separately rather than folded in. An attempt
            # that never answered is not a refusal, so "every attempt refused"
            # would be false whenever one is present - and this line is what a
            # post-mortem reads first to decide whether the club's boundary was
            # actually probed out to the offsets the ladder reached.
            timed_out = [o for o in attempts if o.verdict == RESERVE_TIMEDOUT]
            furthest = max(
                (o.sent_ms_past_window for o in refused if o.sent_ms_past_window is not None),
                default="?",
            )
            if timed_out:
                logger.warning(
                    "RACE_LEDGER: no attempt was granted - %d refused (out to +%sms past the "
                    "window), %d never answered",
                    len(refused),
                    furthest,
                    len(timed_out),
                )
            else:
                logger.warning(
                    "RACE_LEDGER: every attempt refused, out to +%sms past the window",
                    furthest,
                )
        elif attempts:
            # Nothing granted and nothing refused, so every attempt either never
            # answered or came back unclassifiable. Those are opposite facts -
            # RESERVE_UNKNOWN means the club *did* reply and the parser could not
            # read it - and reporting them as one would send a post-mortem
            # looking for a network fault that never happened.
            never_answered = [o for o in attempts if o.verdict == RESERVE_TIMEDOUT]
            unreadable = len(attempts) - len(never_answered)
            logger.warning(
                "RACE_LEDGER: no attempt was granted or refused - %d Reserve(s) sent, "
                "%d never answered, %d answered unreadably",
                len(attempts),
                len(never_answered),
                unreadable,
            )

        if not bucket_name:
            return

        try:
            rows = []
            for observation in attempts:
                row = observation.as_row()
                row["targetTimestampMs"] = target_timestamp_ms
                rows.append(json.dumps(row, default=str))
            uri = self._upload_bytes_to_gcs(
                bucket_name=bucket_name,
                object_name=f"walden/race/{run_id}/ledger.jsonl",
                content_type="application/x-ndjson; charset=utf-8",
                data=("\n".join(rows) + "\n").encode("utf-8", errors="replace"),
            )
            logger.info("RACE_LEDGER: wrote %d row(s) to %s", len(rows), uri)
        except Exception as e:  # noqa: BLE001 - diagnostics must not fail a booking
            logger.warning(f"RACE_LEDGER: failed to write ledger rows: {e}")

        for observation in self._ledger_payloads_worth_storing(attempts):
            if not observation.raw_xml:
                continue
            try:
                self._upload_bytes_to_gcs(
                    bucket_name=bucket_name,
                    object_name=(
                        f"walden/race/{run_id}/attempt_{observation.attempt:02d}"
                        f"_{observation.verdict}.xml"
                    ),
                    content_type="application/xml; charset=utf-8",
                    data=observation.raw_xml.encode("utf-8", errors="replace"),
                )
            except Exception as e:  # noqa: BLE001 - see above
                logger.warning(
                    f"RACE_LEDGER: failed to store attempt {observation.attempt} payload: {e}"
                )

    def _capture_response_artifact(self, context: str, markup: str) -> None:
        """Record a direct-HTTP response body for diagnosis.

        _capture_diagnostic_info photographs the browser, which on this path is
        still showing the pre-booking tee sheet - it cannot explain a response
        the browser never received. The response is the only account of what the
        server did, so it is logged as a text excerpt and stored on its own.
        """
        if not markup:
            return

        try:
            excerpt = visible_text(markup)[:1000]
        except Exception as e:  # noqa: BLE001 - diagnostics must not fail a booking
            excerpt = f"<unparseable: {e}>"
        logger.error("DIRECT_HTTP: %s - response text: %r", context, excerpt)

        bucket_name = os.getenv("DEBUG_ARTIFACTS_BUCKET")
        if not bucket_name:
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            uri = self._upload_bytes_to_gcs(
                bucket_name=bucket_name,
                object_name=f"walden/{context}/{timestamp}/direct_http_response.html",
                content_type="text/html; charset=utf-8",
                data=markup.encode("utf-8", errors="replace"),
            )
            logger.info(f"Saved direct-HTTP response to {uri}")
        except Exception as e:  # noqa: BLE001 - see above
            logger.warning(f"Failed to upload direct-HTTP response artifact: {e}")

    def _upload_bytes_to_gcs(
        self, *, bucket_name: str, object_name: str, content_type: str, data: bytes
    ) -> str:
        """Upload bytes to GCS using ADC and the JSON upload API.

        Returns the gs:// URI for the uploaded object.
        """
        credentials, _ = google.auth.default(  # type: ignore[no-untyped-call]
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
        )
        credentials.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]
        token = credentials.token
        if not token:
            raise RuntimeError("Failed to obtain access token for GCS upload")

        url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o"
        params = {"uploadType": "media", "name": object_name}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, params=params, headers=headers, content=data)
            resp.raise_for_status()

        return f"gs://{bucket_name}/{object_name}"

    def _log_row_element_state(self, driver: webdriver.Chrome, row: Any, player_num: int) -> None:
        """
        Log detailed element state when TBD button detection fails.

        Captures HTML snippet and element attributes to help debug why
        the TBD button couldn't be found.

        Args:
            driver: The WebDriver instance
            row: The player row element that was being processed
            player_num: The player number (2, 3, or 4) for context
        """
        try:
            # Log current page context
            logger.debug(
                f"BOOKING_DEBUG: TBD detection failed for player {player_num}. "
                f"URL: {driver.current_url}, Title: {driver.title}"
            )

            # Log row HTML snippet (truncated to avoid log bloat)
            try:
                row_html = row.get_attribute("outerHTML")
                # Truncate to 2KB to stay within log limits
                if len(row_html) > 2000:
                    row_html = row_html[:2000] + "... [truncated]"
                logger.debug(f"BOOKING_DEBUG: Row HTML for player {player_num}: {row_html}")
            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Could not get row HTML: {e}")

            # Log summary of clickable elements in the row
            try:
                clickables = row.find_elements(
                    By.CSS_SELECTOR, "a, button, span[onclick], input, select"
                )
                element_summary = []
                for i, elem in enumerate(clickables[:10]):  # Limit to first 10
                    try:
                        elem_info = {
                            "tag": elem.tag_name,
                            "id": elem.get_attribute("id") or "",
                            "class": elem.get_attribute("class") or "",
                            "text": (elem.text or "")[:50],
                            "displayed": elem.is_displayed(),
                            "enabled": elem.is_enabled(),
                        }
                        element_summary.append(elem_info)
                    except Exception:
                        continue

                logger.debug(
                    f"BOOKING_DEBUG: Clickable elements in row {player_num}: {element_summary}"
                )
            except Exception as e:
                logger.debug(f"BOOKING_DEBUG: Could not enumerate clickables: {e}")

            # Log the player table container if we can find it
            try:
                tables = driver.find_elements(
                    By.CSS_SELECTOR, "[id*='player'], [class*='player'], table"
                )
                for table in tables[:3]:
                    table_id = table.get_attribute("id") or "no-id"
                    table_class = table.get_attribute("class") or "no-class"
                    rows = table.find_elements(By.CSS_SELECTOR, "tr")
                    logger.debug(
                        f"BOOKING_DEBUG: Table context - id='{table_id}', "
                        f"class='{table_class}', row_count={len(rows)}"
                    )
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"BOOKING_DEBUG: Error logging row element state: {e}")

    @with_retry(max_attempts=2, backoff_base=1.0)
    def _complete_booking_sync(
        self,
        driver: webdriver.Chrome,
        reserve_element: Any,
        booked_time: time,
        num_players: int,
        fallback_reason: str | None = None,
        already_clicked: bool = False,
    ) -> BookingResult:
        """
        Complete the booking by clicking Reserve, selecting player count, and confirming.

        Args:
            driver: The WebDriver instance
            reserve_element: The Reserve button/link element to click (ignored if already_clicked)
            booked_time: The time being booked
            num_players: Number of players (1-4)
            fallback_reason: Optional reason why a fallback time was used
            already_clicked: If True, the Reserve button was already clicked via JS.
                           Skip the scroll+click and go straight to player count selection.

        Returns:
            BookingResult with booking outcome
        """
        try:
            logger.info(
                f"BOOKING_DEBUG: Starting booking completion for time={booked_time}, "
                f"players={num_players}, already_clicked={already_clicked}"
            )

            wait = WebDriverWait(driver, 10)

            if not already_clicked:
                # Scroll element into view with offset to account for sticky header
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", reserve_element
                )
                self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

                wait.until(expected_conditions.element_to_be_clickable(reserve_element))

                # Use JavaScript click to bypass any overlay issues
                driver.execute_script("arguments[0].click();", reserve_element)
                logger.debug("BOOKING_DEBUG: Clicked Reserve button")

            # Check for blocked-slot popup BEFORE waiting for modal
            # This happens when another user grabs the slot at the same moment
            self.wait_strategy.simple_wait(fixed_duration=0.3, event_driven_duration=0.1)
            if self._check_slot_blocked_popup(driver):
                self._capture_diagnostic_info(driver, "slot_blocked_by_other_user")
                return BookingResult(
                    success=False,
                    error_message="Slot blocked by another user",
                    booked_time=booked_time,
                )

            # Attempt to detect the booking modal/dialog. If found, scope all
            # subsequent element searches to the modal to avoid matching elements
            # on the underlying tee sheet page (e.g., the time period filter's
            # .ui-selectonebutton matching instead of the player count buttons).
            # See Issue #105.
            booking_context: webdriver.Chrome | WebElement = driver  # default: full page
            try:
                # visibility_of_element_located only ever inspects the FIRST match,
                # and the tee sheet ships a permanently hidden golfEventDetailPopup
                # ahead of any real dialog - so the wait timed out even when the
                # booking modal was open. Check every match for visibility instead.
                visible_modals = wait.until(
                    expected_conditions.visibility_of_any_elements_located(
                        (By.CSS_SELECTOR, DOM.BOOKING_MODAL.modal_container)
                    )
                )
                booking_context = visible_modals[0]
                logger.debug(
                    "BOOKING_DEBUG: Booking dialog/modal appeared, scoping searches to modal"
                )
            except TimeoutException:
                logger.debug("BOOKING_DEBUG: No modal detected, using full page as search context")

            logger.debug(f"BOOKING_DEBUG: Selecting player count: {num_players}")
            if not self._select_player_count_sync(
                driver, num_players, search_context=booking_context
            ):
                logger.error(f"BOOKING_DEBUG: Failed to select {num_players} players")
                self._capture_diagnostic_info(driver, "player_count_selection_failed")
                return BookingResult(
                    success=False,
                    error_message=f"Failed to select {num_players} players",
                    booked_time=booked_time,
                )
            logger.debug("BOOKING_DEBUG: Player count selection successful")

            # If booking for multiple players, add TBD Registered Guests for the additional slots
            if num_players > 1:
                num_tbd_guests = num_players - 1
                logger.debug(f"BOOKING_DEBUG: Adding {num_tbd_guests} TBD Registered Guests")
                if not self._add_tbd_registered_guests_sync(
                    driver, num_tbd_guests, search_context=booking_context
                ):
                    logger.error(f"BOOKING_DEBUG: Failed to add {num_tbd_guests} TBD guests")
                    self._capture_diagnostic_info(driver, "tbd_guest_registration_failed")
                    return BookingResult(
                        success=False,
                        error_message=f"Failed to add {num_tbd_guests} TBD Registered Guests",
                        booked_time=booked_time,
                    )
                logger.debug("BOOKING_DEBUG: TBD guest registration successful")

            try:
                # Wait for the booking form to load
                logger.debug("BOOKING_DEBUG: Looking for Book Now button")
                self.wait_strategy.wait_for_element(
                    driver,
                    (
                        By.CSS_SELECTOR,
                        DOM.BOOKING_COMPLETION.book_now_wait,
                    ),
                    fixed_duration=2.0,
                    timeout=10.0,
                )

                # Scroll down to make sure the Book Now button is visible
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                self.wait_strategy.simple_wait(fixed_duration=1.0, event_driven_duration=0.2)

                # Look for "Book Now" link/button - it's an <a> element on Walden Golf
                # Try to find by ID first (most reliable), then by text content
                confirm_button = None
                try:
                    # First try to find by ID (most specific), scoped to booking context
                    confirm_button = booking_context.find_element(
                        By.CSS_SELECTOR, DOM.BOOKING_COMPLETION.book_now_by_id
                    )
                    logger.debug("BOOKING_DEBUG: Found Book Now button by ID")
                except NoSuchElementException:
                    logger.debug("BOOKING_DEBUG: Book Now button not found by ID, trying XPath")
                    # Fallback to XPath with text content
                    confirm_button = wait.until(
                        expected_conditions.element_to_be_clickable(
                            (
                                By.XPATH,
                                " | ".join(DOM.BOOKING_COMPLETION.book_now_xpaths),
                            )
                        )
                    )

                button_id = confirm_button.get_attribute("id") or "no-id"
                button_text = confirm_button.text[:50] if confirm_button.text else "no-text"
                logger.info(
                    f"BOOKING_DEBUG: Found Book Now button: id='{button_id}', text='{button_text}'"
                )

                # Scroll to the button and use JavaScript click
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", confirm_button
                )
                self.wait_strategy.simple_wait(fixed_duration=0.5, event_driven_duration=0.1)

                current_url = driver.current_url
                driver.execute_script("arguments[0].click();", confirm_button)
                logger.debug("BOOKING_DEBUG: Clicked Book Now button")

                try:
                    wait.until(expected_conditions.url_changes(current_url))
                    logger.info(
                        f"BOOKING_DEBUG: URL changed after clicking Book Now. New URL: {driver.current_url}"
                    )
                except TimeoutException:
                    logger.info(
                        "BOOKING_DEBUG: URL did not change, checking for success indicators"
                    )
                    try:
                        wait.until(
                            expected_conditions.presence_of_element_located(
                                (
                                    By.XPATH,
                                    "//*[contains(text(), 'success') or contains(text(), 'confirm') or contains(text(), 'thank')]",
                                )
                            )
                        )
                    except TimeoutException:
                        logger.debug(
                            "BOOKING_DEBUG: No success indicators found after clicking Book Now"
                        )

            except TimeoutException:
                logger.debug("BOOKING_DEBUG: No confirmation dialog found - booking may be direct")

            confirmation_number = self._extract_confirmation_number(driver)
            logger.debug(f"BOOKING_DEBUG: Extracted confirmation number: {confirmation_number}")

            logger.debug("BOOKING_DEBUG: Verifying booking success")
            if self._verify_booking_success(driver):
                logger.debug("BOOKING_DEBUG: Booking verification PASSED")
                return BookingResult(
                    success=True,
                    booked_time=booked_time,
                    confirmation_number=confirmation_number,
                    fallback_reason=fallback_reason,
                )
            else:
                logger.error("BOOKING_DEBUG: Booking verification FAILED")
                self._capture_diagnostic_info(driver, "booking_verification_failed")
                error_details = self._extract_booking_error_message(driver)
                if error_details:
                    logger.error(f"BOOKING_DEBUG: Extracted booking error text: {error_details}")
                return BookingResult(
                    success=False,
                    error_message=(
                        f"Booking may not have completed successfully"
                        f"{': ' + error_details if error_details else ''}"
                    ),
                    booked_time=booked_time,
                )

        except TimeoutException as e:
            logger.error(f"BOOKING_DEBUG: Booking confirmation timeout: {e}")
            self._capture_diagnostic_info(driver, "booking_timeout")
            return BookingResult(
                success=False,
                error_message=f"Booking confirmation timeout: {str(e)}",
            )
        except WebDriverException as e:
            logger.error(f"BOOKING_DEBUG: Booking click error: {e}")
            self._capture_diagnostic_info(driver, "booking_error")
            return BookingResult(
                success=False,
                error_message=f"Booking error: {str(e)}",
            )

    def _extract_confirmation_number(self, driver: webdriver.Chrome) -> str | None:
        """Try to extract a confirmation number from the page after booking."""
        try:
            return self._extract_confirmation_number_from_text(self._get_visible_page_text(driver))
        except Exception as e:
            logger.debug(f"Could not extract confirmation number: {e}")
            return None

    def _extract_confirmation_number_from_text(self, page_text: str) -> str | None:
        """Extract a confirmation number from post-booking page text.

        Split from the driver-reading wrapper so the direct-HTTP path can run the
        same extraction against its final partial response.
        """
        try:
            page_text_lower = page_text.lower()

            if (
                "confirmation" in page_text_lower
                or "booked" in page_text_lower
                or "reserved" in page_text_lower
            ):
                # Require at least one digit to avoid matching DOM ids/classes (e.g. "DialogDIV")
                patterns = [
                    r"confirmation[:\s#]*([A-Z0-9-]*\d[A-Z0-9-]*)",
                    r"booking[:\s#]*([A-Z0-9-]*\d[A-Z0-9-]*)",
                    r"reference[:\s#]*([A-Z0-9-]*\d[A-Z0-9-]*)",
                ]

                for pattern in patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        return match.group(1)

        except Exception as e:
            logger.debug(f"Could not extract confirmation number: {e}")

        return None

    def _verify_booking_success(self, driver: webdriver.Chrome) -> bool:
        """
        Verify that the booking was successful by checking page content.

        Returns False if verification is ambiguous - we should not assume success
        without positive confirmation.
        """
        try:
            return self._verify_booking_success_text(
                self._get_visible_page_text(driver), driver.current_url
            )
        except Exception as e:
            logger.error(f"BOOKING_DEBUG: Error verifying booking: {e}")
            return False

    def _verify_booking_success_text(self, text: str, context: str) -> bool:
        """
        Verify a booking outcome from page text.

        Split from the driver-reading wrapper so the direct-HTTP path can apply
        the same phrase checks to its final partial response, whose markup is the
        only record of that booking's outcome.

        Args:
            text: Visible text from the page or partial response
            context: Where the text came from, for logging

        Returns:
            True only on positive confirmation; ambiguity is treated as failure.
        """
        confirmed, _detail = self._booking_text_verdict(text, context)
        return bool(confirmed)

    def _member_facing_failure(
        self, *, site_message: str | None, technical: str, unchecked: bool
    ) -> str:
        """Phrase a failed direct-HTTP booking for the member who asked for it.

        When the site said why - "Member: ... is restricted for 1 round(s) on
        Northgate per Day" - that sentence is the whole answer, and the chain's
        own vocabulary (phases, partial responses, phrase checks) only buries it.
        So the site's words go to the member and the technical account goes to
        the log, where the response body and screenshots already are.

        Args:
            site_message: What the response's own message containers said, if
                anything.
            technical: The chain's account of the failure, used when the site
                said nothing.
            unchecked: True when the reservations page could not be read. The
                caveat rides along either way: "could not check" is not "not
                booked", and a member who might be holding a tee time needs to
                hear that whatever else went wrong.
        """
        if site_message:
            logger.error("DIRECT_HTTP: %s; the site said: %s", technical, site_message)
        message = site_message or technical
        if unchecked:
            message += " (the member's reservations page could not be checked)"
        return message

    def _booking_text_verdict(self, text: str, context: str) -> tuple[bool | None, str]:
        """Classify booking text as confirmed, refused, or silent.

        The three-way answer is what separates "the site said no" from "the site
        said nothing we recognize". Both are failures when the browser DOM is the
        source - the DOM would be showing a confirmation if there were one - but
        on the direct-HTTP path silence is routine, and the caller resolves it
        against the reservations page rather than reporting a loss.

        Returns:
            (True, matched success phrases) on confirmation, (False, matched
            failure phrases) on refusal, (None, reason) when neither appears.
            The detail is phrased for a user-facing error message.
        """
        try:
            logger.info(f"BOOKING_DEBUG: Verifying booking success. Source: {context}")
            page_text = text.lower()

            success_indicators = [
                "successfully",
                "confirmed",
                "booked",
                "reservation complete",
                "thank you",
                "your tee time",
            ]

            failure_indicators = [
                "error",
                "failed",
                "unavailable",
                "could not",
                "unable to",
                "already booked",
                "no longer available",
            ]

            # Check for failure indicators first
            found_failures = []
            for indicator in failure_indicators:
                if indicator in page_text:
                    found_failures.append(indicator)

            if found_failures:
                logger.error(f"BOOKING_DEBUG: Found failure indicator(s): {found_failures}")
                return False, f"the response reported: {', '.join(found_failures)}"

            # Check for success indicators
            found_successes = []
            for indicator in success_indicators:
                if indicator in page_text:
                    found_successes.append(indicator)

            if found_successes:
                logger.debug(f"BOOKING_DEBUG: Found success indicator(s): {found_successes}")
                return True, f"confirmed by: {', '.join(found_successes)}"

            logger.warning(
                f"BOOKING_DEBUG: No clear success or failure indicators found - treating as failure. "
                f"Source: {context}"
            )
            return None, "no success or failure wording in the response"

        except Exception as e:
            logger.error(f"BOOKING_DEBUG: Error verifying booking: {e}")
            return False, f"the response could not be read: {e}"

    async def get_available_times(self, target_date: date) -> list[time]:
        """
        Get all available tee times for a given date.

        This method runs the entire workflow in a background thread:
        1. Creates a new WebDriver instance
        2. Logs in to the member portal
        3. Navigates to the tee time page
        4. Retrieves available time slots
        5. Closes the WebDriver

        Args:
            target_date: The date to check availability for

        Returns:
            List of available times
        """
        return await asyncio.to_thread(self._get_available_times_sync, target_date)

    def _get_available_times_sync(self, target_date: date) -> list[time]:
        """Synchronous implementation with full driver lifecycle."""
        driver = self._create_driver()
        try:
            if not self._perform_login(driver):
                return []

            driver.get(self.TEE_TIME_URL)

            wait = WebDriverWait(driver, 15)
            wait.until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, "form")))

            self._select_course_sync(driver, self.NORTHGATE_COURSE_NAME)
            if not self._select_date_sync(driver, target_date):
                logger.error(f"Failed to select date {target_date} for availability check")
                return []

            wait.until(
                expected_conditions.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        DOM.SLOT_DISCOVERY.page_loaded,
                    )
                )
            )

            available_slots = self._find_available_slots(driver)
            return [slot_time for slot_time, _ in available_slots]

        except WebDriverException as e:
            logger.error(f"Error getting available times: {e}")
            return []
        finally:
            driver.quit()

    async def cancel_booking(self, confirmation_number: str) -> bool:
        """
        Cancel an existing booking on the Walden Golf website.

        This method navigates to the member home page where reservations are displayed,
        finds the reservation matching the confirmation number (which contains date/time info),
        and clicks the cancel button.

        The confirmation_number format is expected to be: "YYYY-MM-DD_HH:MM" (e.g., "2025-12-19_09:46")
        This allows us to identify the correct reservation by date and time.

        Args:
            confirmation_number: The booking identifier in format "YYYY-MM-DD_HH:MM"

        Returns:
            True if cancellation was successful, False otherwise
        """
        return await asyncio.to_thread(self._cancel_booking_sync, confirmation_number)

    def _cancel_booking_sync(self, confirmation_number: str) -> bool:
        """
        Synchronous cancellation implementation with full driver lifecycle.

        Creates driver, performs cancellation, and ensures cleanup in finally block.
        Includes retry logic for transient failures (slow page loads, missed clicks).
        """
        max_retries = 3
        retry_delay = 2

        driver = self._create_driver()
        try:
            if not self._perform_login(driver):
                logger.error("Failed to log in for cancellation")
                return False

            logger.info(f"Attempting to cancel booking: {confirmation_number}")

            for attempt in range(max_retries):
                try:
                    logger.info(
                        f"Navigating to member home page for reservations "
                        f"(attempt {attempt + 1}/{max_retries})..."
                    )
                    driver.get(self.DASHBOARD_URL)

                    wait = WebDriverWait(driver, 15)
                    wait.until(
                        expected_conditions.presence_of_element_located(
                            (By.CSS_SELECTOR, DOM.CANCELLATION.dashboard_presence)
                        )
                    )

                    self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)

                    result = self._find_and_cancel_reservation_sync(driver, confirmation_number)
                    if result:
                        return True

                    # If we didn't find/cancel the reservation, it might be a timing issue
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Cancellation attempt {attempt + 1} failed, "
                            f"retrying in {retry_delay} seconds..."
                        )
                        time_module.sleep(retry_delay)
                        driver.refresh()
                        continue

                    return False

                except TimeoutException as e:
                    logger.warning(f"Cancellation timeout on attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        time_module.sleep(retry_delay)
                        continue
                    logger.error(f"Cancellation failed after {max_retries} attempts")
                    return False

            return False

        except WebDriverException as e:
            logger.error(f"Cancellation WebDriver error: {e}")
            return False
        finally:
            driver.quit()

    def _find_and_cancel_reservation_sync(
        self, driver: webdriver.Chrome, confirmation_number: str
    ) -> bool:
        """
        Find and cancel a specific reservation on the member home page.

        The confirmation_number is expected to be in format "YYYY-MM-DD_HH:MM".
        We parse this to match against the reservation date and time displayed on the page.

        Args:
            driver: The WebDriver instance
            confirmation_number: The booking identifier in format "YYYY-MM-DD_HH:MM"

        Returns:
            True if cancellation was successful, False otherwise
        """
        try:
            target_date_str, target_time_str = confirmation_number.split("_")
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
            target_time = datetime.strptime(target_time_str, "%H:%M").time()

            display_date = target_date.strftime("%m/%d/%Y")
            display_time_12h = target_time.strftime("%I:%M %p").lstrip("0")

            logger.info(f"Looking for reservation on {display_date} at {display_time_12h}")
        except (ValueError, AttributeError) as e:
            logger.error(f"Invalid confirmation number format: {confirmation_number}. Error: {e}")
            return False

        try:
            reservation_rows = self._find_reservation_rows(driver)
            logger.info(f"Found {len(reservation_rows)} potential reservation rows")

            for row in reservation_rows:
                try:
                    if self._reservation_row_matches(row, target_date, target_time):
                        logger.info(f"Found matching reservation row: {row.text[:100]}...")

                        cancel_link = None
                        try:
                            cancel_link = row.find_element(
                                By.CSS_SELECTOR,
                                DOM.CANCELLATION.cancel_link,
                            )
                        except NoSuchElementException:
                            cancel_links = row.find_elements(By.TAG_NAME, "a")
                            for link in cancel_links:
                                aria_label = link.get_attribute("aria-label")
                                if aria_label and "cancel" in aria_label.lower():
                                    cancel_link = link
                                    break
                                title = link.get_attribute("title")
                                if title and "cancel" in title.lower():
                                    cancel_link = link
                                    break

                        if cancel_link:
                            logger.info("Clicking cancel button...")
                            cancel_link.click()

                            return self._confirm_cancellation_sync(
                                driver, display_date, display_time_12h
                            )
                        else:
                            logger.warning("Cancel link not found in matching row")

                except StaleElementReferenceException:
                    continue

            logger.warning(f"No matching reservation found for {confirmation_number}")
            return False

        except Exception as e:
            logger.error(f"Error finding reservation: {e}")
            return False

    def _find_reservation_rows(self, driver: webdriver.Chrome) -> list[Any]:
        """Return the rows of the member's reservations table.

        Scoped to the reservations form when it is present; the whole page is
        the fallback, since a missed row reads as "no reservation".
        """
        try:
            reservations_form = driver.find_element(
                By.CSS_SELECTOR, DOM.CANCELLATION.reservations_form
            )
            logger.info("Found reservations form, scoping search to it")
            return list(
                reservations_form.find_elements(By.CSS_SELECTOR, DOM.CANCELLATION.reservation_rows)
            )
        except NoSuchElementException:
            logger.warning("Reservations form not found, searching entire page")
            return list(driver.find_elements(By.CSS_SELECTOR, DOM.CANCELLATION.reservation_rows))

    def _reservation_row_matches(self, row: Any, target_date: date, target_time: time) -> bool:
        """Report whether a reservations-table row is this tee time.

        Both the date and the time have to match, in any of the formats the page
        has been seen to render them in.
        """
        row_text = row.text
        lowered = row_text.lower()

        if "tee time" not in lowered:
            return False

        date_variations = (
            target_date.strftime("%m/%d/%Y"),
            target_date.strftime("%m/%d/%y"),
        )
        if not any(variation in row_text for variation in date_variations):
            return False

        time_variations = (
            target_time.strftime("%I:%M %p").lstrip("0"),
            target_time.strftime("%H:%M"),
            target_time.strftime("%I:%M%p").lstrip("0"),
            target_time.strftime("%I:%M %p"),
        )
        # The hour must not be preceded by another digit. A bare substring test
        # lets a row rendering "12:08 PM" satisfy a search for "2:08 PM", which
        # would report a tee time the member never booked as held - the exact
        # false confirmation this whole check exists to rule out.
        return any(
            re.search(rf"(?<!\d){re.escape(variation)}", row_text, re.IGNORECASE)
            for variation in time_variations
        )

    def _reservation_exists(
        self,
        driver: webdriver.Chrome,
        target_date: date | None,
        booked_time: time,
    ) -> bool | None:
        """Ask the member's reservations page whether a tee time was actually booked.

        The direct-HTTP path leaves the browser on the pre-booking tee sheet, so
        when its final response is unreadable there is nothing local left to
        consult. The reservations page is the site's own answer, and it is the
        only one that can tell a booking that landed from one that did not.

        Navigating away costs the tee sheet, so this runs only on the paths that
        are already about to return - the batch loop reloads and re-selects
        course and date before the next booking either way.

        Returns:
            True when the reservation is listed, False when the page was read and
            it is not there, None when the page could not be read at all.
        """
        if target_date is None:
            logger.warning(
                "RESERVATION_CHECK: No target date available; cannot check the "
                "reservations page for %s",
                booked_time.strftime("%I:%M %p"),
            )
            return None

        try:
            logger.info(
                "RESERVATION_CHECK: Looking for %s at %s on the member's reservations page",
                target_date.strftime("%m/%d/%Y"),
                booked_time.strftime("%I:%M %p"),
            )
            driver.get(self.DASHBOARD_URL)
            WebDriverWait(driver, 15).until(
                expected_conditions.presence_of_element_located(
                    (By.CSS_SELECTOR, DOM.CANCELLATION.dashboard_presence)
                )
            )
            self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)

            for row in self._find_reservation_rows(driver):
                try:
                    if self._reservation_row_matches(row, target_date, booked_time):
                        logger.info("RESERVATION_CHECK: Reservation found - %s", row.text[:100])
                        return True
                except StaleElementReferenceException:
                    continue

            logger.info("RESERVATION_CHECK: No reservation listed for this tee time")
            return False

        except Exception as e:
            # Unknown is its own answer here: reporting "not booked" because the
            # page would not load could send the member to a slot they hold, or
            # away from one they do.
            logger.warning(f"RESERVATION_CHECK: Could not read the reservations page: {e}")
            return None

    def _confirm_cancellation_sync(
        self,
        driver: webdriver.Chrome,
        target_date: str | None = None,
        target_time: str | None = None,
    ) -> bool:
        """
        Handle any confirmation dialog that appears after clicking cancel.

        Args:
            driver: The WebDriver instance
            target_date: The date of the reservation being cancelled (for verification)
            target_time: The time of the reservation being cancelled (for verification)

        Returns:
            True if cancellation was confirmed successfully, False otherwise
        """
        try:
            self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)

            try:
                alert = driver.switch_to.alert
                logger.info(f"Alert detected: {alert.text}")
                alert.accept()
                logger.info("Alert accepted")
                self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                return self._verify_cancellation_success(driver, target_date, target_time)
            except Exception:
                pass

            for selector in DOM.CANCELLATION.confirm_css:
                try:
                    confirm_btn = driver.find_element(By.CSS_SELECTOR, selector)
                    if confirm_btn.is_displayed():
                        logger.info(f"Found confirm button with CSS selector: {selector}")
                        confirm_btn.click()
                        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                        return self._verify_cancellation_success(driver, target_date, target_time)
                except NoSuchElementException:
                    continue

            for xpath in DOM.CANCELLATION.confirm_xpaths:
                try:
                    confirm_btn = driver.find_element(By.XPATH, xpath)
                    if confirm_btn.is_displayed():
                        logger.info(f"Found confirm button with XPath: {xpath}")
                        confirm_btn.click()
                        self.wait_strategy.wait_after_action(driver, fixed_duration=1.0)
                        return self._verify_cancellation_success(driver, target_date, target_time)
                except NoSuchElementException:
                    continue

            self.wait_strategy.wait_after_action(driver, fixed_duration=2.0)

            return self._verify_cancellation_success(driver, target_date, target_time)

        except Exception as e:
            logger.error(f"Error confirming cancellation: {e}")
            return False

    def _verify_cancellation_success(
        self,
        driver: webdriver.Chrome,
        target_date: str | None = None,
        target_time: str | None = None,
    ) -> bool:
        """
        Verify that the cancellation was successful by checking page content.

        This method uses multiple verification strategies:
        1. Look for explicit success/failure messages within the reservations form
        2. If target_date and target_time are provided, verify the reservation row is gone
        3. Default to False if no positive confirmation is found (fail-safe)

        Args:
            driver: The WebDriver instance
            target_date: The date of the cancelled reservation (for row verification)
            target_time: The time of the cancelled reservation (for row verification)

        Returns:
            True if cancellation is confirmed successful, False otherwise
        """
        # First, try to find the reservations form to scope our search
        reservations_text = ""
        try:
            reservations_form = driver.find_element(
                By.CSS_SELECTOR, DOM.CANCELLATION.reservations_form
            )
            reservations_text = reservations_form.text.lower()
            logger.info("Scoped verification to reservations form")
        except NoSuchElementException:
            # Fall back to page source but log a warning
            logger.warning("Reservations form not found, using full page for verification")
            reservations_text = driver.page_source.lower()

        # Check for explicit success messages (scoped to reservations area)
        success_indicators = [
            "cancelled successfully",
            "canceled successfully",
            "reservation cancelled",
            "reservation canceled",
            "successfully cancelled",
            "successfully canceled",
        ]

        # Check for explicit failure messages
        failure_indicators = [
            "error cancelling",
            "error canceling",
            "failed to cancel",
            "unable to cancel",
            "cannot cancel",
            "cancellation failed",
        ]

        # Check for failure indicators first
        for indicator in failure_indicators:
            if indicator in reservations_text:
                logger.warning(f"Cancellation failed - found '{indicator}' in reservations area")
                return False

        # Check for success indicators
        for indicator in success_indicators:
            if indicator in reservations_text:
                logger.info(f"Cancellation confirmed - found '{indicator}' in reservations area")
                return True

        # If we have target date/time, verify the reservation row is gone
        if target_date and target_time:
            try:
                reservations_form = driver.find_element(
                    By.CSS_SELECTOR, DOM.CANCELLATION.reservations_form
                )
                rows = reservations_form.find_elements(By.CSS_SELECTOR, "table tbody tr")

                for row in rows:
                    row_text = row.text.lower()
                    if "tee time" in row_text:
                        # Check if this row matches our cancelled reservation
                        if target_date.lower() in row_text and target_time.lower() in row_text:
                            logger.warning(
                                f"Reservation row still present for {target_date} {target_time}"
                            )
                            return False

                # Row not found - reservation was removed
                logger.info(
                    f"Reservation row for {target_date} {target_time} no longer present - "
                    "cancellation confirmed"
                )
                return True

            except NoSuchElementException:
                logger.warning("Could not verify reservation removal - form not found")

        # No positive confirmation found - fail-safe: return False
        logger.warning(
            "No explicit success confirmation found and could not verify row removal - "
            "treating as failed"
        )
        return False

    async def close(self) -> None:
        """
        Close any resources.

        Note: With the refactored design, each operation manages its own WebDriver
        lifecycle, so there is nothing to clean up here. This method is kept for
        interface compatibility.
        """
        pass


class MockWaldenProvider(ReservationProvider):
    """Mock provider for testing without hitting the real booking system."""

    def __init__(self) -> None:
        """Initialize mock provider with no-op setup."""
        pass

    async def login(self) -> bool:
        return True

    async def book_tee_time(
        self,
        target_date: date,
        target_time: time,
        num_players: int,
        fallback_window_minutes: int = 32,
        tee_time_interval_minutes: int = 8,
    ) -> BookingResult:
        await asyncio.sleep(0.5)

        return BookingResult(
            success=True,
            booked_time=target_time,
            confirmation_number=f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        )

    async def book_multiple_tee_times(
        self,
        target_date: date,
        requests: list[BatchBookingRequest],
        execute_at: datetime | None = None,
    ) -> BatchBookingResult:
        results: list[BatchBookingItemResult] = []
        total_succeeded = 0

        for req in requests:
            await asyncio.sleep(0.1)
            result = BookingResult(
                success=True,
                booked_time=req.target_time,
                confirmation_number=f"MOCK-{datetime.now().strftime('%Y%m%d%H%M%S')}-{req.booking_id[:8]}",
            )
            results.append(BatchBookingItemResult(booking_id=req.booking_id, result=result))
            total_succeeded += 1

        return BatchBookingResult(
            results=results,
            total_succeeded=total_succeeded,
            total_failed=0,
        )

    async def get_available_times(self, target_date: date) -> list[time]:
        base_time = datetime.combine(target_date, datetime.min.time().replace(hour=7))
        times = []
        for i in range(20):
            slot_time = (base_time + timedelta(minutes=i * 8)).time()
            times.append(slot_time)
        return times

    async def cancel_booking(self, confirmation_number: str) -> bool:
        return True

    async def close(self) -> None:
        pass
