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
    messaging_channel: str = "twilio"  # "twilio" or "discord"

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

    # Milliseconds past 06:30:00 to ask for the target slot at, before any
    # fallback tee time is tried. Comma-separated; see walden_sweep_offsets_ms().
    #
    # The club refuses for roughly the first second past the window, and its
    # refusal reads "This slot is blocked by another user" regardless of why. On
    # 2026-08-08 a byte-identical request - same slot, same component id, same
    # ViewState - was refused at 0ms, refused at 812ms and accepted at 1291ms;
    # 08-12 repeated it at 0/817/1239ms on a 5:00 PM nobody else wanted, with
    # every slot still open on the sheet an hour later. Firing once on the
    # instant therefore asks the one question most likely to be answered no.
    #
    # Each rung is also a measurement. The ledger records which offsets were
    # refused and which was granted, so a morning narrows the boundary to the
    # rung spacing whether or not it wins. Once the boundary is known this
    # collapses to a single well-chosen offset.
    #
    # Spacing is bounded by how fast the club answers, not by what is written
    # here. Rungs are slept to as absolute instants, so any rung whose moment has
    # already passed when the previous response lands is skipped - and a Reserve
    # round trip has measured 593ms and 647ms on the two lost mornings and 828ms
    # on an uncontested ad-hoc booking. The previous 150/300/500/750 rungs
    # therefore never once fired: attempt 1's answer arrived at ~860ms, past all
    # four. A ladder written at that spacing reads like nine attempts and
    # delivers three.
    #
    # The first rung is the aim point, and as of 2026-08-15 it is no longer 0.
    # Every refusal on record arrived under +1000ms (-60, -14, -7, 0, 0, 812,
    # 817) and every grant over +1200ms (1239, 1240, 1291), and the club stamped
    # its refusals inside the 06:30:00 second and its grant inside 06:30:01. The
    # boundary that fits all of it is the club's own second tick: it refuses
    # while its clock still reads 06:30:00. Firing at 0ms therefore spends the
    # first request on a certain no, which is what every morning has done.
    #
    # 1030 arrives 30ms past that tick. Rungs are offsets of *arrival* on the
    # club's clock - the lead subtracts the measured offset and flight time - so
    # this is 30ms of margin against the tick measurement, which the tightened
    # clock probe pins to roughly +-15ms.
    #
    # 1250 is kept as the second rung because it is where the club has actually
    # granted three times. It is fired without waiting for 1030's answer (see
    # walden_reserve_pipeline_opening_pair), so aiming earlier cannot cost the
    # offset that has been winning.
    #
    # Each rung is also a measurement. The ledger records which offsets were
    # refused and which was granted, so a morning narrows the boundary whether or
    # not it wins - and a grant at 1030 is the first evidence that the boundary
    # is the tick rather than something later.
    #
    # Spacing past the pair is bounded by how fast the club answers, not by what
    # is written here. Rungs are slept to as absolute instants, so any rung whose
    # moment has passed when the previous response lands is skipped - and a
    # Reserve round trip has measured 593-828ms.
    #
    # "0" restores the historical one-shot behaviour.
    walden_reserve_sweep_offsets_ms: str = "1030,1250,2000,3000,4500"

    # Fire the first two rungs without waiting for the first one's answer.
    #
    # Serialised, the ladder cannot ask twice inside one round trip: 08-15 fired
    # at -60ms, got its refusal back at +940ms, and by then the 900 rung was gone
    # so the next question went at +1240ms. Aiming the first rung at the tick
    # (1030) without this would make the second rung ~1780ms - later than the
    # offset that has won three times - so a mis-measured tick would cost the
    # morning rather than one rung.
    #
    # Pipelined, the pair brackets the boundary in one round trip and the run is
    # no worse off than a serial 1250 even if 1030 is refused. Both requests are
    # for the *same* slot, so the second cannot reserve a second tee time and
    # collide with the one-round-per-day rule; the worst case is that the club
    # grants the same hold twice.
    walden_reserve_pipeline_opening_pair: bool = True

    # Write the per-attempt race ledger to the debug artifacts bucket.
    #
    # Only the *final* Reserve response was ever kept, and on both mornings the
    # club actually granted a slot the evidence was in an earlier one. This
    # stores every attempt's raw partial-response - including the <eval> scripts
    # and callback parameters the parser used to discard, which is where a shown
    # dialog is expected to differ from a re-rendered one.
    walden_capture_race_ledger: bool = True

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

    def walden_sweep_offsets_ms(self) -> tuple[int, ...]:
        """The sweep ladder as ordered, deduplicated, non-negative offsets.

        Parsed leniently and never allowed to fail a booking: a malformed value
        degrades to the historical single shot on the instant rather than
        stopping the morning. Negative offsets are dropped - arriving before the
        window is the one thing five mornings of evidence says does not work.
        """
        offsets: list[int] = []
        for piece in self.walden_reserve_sweep_offsets_ms.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value = int(piece)
            except ValueError:
                logger.warning(
                    "WALDEN_RESERVE_SWEEP_OFFSETS_MS: ignoring unparseable offset %r", piece
                )
                continue
            if value >= 0:
                offsets.append(value)
        return tuple(sorted(dict.fromkeys(offsets))) or (0,)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # .env may carry keys this branch doesn't know about (e.g. settings
        # introduced on another branch); ignore them instead of crashing.
        extra = "ignore"


settings = Settings()
