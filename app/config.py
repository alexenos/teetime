import logging
from enum import Enum

from pydantic import field_validator
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


def _parse_offsets_ms(value: str, name: str) -> tuple[int, ...]:
    """Comma-separated millisecond offsets as an ordered, deduplicated tuple.

    Shared by the sweep ladder and the opening burst, which want the same
    leniency: an unparseable piece is logged and skipped, a negative one is
    dropped, and nothing usable degrades to ``(0,)`` - one send on the aim -
    rather than to an exception that would cost the morning.
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
        if parsed >= 0:
            offsets.append(parsed)
    return tuple(sorted(dict.fromkeys(offsets))) or (0,)


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
    # 0 restores the historical behaviour of treating 06:30:00 as the open.
    walden_window_opens_offset_ms: int = 1000

    # Slack added to the aim, for measurement error rather than for the club.
    #
    # Kept separate from the offset above because the two are tuned for different
    # reasons: that one is what we believe about the club, this one is how much we
    # distrust our own clock probe. Folding them into one number would leave a
    # refusal at the aim point ambiguous between "move the belief" and "widen
    # the slack".
    #
    # 0 since 2026-09-04. It was 30, on the reasoning that the probe pins the
    # club's tick to roughly +-22ms and arriving early lands inside the second
    # that has never been granted. That protects against a cost that does not
    # exist - an early ask is one free refusal - and on a contested slot the
    # 30ms is the whole loss: every Friday first ask on record went at
    # +1005..+1026ms, club clock :01, and was refused, while the identical ask
    # at the identical club-second was accepted every other day. Under the
    # opening burst (walden_reserve_opening_mode) the first send is aimed at the
    # tick itself and the members behind it cover the probe's error.
    walden_reserve_aim_margin_ms: int = 0

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
    # opening: the requests in walden_reserve_burst_offsets_ms are sent at
    # their instants *without waiting for answers*, so a request lands on the
    # club every hundred-odd milliseconds through the window's first seconds.
    # The first grant wins; members not yet sent when it lands are skipped;
    # the serial fallback walk continues after the burst if nothing was
    # granted. Why: the ladder's cadence was one round trip plus a parse -
    # 700-1030ms between asks - and every Friday's target was gone before the
    # second ask. Under a crowd whose retries land every second or so, or a
    # gate that opens somewhere in a two-second span, one ask per 750ms is
    # not in the race. See docs/booking-post-mortem-2026-09-04.md.
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

    # Instants past the aim to send the burst's members at, comma-separated ms.
    #
    # The aim is the club's tick (walden_window_opens_offset_ms), so 0 is
    # :01.000 and 2600 is :03.600. Dense for the first half-second, because the
    # probe brackets the tick to +-22ms and the first member can land a hair
    # early; then every 200-400ms out past the latest instant at which the club
    # has rendered its sheet closed to us on a Friday (:02.8 on 08-28). A member
    # is one POST of ~1.8KB and one ~670KB refusal back; twelve of them over
    # 2.6s is three or four in flight at once, which the pair had already
    # exercised at two. Whether the club tolerates that many is the ad-hoc test
    # this mode must pass before a race - see the module docstring.
    walden_reserve_burst_offsets_ms: str = "0,100,220,370,520,700,900,1150,1450,1800,2200,2600"

    # How many members from the front of the burst ask for the target alone.
    #
    # Unset (the default) couples this to the burst plan's own length, so the
    # burst asks nothing but the target regardless of how
    # walden_reserve_burst_offsets_ms is configured - see
    # walden_burst_target_only() and
    # docs/booking-post-mortem-2026-09-04-evening.md. A copy of the offset
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

    def telegram_allowed_ids(self) -> frozenset[str]:
        """The Telegram allowlist as a set of IDs, empty when unset."""
        return frozenset(
            piece.strip() for piece in self.telegram_allowed_user_ids.split(",") if piece.strip()
        )

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
        """The opening burst's send instants, ordered and deduplicated.

        Same leniency as the sweep: a malformed value degrades to a single send
        on the aim rather than losing the morning. A burst of one is the
        historical single shot, which is the safe direction to fail in.
        """
        return _parse_offsets_ms(
            self.walden_reserve_burst_offsets_ms, "WALDEN_RESERVE_BURST_OFFSETS_MS"
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
