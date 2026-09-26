import logging
from enum import Enum

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class WaitMode(str, Enum):
    """
    Wait strategy mode for Selenium operations.

    FIXED: Use fixed sleep durations (current behavior, most reliable)
    EVENT_DRIVEN: Use WebDriverWait only, no fixed sleeps (fastest, less reliable)
    HYBRID: Use WebDriverWait + small buffer sleep (balanced approach)
    """

    FIXED = "fixed"
    EVENT_DRIVEN = "event_driven"
    HYBRID = "hybrid"


def _parse_offsets_ms(value: str, name: str, *, allow_negative: bool = False) -> tuple[int, ...]:
    """Comma-separated millisecond offsets as an ordered, deduplicated tuple.

    Shared by the sweep ladder and the opening burst, which want the same
    leniency: an unparseable piece is logged and skipped, and nothing usable
    degrades to ``(0,)`` - one send on the aim - rather than to an exception
    that would cost the morning. A negative piece is dropped unless
    ``allow_negative``: the sweep's rungs are retries after the aim, while the
    burst deliberately starts before it.
    """
    offsets: list[int] = []
    for piece in value.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            parsed = int(piece)
        except ValueError:
            logger.warning("%s: ignoring unparseable offset %r", name, piece)
            continue
        if parsed >= 0 or allow_negative:
            offsets.append(parsed)
    return tuple(sorted(dict.fromkeys(offsets))) or (0,)


def burst_plan_offsets_ms(
    *,
    start_before_aim_ms: int,
    end_after_aim_ms: int,
    dense_half_width_ms: int,
    dense_spacing_ms: int,
    sparse_spacing_ms: int,
) -> tuple[int, ...]:
    """The opening burst's members as offsets around the aim, dense near it.

    Dense members sit on multiples of ``dense_spacing_ms`` within
    ``+-dense_half_width_ms`` of the aim, so the aim itself is always one of
    them. Sparse members are stepped outward from the dense part's edges by
    ``sparse_spacing_ms``: down to ``-start_before_aim_ms`` before it and up to
    ``end_after_aim_ms`` after it. Stepping from the edges keeps the spacing
    even at the seams; it also means an extent that is not a whole number of
    sparse steps from the dense edge ends on the last step inside it.

    Lenient on purpose. A non-positive spacing is read as 1ms and a negative
    extent as 0, because a load-time error here would take the morning's race
    down with it; terraform validates the values that actually deploy.
    """
    dense = max(1, dense_spacing_ms)
    sparse = max(1, sparse_spacing_ms)
    start = max(0, start_before_aim_ms)
    end = max(0, end_after_aim_ms)
    steps = max(0, dense_half_width_ms) // dense
    offsets = {step * dense for step in range(-steps, steps + 1)}
    offset = -steps * dense - sparse
    while offset >= -start:
        offsets.add(offset)
        offset -= sparse
    offset = steps * dense + sparse
    while offset <= end:
        offsets.add(offset)
        offset += sparse
    return tuple(sorted(o for o in offsets if -start <= o <= end)) or (0,)


class Settings(BaseSettings):
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_channel: str = "whatsapp"  # "sms" or "whatsapp"

    discord_bot_token: str = ""
    discord_user_id: str = ""  # Snowflake ID of the (single) user allowed to DM the bot
    # Snowflake ID of a shared channel (e.g. #general) to post outbound
    # notifications into. When set, booking confirmations/failures go to this
    # channel (mentioning the user) instead of a private DM, so the whole
    # conversation stays in one place. Leave empty to fall back to DMs.
    discord_channel_id: str = ""

    # Telegram: the same conversation, over HTTP webhooks instead of a
    # persistent socket. Inbound updates arrive as POSTs (app/api/webhooks.py),
    # so unlike the Discord gateway this needs no always-on instance and the
    # service can scale to zero.
    #
    # The webhook is mounted whenever telegram_bot_token is set, independently
    # of messaging_channel, so Telegram can be exercised end to end while
    # Discord is still the live channel. messaging_channel only decides where
    # a conversation with no recorded channel of its own is answered.
    telegram_bot_token: str = ""
    # Comma-separated Telegram user IDs allowed to talk to the bot. Empty means
    # nobody, matching the Discord allowlist - fail closed.
    telegram_allowed_user_ids: str = ""
    # The single Telegram user ID allowed to book on another friend's behalf
    # (issue #185): "for @alex book 9/12 at 8a". Not a role any allowed user
    # can hold - exactly one ID, and it must also appear in
    # telegram_allowed_user_ids to reach the bot at all; this is an extra flag
    # on top of the allowlist, never a substitute for it. Empty (the default)
    # means nobody can proxy-book and every user books only for themselves.
    #
    # The admin deliberately has no Walden login of its own: every booking it
    # makes is attributed to a real friend, and an unresolved target fails
    # loudly rather than falling back to the shared global account. See
    # app/services/proxy_booking.py.
    telegram_admin_user_id: str = ""
    # Shared secret Telegram echoes back in X-Telegram-Bot-Api-Secret-Token.
    # This is the entire authentication story for a public webhook, so an unset
    # value means the endpoint refuses every update rather than trusting the
    # caller. Set it and setWebhook registers it; leave it unset locally and
    # inbound Telegram is simply off.
    telegram_webhook_secret: str = ""
    # Public base URL of this service (e.g. the Cloud Run URL), used to register
    # the webhook with Telegram at startup. Empty skips registration, which is
    # what local development wants - there is no public URL to register.
    telegram_webhook_base_url: str = ""

    messaging_channel: str = "twilio"  # "twilio", "discord" or "telegram"

    gemini_api_key: str = ""
    # Floating alias rather than a pinned version: a pinned model (gemini-2.0-flash)
    # was retired out from under us and every message silently mis-parsed.
    gemini_model: str = "gemini-flash-latest"

    walden_member_number: str = ""
    walden_password: str = ""
    walden_base_url: str = "https://www.waldengolf.com"

    # Symmetric key (Fernet, url-safe base64) used to encrypt per-friend Walden
    # logins at rest in the walden_credentials table - see
    # app/services/credential_crypto.py and issue #179. Generate one with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    # Unset means no per-user credential can be added or read; the single
    # global walden_member_number/walden_password above keeps working
    # regardless, since that is the fallback every requester without their own
    # row uses.
    credential_encryption_key: str = ""

    # Run the booking chain as direct PrimeFaces HTTP calls instead of browser
    # clicks. Login, navigation and slot discovery still run in Chrome; only the
    # chain itself moves to HTTP. A failure before the reservation is submitted
    # falls back to the JS chain; a failure after it is reported without a
    # browser retry, because the slot may already be held.
    #
    # On, paired with walden_fast_booking_immediate below, because the only way
    # to exercise this against the live site is to run it there. Ad-hoc
    # bookings are the safe place to find out - a lost slot on a Tuesday
    # afternoon costs nothing, a lost 6:30 race costs the tee time.
    walden_direct_http_booking: bool = True

    # Re-render the tee sheet at 6:30:00 and fire Reserve against that render,
    # instead of against the one the request was staged from ~60s earlier.
    #
    # Off, having been tried and found to buy nothing. It was built on the
    # reading that the club refuses a Reserve staged before the window for being
    # stale. On 2026-08-07 it worked perfectly - fresh sheet, countdown gone, 86
    # of 87 rows offering a Reserve - and the club refused anyway, returning the
    # same ViewState and component id the staged request already held. It cost
    # 730ms of a race decided in the first second and changed no byte of the
    # request. Kept behind the flag rather than deleted, because it is the only
    # way back if a future morning does show a stale-view refusal.
    walden_refresh_view_at_window: bool = False

    # Probe the club's clock during staging, and send the Reserve early enough
    # to *arrive* as the booking window opens rather than to leave then.
    #
    # Two things sit between us and the window and both run against us: the
    # club's clock reaches 06:30:00 before ours does, and the request still has
    # to fly there. Firing at our own 06:30:00.000 has been putting the Reserve
    # on the club's desk something like half a second into a window members have
    # been clicking into since it opened. The lead is measured, never assumed,
    # and is clamped in the booker; a failed measurement sends unled. Timed
    # bookings only - an immediate one has no instant to hit.
    walden_measure_clock_skew: bool = True

    # Whether a booking uses the fast chain (JS, or direct HTTP when the flag
    # above is on) instead of the original Selenium flow. This is deliberately
    # NOT derived from whether the booking is timed: waiting for 6:30 and going
    # fast are independent, and conflating them left the fast chain reachable
    # only from the scheduled batch job. See issue #124.
    #
    # Batch (the 6:30 race) defaults on - that is what it was built for.
    walden_fast_booking_batch: bool = True
    # Ad-hoc bookings (a date inside the 7-day window, booked on the spot) are
    # on so the chain gets exercised off-race, where losing the slot costs
    # nothing. Set this and walden_direct_http_booking to false together to put
    # ad-hoc bookings back on the original Selenium flow.
    walden_fast_booking_immediate: bool = True

    # Seconds an ad-hoc booking waits before firing Reserve, instead of firing
    # as soon as the sheet is staged.
    #
    # This exists to make ad-hoc bookings run the *same* code as the 6:30 race.
    # Every timed-path behaviour - slot pre-location, clock-skew probing, the
    # precision wait, and above all a session that has sat idle since it was
    # staged - is gated on `execute_at` being set, not on the hour being 06:30.
    # Firing ad-hoc immediately meant the machinery that decides the only
    # booking that matters was exercised once a day, unobserved, against slots
    # a dozen members were racing us for.
    #
    # With a delay set, a Tuesday-afternoon booking nobody is competing for
    # runs the whole race path. A refusal there cannot be another member, so it
    # is a refusal we caused - which is the thing five straight lost mornings
    # could not distinguish. 90s brackets the ~68s the 6:30 job stages ahead;
    # sweep it (15/60/120/300) to find out whether failures track the wait.
    #
    # 0 restores the old fire-immediately behaviour.
    walden_adhoc_execute_delay_s: int = 90

    # After an ad-hoc booking is refused on the timed path above, re-attempt it
    # on the untimed one - a fresh session firing Reserve straight away, which
    # is exactly what ad-hoc bookings did before the delay existed.
    #
    # Two jobs. It keeps ad-hoc bookings working while the timed path is under
    # suspicion, and it turns every one of them into a controlled pair: same
    # tee time, same day, same code, minutes apart, differing only in the wait.
    # A timed refusal followed by an untimed success is the comparison that
    # settles what the club's "blocked by another user" actually means.
    #
    # Only ever attempted for a result that carries `verified_not_reserved`, so
    # a Reserve whose outcome is unknown is never sent twice.
    walden_adhoc_untimed_retry: bool = True

    # When the tee sheet actually opens, as milliseconds past the club's stated
    # 06:30:00. This is a claim about the club, and the one tomorrow tests.
    #
    # Every refusal on record arrived under +1000ms (-60, -14, -7, 0, 0, 812,
    # 817) and every grant over +1200ms (1239, 1240, 1291). On 2026-08-15 the
    # refusal carried a Date header stamped inside the 06:30:00 second and the
    # grant one inside 06:30:01, and the clock probe agreed with the application
    # server to within that header's one-second resolution. So the club is not
    # running on a clock we misread: it refuses while its own clock still reads
    # 06:30:00, and the sheet is open from 06:30:01.
    #
    # Everything is timed from here. The sweep offsets below are past *this*
    # instant, not past 06:30:00 - so a run aims at the moment we believe the
    # sheet opens and retries from there, rather than searching for it.
    #
    # Reporting deliberately stays in the 06:30:00 frame: the ledger's
    # sentMsPastWindow and serverMsPastWindow are still measured from the club's
    # stated window, so tomorrow's numbers line up with the ten data points above
    # rather than starting a second, incompatible scale.
    #
    # This is the gate the opening burst is centred on. Since 2026-09-25 every
    # race brackets where the gate really opened (GATE_BRACKET in the log, and
    # `scripts/fetch_debug_artifacts.py gate` across mornings), and the terraform
    # default is where to move it once the brackets settle.
    #
    # 0 restores the historical behaviour of treating 06:30:00 as the open.
    walden_window_opens_offset_ms: int = 1000

    # How far past the gate above to aim, so the aim is gate + margin.
    #
    # Kept separate from the offset above because the two are tuned for different
    # reasons: that one is what we believe about the club, and this one only puts
    # the aim just after it. Folding them into one number would leave a refusal
    # at the aim point ambiguous between "move the belief" and "widen the slack".
    #
    # 5 since 2026-09-25, making the aim +1005 against a +1000 gate. The margin
    # is not what covers the probe's error any more: the burst's dense part
    # (walden_burst_dense_half_width_ms) puts a member every 5ms within +-30ms
    # of the aim. History worth keeping: #173 set this file to 0 on 2026-09-04,
    # but terraform's default stayed 30 and terraform is what deploys, so every
    # race from then until 2026-09-25 aimed at +1030. Keep the two defaults equal.
    walden_reserve_aim_margin_ms: int = 5

    # Milliseconds past the open (above) to ask for the target slot at, before
    # any fallback tee time is tried. Comma-separated; see
    # walden_sweep_offsets_ms().
    #
    # These are retries now, not a search. 0 is the aim point - the instant we
    # believe the sheet opens - and the rest exist to catch the hypothesis being
    # wrong. A grant at 0 confirms it; a grant at 250 or 1000 says the boundary
    # is later than the tick and the offset above should move.
    #
    # 250 is fired without waiting for 0's answer (see
    # walden_reserve_pipeline_opening_pair), which puts it near +1280ms in the
    # old frame - close to the +1239/1240/1291 that have actually been granted.
    # So a wrong hypothesis costs a rung rather than the morning.
    #
    # 1000 is the last ask before the fallback list. It lands around +2030ms in
    # the old frame, past every grant on record.
    #
    # Spacing is bounded by how fast the club answers, not by what is written
    # here: a rung is reached only once the previous answer lands, and a Reserve
    # round trip has measured 593-828ms. A rung our own latency has just
    # overshot still fires - see _RUNG_LATE_GRACE_MS - but one spaced tighter
    # than a round trip will not fire at the instant it names.
    #
    # "0" restores the historical single ask.
    walden_reserve_sweep_offsets_ms: str = "0,250,1000"

    # Keep asking for the target slot while the club renders its sheet closed.
    #
    # The sweep above was built for a boundary that sits at 06:30:01. On the
    # two Friday races on record (2026-08-21 and 08-28) it sat at :03-:05, and
    # the ladder spent every ask for the target into a provably closed sheet -
    # the club's own disable-div marker was on each refusal - then walked off to
    # the fallback list at :03, two seconds before the club granted anything to
    # anyone. The member who got the target both Fridays did not have to be
    # faster than the bot; he only had to still be asking when the sheet opened.
    #
    # Under this policy a refusal whose response renders the sheet closed does
    # not count against the target at all: the same slot is asked for again
    # immediately, paced by nothing but the club's own answer rate (~300-500ms a
    # round trip). The first refusal that arrives on an open sheet ends the hold
    # and starts the fallback walk.
    #
    # One execution path, race and ad-hoc alike. An ad-hoc booking fires into a
    # window that opened days ago, so its sheet is already open and the hold
    # naturally has nothing to do - but it runs the same loop, for the same
    # reason walden_adhoc_execute_delay_s exists: a Tuesday-afternoon booking
    # nobody is racing for is the only place this code gets exercised before
    # the morning it decides.
    walden_reserve_hold_until_open: bool = True

    # How long past the stated window to keep holding for the sheet to open,
    # before conceding it is not going to and walking the fallback list anyway.
    #
    # Measured from the stated 06:30:00, not the aim. The latest open on record
    # is ~+3s (2026-08-28), drifting roughly a second later per week; 8s covers
    # that with margin while leaving room inside the 10s reserve deadline for
    # the fallback walk if the sheet never opens. A sheet still closed at +8s is
    # a morning something else is wrong on.
    walden_reserve_hold_cap_ms: int = 8000

    # Once the sheet is open, re-ask the target slot between fallback attempts
    # (target, fallback 1, target, fallback 2, ...) instead of abandoning it on
    # its first open-sheet refusal.
    #
    # Both Fridays the first grant to anyone came at :05 while the sheet showed
    # open from :03 - so an open-sheet refusal of the target is not yet proof
    # the slot is taken, and leaving it on that evidence hands it to whoever is
    # still asking at :05. A re-ask costs one round trip of fallback delay and
    # nothing else: same-slot repeats never consume the attempt budget.
    walden_reserve_target_interleave: bool = True

    # Fire the first two rungs without waiting for the first one's answer.
    #
    # Serialised, the ladder cannot ask twice inside one round trip: 08-15 fired
    # at -60ms, got its refusal back at +940ms, and by then the 900 rung was gone
    # so the next question went at +1240ms. Aiming at the open without this would
    # put the retry near +1780ms in the old frame - later than the offset that has
    # won three times - so a wrong hypothesis would cost the morning rather than
    # one rung.
    #
    # Pipelined, the pair brackets the open in one round trip and the run is no
    # worse off than the +1240ms that has been winning. Both requests are for the
    # *same* slot, so the second cannot reserve a second tee time and collide
    # with the one-round-per-day rule; the worst case is that the club grants the
    # same hold twice.
    #
    # Off by default, deliberately, and not because it is thought wrong.
    #
    # Two reasons. Serially the ladder now asks at roughly +1030, +1770 and
    # +2510ms from the stated window - every one of them past all seven refusals
    # on record, and the last two past every grant - so pipelining buys an ask at
    # +1280 instead of +1770 and only pays on a morning where the hypothesis is
    # wrong *and* the slot is contested.
    #
    # Against that, it is untested against the club, and the sequence it creates
    # - a Reserve granted while a second one for the same slot is already in
    # flight - has never been observed. That was an occasional case when the
    # first rung was a guess. Now that rung 0 is the hypothesis, a right
    # hypothesis means rung 0 wins every morning, which makes the untested
    # sequence the daily case rather than the rare one.
    #
    # And the morning it would first run is the morning testing whether the sheet
    # opens at 06:30:01. Two new variables at once makes a failure unreadable.
    # Exercise this with an ad-hoc booking - same timed path, nothing at stake,
    # and rung 0 always wins there - before letting it near the race.
    walden_reserve_pipeline_opening_pair: bool = False

    # How the opening of the window is asked: "burst" or "ladder".
    #
    # "ladder" is everything above exactly as it ran through 2026-09-04: the
    # sweep rungs, the opening pair, the hold-until-open policy and the target
    # interleave, one request in flight at a time, each rung reached only once
    # the previous answer lands. It stays reachable by this switch so that a
    # burst that misbehaves on the ad-hoc test can be rolled back by setting
    # WALDEN_RESERVE_OPENING_MODE=ladder on the service, with no code change.
    #
    # "burst" replaces the sweep, the pair and the hold with a pipelined
    # opening: the members of the burst plan (see walden_burst_offsets_ms()) are
    # sent at their instants *without waiting for answers*, so requests keep
    # landing on the club throughout the span the gate is believed to open in.
    # The first grant wins; members not yet sent when it lands are skipped;
    # the serial fallback walk continues after the burst if nothing was
    # granted. Why: the ladder's cadence was one round trip plus a parse -
    # 700-1030ms between asks - and every Friday's target was gone before the
    # second ask. Under a crowd whose retries land every second or so, or a
    # gate that opens somewhere in a two-second span, one ask per 750ms is
    # not in the race. See operations/race-reports/2026-09-04.md.
    #
    # The fallback list is *not* replaced: by default the burst asks nothing
    # but the target (see walden_burst_target_only()) and the whole list is
    # walked serially afterwards, out to the deadline, one request in flight at
    # a time - as before, and as the burst itself falls back to when nothing
    # inside it was granted.
    #
    # Every day, not Fridays only. The path the race runs has to be the path
    # every ad-hoc booking runs, or it is untested until the morning it counts.
    walden_reserve_opening_mode: str = "burst"

    # The opening burst's shape, in milliseconds around the aim (the gate plus
    # the margin above: +1005 by default). See burst_plan_offsets_ms().
    #
    # One member every walden_burst_dense_spacing_ms within
    # +-walden_burst_dense_half_width_ms of the aim, and one every
    # walden_burst_sparse_spacing_ms outside that, from
    # walden_burst_start_before_aim_ms before the aim to
    # walden_burst_end_after_aim_ms after it. With the defaults and a +1005 aim,
    # in ms past 06:30:00:
    #
    #   815, 835, ... 955       every 20ms   8 members
    #   975, 980, ... 1035      every 5ms   13 members
    #   1055, 1075, ... 1175    every 20ms   7 members
    #
    # 28 members. The sparse steps are laid outward from the dense part, so the
    # last member is +1175: one more 20ms step would pass +1185.
    #
    # Why this shape, since 2026-09-25. Nothing had ever been written to the wire
    # between +817 (refused, 08-12) and about +1014 (granted, 09-18), so where
    # inside that span the gate opens had never been measured. The sparse part
    # before the aim finds it, every weekday alike; the dense part is the race,
    # so that wherever in +975..+1035 the gate falls, some member arrives within
    # 5ms after it; the sparse part after the aim covers a later gate. Every
    # member asks for the target, and the serial fallback walk runs after the
    # burst as before. See operations/design-gate-burst.md.
    walden_burst_start_before_aim_ms: int = 190
    walden_burst_end_after_aim_ms: int = 180
    walden_burst_dense_half_width_ms: int = 30
    walden_burst_dense_spacing_ms: int = 5
    walden_burst_sparse_spacing_ms: int = 20

    # An explicit burst plan as comma-separated ms around the aim (negative is
    # before it), overriding the shape above. Empty, the default, means "use
    # the shape". For an experiment the shape cannot express; the terraform
    # defaults tune the shape, not this.
    walden_reserve_burst_offsets_ms: str = ""

    # Open one connection per burst member shortly before the burst, so that no
    # member pays a TCP and TLS handshake at its own instant.
    #
    # Until 2026-09-25 every member dialled at fire time. httpx drops a pooled
    # connection after 5s idle, and staging's last request came 66-95s before
    # the window, so each ask reached the wire ~55ms after its logged send. The
    # one morning the first ask rode a still-open connection - 2026-09-18, when
    # staging ran late - is the only burst-era Friday that won 08:38.
    walden_burst_prewarm_connections: bool = True

    # How many members from the front of the burst ask for the target alone.
    #
    # Unset (the default) couples this to the burst plan's own length, so the
    # burst asks nothing but the target regardless of how
    # walden_reserve_burst_offsets_ms is configured - see
    # walden_burst_target_only() and
    # operations/race-reports/2026-09-04-evening.md. A copy of the offset
    # count hard-coded here instead would silently drift the moment someone
    # lengthens the offsets list past it, quietly reintroducing the fallback
    # interleave this default exists to remove.
    #
    # Set explicitly (WALDEN_RESERVE_BURST_TARGET_ONLY) to opt back into
    # interleaving fallback and target - F1, T, F2, T, ... - which is how this
    # was first shipped, on the theory that a target gone from the first ask
    # should not cost the uncontested neighbour beside it. The 2026-09-04
    # evening ad-hoc test - the test this mode was explicitly built to need
    # before a race - ran that plan and found the failure mode it was built to
    # answer: two members shared one PrimeFaces ViewState, the target (05:06 PM)
    # was granted first and adopted as the win, a later-arriving fallback grant
    # (04:58 PM) landed under the same ViewState, and the club's own reservation
    # record ended up anchored to the fallback - the sheet showed 04:58 PM
    # reserved and 05:06 PM open, while the chain reported success for
    # 05:06 PM. A fallback interleaved into the burst is not a free hedge; it
    # can overwrite which slot the club actually finalizes. Until that is fixed
    # at the session level, the burst asks only the target, and the fallback
    # list is walked serially afterwards - one request in flight at a time, the
    # ladder's own contract - exactly as it already does when nothing is
    # granted inside the burst.
    walden_reserve_burst_target_only: int | None = None

    # Write the per-attempt race ledger to the debug artifacts bucket.
    #
    # Only the *final* Reserve response was ever kept, and on both mornings the
    # club actually granted a slot the evidence was in an earlier one. This
    # stores every attempt's raw partial-response - including the <eval> scripts
    # and callback parameters the parser used to discard, which is where a shown
    # dialog is expected to differ from a re-rendered one.
    walden_capture_race_ledger: bool = True

    # Clear the resident Chrome page's JS timers once a Reserve has been granted.
    #
    # The browser stays parked on the pre-window tee sheet through the whole
    # race - live countdown and datascroller timers included - and 2026-08-28
    # attempt 2 caught the cost on the new counters: cpu/wall 0.39 with 310ms of
    # container CPU burned by a process that was not ours, on the machine that
    # was mid-race. A side thread clears the page's timers when the booker
    # raises its signal; nothing runs on the race thread.
    #
    # The signal moved on 2026-09-04, from "first Reserve answered" to "a
    # Reserve was granted", and the sweep no longer calls the club's own
    # stopSheetTimers(). The first Friday this ran, the sweep fired at +1.6s on
    # a refusal, and every one of the fourteen responses that followed was
    # byte-identical to the pre-window render - countdown still reading
    # 00:01:20 at +10.7s. Those page timers are what had been advancing the
    # server-side view our requests are evaluated against (the two Fridays
    # before it, with the timers alive, the view changed mid-race); with them
    # dead on a morning that needed a retry, the hold policy read our own
    # frozen render as "sheet still closed" for the rest of the race. After a
    # grant the chain advances the view itself, so quieting then costs nothing.
    walden_quiet_browser_during_race: bool = True

    # Photograph the live tee sheet right after a race, names and all.
    #
    # Both Friday losses were diagnosed blind on this point: a refusal's slot
    # rows are echoed pre-window chrome (established 08-21), so nothing the
    # race stores says who actually holds the slot it lost. The member's own
    # screenshots are what established that the same foursome held 08:38 on
    # both Fridays. This re-renders the sheet minutes after the race and stores
    # HTML and screenshot beside the ledger - post-race, driver-closing time,
    # nowhere near the critical path.
    walden_capture_postrace_sheet: bool = True

    user_phone_number: str = ""

    database_url: str = "sqlite+aiosqlite:///./teetime.db"

    timezone: str = "America/Chicago"
    booking_open_hour: int = 6
    booking_open_minute: int = 30
    days_in_advance: int = 7
    max_tee_times_per_day: int = 2

    scheduler_api_key: str = ""
    scheduler_service_account: str = ""
    oidc_audience: str = ""  # Expected OIDC audience (Cloud Run service URL)

    # Logging configuration
    log_level: str = "INFO"  # Set to "DEBUG" to see BOOKING_DEBUG messages in GCP Cloud Logs

    # Wait strategy for Selenium operations (fixed, event_driven, hybrid)
    wait_mode: WaitMode = WaitMode.FIXED

    # --- The observer job (app/observer/, issue #189) ---------------------
    #
    # A separate Cloud Run job that photographs the tee sheet once a second
    # across the window and never reserves anything. It exists because two
    # incompatible explanations - a late gate, or a faster rival - fit every
    # artifact the racer can produce, and only an independent reader of the
    # sheet separates them. These settings are read by that job alone; none of
    # them is on the booking path.
    #
    # Defaulted on, and wired into terraform/ as env on the observer job: a
    # flag defaulted off is a flag that never runs, which is how
    # WALDEN_DIRECT_HTTP_BOOKING sat dead in production for months.
    observer_enabled: bool = True

    # Whose due booking tells the observer which date to watch. Empty falls
    # back to user_phone_number, and if that is empty too the earliest due
    # booking of the morning is used. When the chosen requester has nothing due
    # the observer still watches today + days_in_advance, because a non-Friday
    # morning with no booking is still a free control group.
    observer_phone_number: str = ""

    # Nine snapshots at -0.5s, +0.5s, +1.5s..+7.5s. The window is decided inside
    # three seconds and the first grant to anyone has never been seen later than
    # club :06, so this span covers the contested range with room either side.
    #
    # The -500ms start offset (rather than 0) puts one control shot just before
    # the window and moves the first post-window shot half a second earlier than
    # a plain +0s start would - the two together are what can show a slot already
    # gone at +0.5s that was still there at -0.5s, which a same-day comparison
    # against every other weekday can attribute to a specific morning rather than
    # to the observer's own timing. Applies to every morning, not only Fridays:
    # the whole point is a same-mechanism baseline to compare a contested morning
    # against.
    observer_snapshot_count: int = 9
    observer_snapshot_interval_ms: int = 1000
    observer_snapshot_start_offset_ms: int = -500

    @field_validator("observer_snapshot_count", "observer_snapshot_interval_ms")
    @classmethod
    def _validate_observer_cadence(cls, v: int, info: ValidationInfo) -> int:
        """Reject a snapshot cadence that would quietly produce nothing useful.

        Both failure modes are silent, which is why they are worth a load-time
        error rather than a comment. A count of 0 or less makes ``range(count)``
        empty, so the run captures nothing; an interval of 0 or less leaves every
        planned offset at or below zero, so all nine snapshots fire in a burst at
        the window and the run *looks* fine while recording one instant instead
        of nine. Neither is discoverable until someone reads the artifacts, by
        which point the morning is spent.
        """
        if v < 1:
            raise ValueError(
                f"{info.field_name} must be at least 1, got {v}. "
                "A non-positive count captures no snapshots, and a non-positive "
                "interval collapses all of them onto the window instant."
            )
        return v

    @field_validator("discord_channel_id")
    @classmethod
    def _validate_discord_channel_id(cls, v: str) -> str:
        """Reject a non-numeric DISCORD_CHANNEL_ID at load time.

        The value is interpolated straight into /channels/{id}/messages, so a
        channel *name* like "#general" would only fail later at send time. Trim
        whitespace and require a numeric snowflake; empty stays valid and means
        "fall back to DMs".
        """
        v = v.strip()
        if v and not v.isdigit():
            raise ValueError(
                "DISCORD_CHANNEL_ID must be a numeric Discord channel ID (snowflake); "
                f"got {v!r}. In Discord, enable Developer Mode, then right-click the "
                "channel and choose Copy Channel ID. Leave it unset to use DMs."
            )
        return v

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _validate_telegram_allowed_user_ids(cls, v: str) -> str:
        """Reject a TELEGRAM_ALLOWED_USER_IDS that is not numeric IDs at load time.

        This is the allowlist; a value that silently parses to nothing would
        fail closed and leave the bot mute with no obvious cause. A Telegram
        @username is the likely mistake, and it is not an ID - reject it here
        where the message can say so.
        """
        v = v.strip()
        for piece in v.split(","):
            piece = piece.strip()
            if piece and not piece.isdigit():
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric Telegram "
                    f"user IDs; got {piece!r}. A @username is not an ID - message "
                    "@userinfobot in Telegram to get yours. Leave it unset to allow no one."
                )
        return v

    @field_validator("telegram_admin_user_id")
    @classmethod
    def _validate_telegram_admin_user_id(cls, v: str) -> str:
        """Reject a TELEGRAM_ADMIN_USER_ID that is not a single numeric ID.

        Same reasoning as the allowlist above, plus one of its own: this value
        decides who may book under someone else's Walden account, so a typo
        that silently parses to nothing must be caught at load time rather
        than discovered as "the bot ignored my 'for @alex'".
        """
        v = v.strip()
        if not v:
            return v
        if not v.isdigit():
            raise ValueError(
                "TELEGRAM_ADMIN_USER_ID must be a single numeric Telegram user ID; "
                f"got {v!r}. A @username is not an ID - message @userinfobot in "
                "Telegram to get yours. Leave it unset to disable proxy booking."
            )
        return v

    @model_validator(mode="after")
    def _warn_if_admin_not_allowlisted(self) -> "Settings":
        """Warn when the proxy admin cannot actually reach the bot.

        The allowlist is checked first on every inbound update, so an admin ID
        missing from it is not a smaller problem than a wrong ID - it is the
        same problem, and its symptom is total silence. Warned rather than
        raised: the allowlist is loaded from a secret whose value this process
        may legitimately not control, and a hard failure here would take the
        6:30 booking run down over a feature it does not use.
        """
        admin = self.telegram_admin_user_id.strip()
        if admin and admin not in self.telegram_allowed_ids():
            logger.warning(
                "TELEGRAM_ADMIN_USER_ID (%s) is not in TELEGRAM_ALLOWED_USER_IDS; the admin "
                "cannot talk to the bot at all, so proxy booking is effectively off. "
                "Add the ID to the allowlist as well.",
                admin,
            )
        return self

    def telegram_allowed_ids(self) -> frozenset[str]:
        """The Telegram allowlist as a set of IDs, empty when unset."""
        return frozenset(
            piece.strip() for piece in self.telegram_allowed_user_ids.split(",") if piece.strip()
        )

    def telegram_admin_id(self) -> str | None:
        """The single proxy-booking admin's Telegram ID, or None when unset."""
        return self.telegram_admin_user_id.strip() or None

    def walden_sweep_offsets_ms(self) -> tuple[int, ...]:
        """The sweep ladder as ordered, deduplicated, non-negative offsets.

        Parsed leniently and never allowed to fail a booking: a malformed value
        degrades to the historical single shot on the instant rather than
        stopping the morning. Negative offsets are dropped - arriving before the
        window is the one thing five mornings of evidence says does not work.
        """
        return _parse_offsets_ms(
            self.walden_reserve_sweep_offsets_ms, "WALDEN_RESERVE_SWEEP_OFFSETS_MS"
        )

    def walden_burst_offsets_ms(self) -> tuple[int, ...]:
        """The opening burst's members around the aim, ordered and deduplicated.

        The explicit WALDEN_RESERVE_BURST_OFFSETS_MS when one is set, else the
        plan the shape settings describe. Negative offsets are before the aim
        and are kept: unlike the sweep, the burst is meant to start before the
        gate. Same leniency as the sweep - a malformed override degrades to a
        single send on the aim rather than losing the morning.
        """
        if self.walden_reserve_burst_offsets_ms.strip():
            return _parse_offsets_ms(
                self.walden_reserve_burst_offsets_ms,
                "WALDEN_RESERVE_BURST_OFFSETS_MS",
                allow_negative=True,
            )
        return burst_plan_offsets_ms(
            start_before_aim_ms=self.walden_burst_start_before_aim_ms,
            end_after_aim_ms=self.walden_burst_end_after_aim_ms,
            dense_half_width_ms=self.walden_burst_dense_half_width_ms,
            dense_spacing_ms=self.walden_burst_dense_spacing_ms,
            sparse_spacing_ms=self.walden_burst_sparse_spacing_ms,
        )

    def walden_burst_target_only(self) -> int:
        """How many burst members ask for the target alone, coupled to the plan.

        Unset (the default) is the whole burst plan's own length, so lengthening
        walden_reserve_burst_offsets_ms can never quietly reintroduce a fallback
        interleave - a hard-coded copy of the offset count would drift the
        moment the two were edited separately. An explicit
        WALDEN_RESERVE_BURST_TARGET_ONLY opts back into interleaving fallback
        and target past that many members.
        """
        if self.walden_reserve_burst_target_only is not None:
            return max(1, self.walden_reserve_burst_target_only)
        return len(self.walden_burst_offsets_ms())

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # .env may carry keys this branch doesn't know about (e.g. settings
        # introduced on another branch); ignore them instead of crashing.
        extra = "ignore"


settings = Settings()
