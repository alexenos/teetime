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

    def test_the_default_ladder_aims_past_the_clubs_second_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first rung clears +1000ms, and the ladder still reaches the grants.

        Every refusal on record arrived under +1000ms and every grant over
        +1200ms, with the club stamping refusals inside the 06:30:00 second and
        its grant inside 06:30:01. Aiming the first question at 0ms spent it on a
        certain no; this pins that it is no longer aimed there.

        Isolated from the environment: terraform sets this variable on the
        deployed service, so a shell mirroring the deployment would make this
        assert what is configured rather than what ships. The tests below need no
        such care - an explicit init argument outranks both sources.
        """
        monkeypatch.delenv("WALDEN_RESERVE_SWEEP_OFFSETS_MS", raising=False)
        ladder = Settings(_env_file=None).walden_sweep_offsets_ms()

        assert ladder[0] > 1000
        assert list(ladder) == sorted(ladder)
        # 08-08 was granted at +1291ms, 08-12 at +1239ms and 08-15 at +1240ms, so
        # a ladder that stopped short of those would give up the offset that has
        # actually been winning while the new aim point is still unproven.
        assert ladder[1] >= 1239
        assert ladder[-1] >= 1300

    def test_a_malformed_ladder_degrades_to_one_shot(self) -> None:
        """A bad value must not stop a morning."""
        assert Settings(walden_reserve_sweep_offsets_ms="nonsense").walden_sweep_offsets_ms() == (
            0,
        )

    def test_arriving_before_the_window_is_not_offered(self) -> None:
        """Negative offsets are dropped; every refusal so far was an early one."""
        ladder = Settings(walden_reserve_sweep_offsets_ms="-500,0,250").walden_sweep_offsets_ms()

        assert ladder == (0, 250)
