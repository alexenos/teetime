"""The race's own instrumentation, tested against the morning that motivated it.

2026-08-21 is the first morning the bot was pushed off its requested slot since
#150, and establishing *why* took hand-diffing two 670KB payloads to find a
single CSS class. The point of these fields is that nobody has to do that again.

The three fixtures here are the raw ``<partial-response>`` envelopes from that
race, not extracted markup like the ``.html.gz`` ones beside them - the envelope
is what carries the ``<eval>`` scripts, and the sheet-open marker only means
anything read against the attempt it came from:

* ``sheet_closed``  - attempt 1, sent +1015ms, club clock 06:30:01, refused
* ``sheet_open``    - attempt 4, sent +4009ms, club clock 06:30:04, refused
* ``accepted``      - attempt 5, sent +4871ms, club clock 06:30:05, granted

See docs/booking-post-mortem-2026-08-21.md.
"""

import gzip
import time
from pathlib import Path

import pytest

from app.providers.walden_http import parse_html, parse_partial_response
from app.providers.walden_http_booker import (
    RESERVE_ACCEPTED,
    RESERVE_REFUSED,
    ReserveObservation,
    _container_cpu_ms,
    _post_response_phrase,
    _sheet_open_in,
    backfill_reserve_telemetry,
    classify_reserve_response,
    observe_reserve_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reserve_responses"


def envelope(name: str) -> str:
    """Read one saved raw partial-response envelope."""
    return gzip.decompress((FIXTURES / f"{name}.xml.gz").read_bytes()).decode(
        "utf-8", errors="replace"
    )


def markup_of(name: str) -> str:
    """The re-rendered markup inside one saved envelope."""
    return parse_partial_response(envelope(name)).markup


class TestSheetOpenMarker:
    """`disable-div` on the slot datascroller, which is live where the countdown is not."""

    def test_the_club_rendered_its_sheet_closed_at_plus_1015ms(self) -> None:
        """Attempt 1 was refused while the club still considered itself shut.

        This is the whole 08-21 finding: the bot arrived before the door opened,
        rather than losing the slot to another member.
        """
        assert _sheet_open_in(markup_of("20260821_refused_sheet_closed")) is False

    def test_the_club_rendered_its_sheet_open_by_plus_4009ms(self) -> None:
        """By attempt 4 the same sheet came back enabled."""
        assert _sheet_open_in(markup_of("20260821_refused_sheet_open")) is True

    def test_an_acceptance_carries_no_sheet_to_read(self) -> None:
        """A grant returns the booking form, so there is no marker either way."""
        assert _sheet_open_in(markup_of("20260821_accepted")) is None

    def test_the_two_refusals_differ_only_in_that_marker(self) -> None:
        """Guards the inference the field rests on.

        The slot rows are byte-identical across four seconds of an open window,
        which is why row and button counts say nothing about who holds a slot. If
        this ever fails, the echoed-rows finding needs revisiting.
        """
        closed = markup_of("20260821_refused_sheet_closed")
        opened = markup_of("20260821_refused_sheet_open")
        assert closed != opened
        assert closed.count("reserve_button") == opened.count("reserve_button")


class TestVerdictsAreUnchanged:
    """The new fields must not have moved the verdict."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("20260821_refused_sheet_closed", RESERVE_REFUSED),
            ("20260821_refused_sheet_open", RESERVE_REFUSED),
            ("20260821_accepted", RESERVE_ACCEPTED),
        ],
    )
    def test_each_attempt_is_classified_as_the_club_answered(
        self, name: str, expected: str
    ) -> None:
        """Reproduces the verdicts the live run logged on 2026-08-21."""
        markup = markup_of(name)
        verdict, _ = classify_reserve_response(parse_html(markup), markup)
        assert verdict == expected


class TestDeferredTelemetry:
    """Skipping the DOM walks during the race must lose nothing afterwards."""

    def _observe(self, name: str, *, defer: bool) -> ReserveObservation:
        response = parse_partial_response(envelope(name))
        return observe_reserve_response(
            attempt=1,
            slot_time=None,
            source="src",
            view_state="vs",
            response=response,
            document=parse_html(response.markup),
            markup=response.markup,
            target_timestamp_ms=None,
            defer_telemetry=defer,
        )

    @pytest.mark.parametrize(
        "name",
        [
            "20260821_refused_sheet_closed",
            "20260821_refused_sheet_open",
            "20260821_accepted",
        ],
    )
    def test_backfill_reproduces_what_the_race_would_have_computed(self, name: str) -> None:
        """A deferred row, once backfilled, equals the row computed inline."""
        inline = self._observe(name, defer=False)
        deferred = self._observe(name, defer=True)
        assert deferred.telemetry_deferred is True

        backfill_reserve_telemetry([deferred])

        assert deferred.telemetry_deferred is False
        assert deferred.countdown_s == inline.countdown_s
        assert deferred.popup_present == inline.popup_present

    def test_deferring_never_changes_the_verdict(self) -> None:
        """The verdict is a decision, so it is computed inline either way."""
        name = "20260821_refused_sheet_closed"
        assert self._observe(name, defer=True).verdict == self._observe(name, defer=False).verdict

    def test_the_countdown_is_still_recovered_from_the_closed_sheet(self) -> None:
        """Attempt 1 carried the frozen pre-window countdown; backfill finds it."""
        deferred = self._observe("20260821_refused_sheet_closed", defer=True)
        assert deferred.countdown_s is None  # not yet read
        backfill_reserve_telemetry([deferred])
        assert deferred.countdown_s == 68

    def test_an_unreadable_row_stays_flagged_rather_than_raising(self) -> None:
        """Telemetry must never be able to fail a booking that succeeded."""
        broken = ReserveObservation(
            attempt=1,
            slot_time=None,
            source="src",
            view_state="vs",
            verdict=RESERVE_REFUSED,
            reason="refused",
            telemetry_deferred=True,
            raw_xml="<not-a-partial-response",
        )
        backfill_reserve_telemetry([broken])  # must not raise
        assert broken.verdict == RESERVE_REFUSED
        # Still flagged, and the flag reaches the ledger - otherwise the row is
        # indistinguishable from a sheet that genuinely had no countdown.
        assert broken.telemetry_deferred is True
        assert broken.as_row()["telemetryDeferred"] is True

    def test_a_successful_backfill_clears_the_flag_in_the_ledger(self) -> None:
        """The read-it-later marker must not outlive the read."""
        deferred = self._observe("20260821_refused_sheet_open", defer=True)
        assert deferred.as_row()["telemetryDeferred"] is True
        backfill_reserve_telemetry([deferred])
        assert deferred.as_row()["telemetryDeferred"] is False

    def test_a_row_with_no_payload_is_left_alone(self) -> None:
        """A timed-out attempt has no markup to re-read."""
        timed_out = ReserveObservation(
            attempt=1,
            slot_time=None,
            source="src",
            view_state="vs",
            verdict=RESERVE_REFUSED,
            reason="refused",
            telemetry_deferred=True,
        )
        backfill_reserve_telemetry([timed_out])
        assert timed_out.telemetry_deferred is True


class TestPostResponseAccounting:
    """Wall against CPU, which is what separates 'slow' from 'starved'."""

    def test_container_cpu_is_readable_or_cleanly_absent(self) -> None:
        """Best-effort by design: a number, or None, never an exception."""
        value = _container_cpu_ms()
        assert value is None or value > 0

    def test_container_cpu_counts_work_rather_than_idle(self) -> None:
        """The counter must not advance with wall-clock on an idle container.

        Guards the bug this replaced: the first version summed every /proc/stat
        column, idle included, so on a 4-CPU box it advanced by roughly
        wall-clock x CPU-count no matter how little work was done - reading as
        heavy contention on an idle machine, in the one field meant to tell
        contention from a slow vCPU.

        Sleeping burns wall time and no CPU, so a counter of real usage must
        advance by far less than the sleep.
        """
        before = _container_cpu_ms()
        if before is None:
            pytest.skip("no cgroup CPU accounting available here")
        time.sleep(0.3)
        after = _container_cpu_ms()
        assert after is not None
        # Generous: other processes share this container. The broken version
        # would have advanced by ~1200ms here, so anything near the sleep fails.
        assert after - before < 250

    def test_container_cpu_is_monotonic(self) -> None:
        """A cumulative counter, so a delta is meaningful."""
        first = _container_cpu_ms()
        if first is None:
            pytest.skip("no cgroup CPU accounting available here")
        second = _container_cpu_ms()
        assert second is not None
        assert second >= first

    def test_burned_cycles_read_as_burned(self) -> None:
        """cpu/wall near 1.0 means the work really cost that much CPU."""
        observation = ReserveObservation(
            attempt=1,
            slot_time=None,
            source="s",
            view_state="v",
            verdict=RESERVE_REFUSED,
            reason="r",
            post_response_wall_ms=560,
            post_response_cpu_ms=550,
        )
        assert "burned" in _post_response_phrase(observation)

    def test_stolen_cycles_read_as_descheduled(self) -> None:
        """cpu/wall near 0.1 means something else had the CPU.

        This is the 2026-08-21 shape if the gap turns out to be contention: 564ms
        of wall for ~54ms of work.
        """
        observation = ReserveObservation(
            attempt=1,
            slot_time=None,
            source="s",
            view_state="v",
            verdict=RESERVE_REFUSED,
            reason="r",
            post_response_wall_ms=564,
            post_response_cpu_ms=54,
        )
        assert "descheduled" in _post_response_phrase(observation)

    def test_an_unmeasured_segment_says_so(self) -> None:
        """No counters is reported as unmeasured, not as zero."""
        observation = ReserveObservation(
            attempt=1,
            slot_time=None,
            source="s",
            view_state="v",
            verdict=RESERVE_REFUSED,
            reason="r",
        )
        assert _post_response_phrase(observation) == "unmeasured"


class TestLedgerRow:
    """The new fields have to reach the JSONL, or none of this is readable later."""

    def test_the_row_carries_the_sheet_and_timing_fields(self) -> None:
        """Every field a future post-mortem needs is serialized."""
        response = parse_partial_response(envelope("20260821_refused_sheet_closed"))
        observation = observe_reserve_response(
            attempt=1,
            slot_time=None,
            source="src",
            view_state="vs",
            response=response,
            document=parse_html(response.markup),
            markup=response.markup,
            target_timestamp_ms=None,
        )
        observation.post_response_wall_ms = 564
        observation.post_response_cpu_ms = 54
        observation.container_cpu_ms = 900

        row = observation.as_row()

        assert row["sheetOpen"] is False
        assert row["postResponseWallMs"] == 564
        assert row["postResponseCpuMs"] == 54
        assert row["containerCpuMs"] == 900
