"""The Reserve verdict, tested against the mornings that produced it.

Every response in ``tests/fixtures/reserve_responses`` is a real one the club
sent, pulled from the debug bucket. They are here because reasoning about this
verdict from the markup's prose is what went wrong: the "This slot is blocked by
another user" popup is present in all five, including the two where the club had
just handed over a tee time, and the shipped check read that popup and threw the
tee time away. Twice.

The fixtures are stored gzipped because two of them are the whole tee sheet at
~500-680KB. They are kept whole rather than trimmed so that a future question
nobody has thought of yet can still be asked of them.
"""

import gzip
from datetime import time
from pathlib import Path

import pytest

from app.config import Settings
from app.providers.walden_http import parse_html, parse_partial_response
from app.providers.walden_http_booker import (
    RESERVE_ACCEPTED,
    RESERVE_REFUSED,
    _find_new_blocked_message,
    _reservation_form_slot,
    classify_reserve_response,
    observe_reserve_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reserve_responses"

# What the club actually did on each morning, and the slot it granted when it
# granted one. Established from the response structure, not from its wording -
# see the module docstring.
MORNINGS = [
    ("20260806_refused", RESERVE_REFUSED, None),
    ("20260807_refused", RESERVE_REFUSED, None),
    ("20260808_accepted", RESERVE_ACCEPTED, "12:08 PM"),
    ("20260809_refused", RESERVE_REFUSED, None),
    ("20260812_accepted", RESERVE_ACCEPTED, "04:53 PM"),
]


def load(name: str) -> str:
    """Read one saved Reserve response."""
    return gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes()).decode(
        "utf-8", errors="replace"
    )


class TestVerdictAgainstRealMornings:
    """The five saved Reserve responses, classified."""

    @pytest.mark.parametrize(("name", "expected", "slot"), MORNINGS)
    def test_the_club_s_answer_is_read_correctly(
        self, name: str, expected: str, slot: str | None
    ) -> None:
        """Each morning is classified as what the club actually did."""
        markup = load(name)
        verdict, reason = classify_reserve_response(parse_html(markup), markup)

        assert verdict == expected, f"{name}: {reason}"
        assert _reservation_form_slot(markup) == slot

    @pytest.mark.parametrize(("name", "expected", "_slot"), MORNINGS)
    def test_the_blocked_popup_is_in_every_one_of_them(
        self, name: str, expected: str, _slot: str | None
    ) -> None:
        """The popup cannot be the verdict, because it never varies.

        This is the test that would have caught the bug. The refusal message is
        present in all five responses - the accepted ones included - so any check
        that keys on it alone must classify an acceptance as a refusal, which is
        exactly what happened on 2026-08-08 and 2026-08-12.
        """
        assert "blocked by another user" in load(name)

    @pytest.mark.parametrize(("name", "expected", "_slot"), MORNINGS)
    def test_the_two_shapes_are_what_separates_them(
        self, name: str, expected: str, _slot: str | None
    ) -> None:
        """A refusal hands back the tee sheet; an acceptance hands back a form."""
        markup = load(name)
        observation = observe_reserve_response(
            attempt=1,
            slot_time=time(17, 0),
            source="reserve_button",
            view_state="abcd1234",
            response=parse_partial_response("<partial-response></partial-response>"),
            document=parse_html(markup),
            markup=markup,
            target_timestamp_ms=None,
        )

        if expected == RESERVE_REFUSED:
            assert observation.sheet_rows > 0
            assert observation.reserve_buttons > 0
            assert observation.reservation_form_slot is None
        else:
            assert observation.sheet_rows == 0
            assert observation.reserve_buttons == 0
            assert observation.reservation_form_slot is not None
        assert observation.popup_present is True


class TestStaleMessagesLaterInTheChain:
    """A message the club was already showing is not the next step's answer."""

    def test_the_message_that_came_with_the_slot_is_ignored(self) -> None:
        """The popup rides along in the granting response and must not abort.

        Without this the verdict fix alone would still lose the booking: the
        chain would take the slot, then read the same stale popup one step later
        and report itself blocked.
        """
        granting = parse_partial_response(
            '<partial-response><changes><update id="f">'
            '<![CDATA[<div id="teeSheetValidationErrorPopup">'
            "This slot is blocked by another user</div>]]>"
            "</update></changes></partial-response>"
        )
        stale = "Slot blocked by another user (blocked by another user)"

        assert _find_new_blocked_message(granting, stale) is None

    def test_a_message_that_was_not_there_before_still_counts(self) -> None:
        """Staleness must not become a blanket amnesty for later refusals."""
        refusal = parse_partial_response(
            '<partial-response><changes><update id="f">'
            '<![CDATA[<div id="teeSheetValidationErrorPopup">'
            "This slot is blocked by another user</div>]]>"
            "</update></changes></partial-response>"
        )

        assert _find_new_blocked_message(refusal, None) is not None


class TestSweepLadder:
    """The offsets the Reserve is asked at, parsed from configuration."""

    def test_the_aim_is_the_tick_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sheet is believed to open at 06:30:01, and that is exactly where we aim.

        Every refusal on record arrived under +1000ms (-60, -14, -7, 0, 0, 812,
        817) and every grant over +1000ms, with the club stamping refusals inside
        the 06:30:00 second and its grants inside 06:30:01. An aim short of the
        tick would spend the primary shot on a question already answered no.

        The margin past the tick is 0 since 2026-09-04. It was 30, to keep the
        probe's +-22ms from landing the first send early - but an early send is
        one free refusal, and on a contested slot the 30ms was the loss: every
        Friday first ask went at +1005..+1026ms and was refused, while the same
        ask at the same club-second won every other day. The burst's later
        members are what cover the probe's error now.

        Isolated from the environment: terraform sets these on the deployed
        service, so a shell mirroring the deployment would make this assert what
        is configured rather than what ships.
        """
        monkeypatch.delenv("WALDEN_WINDOW_OPENS_OFFSET_MS", raising=False)
        monkeypatch.delenv("WALDEN_RESERVE_AIM_MARGIN_MS", raising=False)
        config = Settings(_env_file=None)

        aim_ms = config.walden_window_opens_offset_ms + config.walden_reserve_aim_margin_ms

        assert config.walden_window_opens_offset_ms >= 1000
        assert config.walden_reserve_aim_margin_ms == 0
        assert aim_ms == config.walden_window_opens_offset_ms

    def test_the_opening_is_a_burst_every_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The burst is the default, not a Friday special.

        The path the race runs has to be the path every ad-hoc booking runs, or
        it is untested until the morning it counts; and the ladder stays one
        setting away for the rollback.
        """
        for name in (
            "WALDEN_RESERVE_OPENING_MODE",
            "WALDEN_RESERVE_BURST_OFFSETS_MS",
            "WALDEN_RESERVE_BURST_TARGET_ONLY",
        ):
            monkeypatch.delenv(name, raising=False)
        config = Settings(_env_file=None)

        assert config.walden_reserve_opening_mode == "burst"
        offsets = config.walden_burst_offsets_ms()
        # Starts on the tick, is dense through the probe's error, and reaches
        # past the latest instant a Friday sheet has rendered closed (+2.8s
        # past the window on 08-28, i.e. ~+1800ms past the aim).
        assert offsets[0] == 0
        assert offsets[1] <= 120
        assert offsets[-1] >= 1800
        # Target-only by default (see docs/booking-post-mortem-2026-09-04-evening.md):
        # a fallback interleaved into the burst shares the target's ViewState, and
        # the 2026-09-04 evening ad-hoc test found the club can finalize the
        # fallback's grant instead of the target's. The fallback list is walked
        # serially after the burst instead.
        assert config.walden_reserve_burst_target_only >= len(offsets)

    def test_burst_offsets_parse_leniently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed value degrades to one send on the aim, never to an error."""
        monkeypatch.setenv("WALDEN_RESERVE_BURST_OFFSETS_MS", " 100, 0,bad,-5,100,220 ")
        assert Settings(_env_file=None).walden_burst_offsets_ms() == (0, 100, 220)

        monkeypatch.setenv("WALDEN_RESERVE_BURST_OFFSETS_MS", "nonsense")
        assert Settings(_env_file=None).walden_burst_offsets_ms() == (0,)

    def test_the_default_ladder_is_retries_from_the_aim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rungs are offsets past the open now, so the first one is 0.

        The ladder stopped being a search over offsets once the open was named:
        rung 0 *is* the hypothesis, and the rest are there to catch it being
        wrong. One of them still has to reach the offsets the club has actually
        granted at, or a wrong hypothesis costs the morning rather than a rung.
        """
        monkeypatch.delenv("WALDEN_RESERVE_SWEEP_OFFSETS_MS", raising=False)
        monkeypatch.delenv("WALDEN_WINDOW_OPENS_OFFSET_MS", raising=False)
        monkeypatch.delenv("WALDEN_RESERVE_AIM_MARGIN_MS", raising=False)
        config = Settings(_env_file=None)
        ladder = config.walden_sweep_offsets_ms()
        aim_ms = config.walden_window_opens_offset_ms + config.walden_reserve_aim_margin_ms

        assert ladder[0] == 0
        assert list(ladder) == sorted(ladder)
        # 08-08 was granted at +1291ms, 08-12 at +1239ms and 08-15 at +1240ms,
        # measured from the stated window. A rung has to land out there.
        assert any(aim_ms + rung >= 1239 for rung in ladder)
        # And the ladder has to be short. It is retries now; a long one is the
        # search it replaced.
        assert len(ladder) <= 4

    def test_the_opening_pair_is_off_until_it_has_been_exercised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Untested concurrency does not get its first run in the race.

        Firing rungs 0 and 250 together has never run against the club, and the
        sequence it creates - a Reserve granted while a second for the same slot
        is already in flight - has never been observed. With rung 0 now being the
        hypothesis rather than a guess, a right hypothesis makes that sequence
        the daily case rather than the rare one.

        Serially the ladder still asks at roughly +1030, +1770 and +2510ms from
        the stated window, all past every refusal on record, so the cost of
        leaving this off is an ask at +1770 instead of +1280.
        """
        monkeypatch.delenv("WALDEN_RESERVE_PIPELINE_OPENING_PAIR", raising=False)

        assert Settings(_env_file=None).walden_reserve_pipeline_opening_pair is False

    def test_a_malformed_ladder_degrades_to_one_shot(self) -> None:
        """A bad value must not stop a morning."""
        assert Settings(walden_reserve_sweep_offsets_ms="nonsense").walden_sweep_offsets_ms() == (
            0,
        )

    def test_arriving_before_the_window_is_not_offered(self) -> None:
        """Negative offsets are dropped; every refusal so far was an early one."""
        ladder = Settings(walden_reserve_sweep_offsets_ms="-500,0,250").walden_sweep_offsets_ms()

        assert ladder == (0, 250)
