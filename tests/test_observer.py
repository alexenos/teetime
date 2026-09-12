"""Tests for the observer job (app/observer/, issue #189).

The load-bearing ones are the first two classes. Everything else here checks
ordinary wiring; those two check the properties the job exists to have:

* it cannot send a Reserve, because the code to do so is not linked into it;
* it re-reads the sheet before every snapshot, so it cannot hand back nine
  copies of its own pre-window DOM - which is exactly the trap that makes the
  racer's refusal bodies useless as evidence (section 7d of the post-mortem
  skill: the verdict is live, the body is not).
"""

import pathlib
import subprocess
import sys
import textwrap
import time as time_module
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import (
    StaleElementReferenceException,
    WebDriverException,
)

from app.observer import run as observer_run
from app.observer import sheet as observer_sheet
from app.utils.timezone import CTDateTime

NORTHGATE_ROW = "teeTimeCourses:0:teeTimeSlots:"


class TestNoReservePathIsLinkedIn:
    """The read-only guarantee, checked the way the issue states it."""

    def test_entry_point_imports_no_reserve_module(self) -> None:
        """Importing the observer must not pull in a module that can reserve.

        Run in a subprocess on purpose: the rest of this suite imports
        WaldenGolfProvider, so asserting on this process's sys.modules would
        pass or fail for reasons unrelated to the observer's own imports.
        """
        probe = textwrap.dedent(
            """
            import sys
            import app.observer.run

            banned = [
                name
                for name in (
                    "app.providers.walden_provider",
                    "app.providers.walden_http_booker",
                    "app.providers.walden_http",
                    "app.services.booking_service",
                    "app.main",
                )
                if name in sys.modules
            ]
            print(",".join(banned))
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            f"the observer entry point links in {result.stdout.strip()}; it must stay "
            "read-only by construction"
        )

    def test_observer_package_never_imports_reserve_modules(self) -> None:
        """A lazy import inside a function would slip past the check above."""
        offenders = []
        for path in sorted(pathlib.Path("app/observer").glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                for banned in ("walden_provider", "walden_http_booker", "booking_service"):
                    if banned in stripped:
                        offenders.append(f"{path}: {stripped}")
        assert offenders == [], offenders


class TestCaptureRereadsTheSheet:
    """Every snapshot must follow a fresh re-render, not reuse a parked DOM."""

    @staticmethod
    def _driver(pages: list[str]) -> MagicMock:
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        type(driver).page_source = property(lambda self: pages.pop(0) if pages else "<html/>")
        return driver

    def test_clicks_the_selected_tab_once_per_snapshot(self) -> None:
        driver = self._driver([f"<html>{i}</html>" for i in range(3)])
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            snaps = observer_sheet.capture_across_window(
                driver, window_epoch_ms=int(time_module.time() * 1000), count=3, interval_ms=0
            )
        assert len(snaps) == 3
        assert driver.execute_script.call_count == 3
        assert [s.html for s in snaps] == [b"<html>0</html>", b"<html>1</html>", b"<html>2</html>"]
        assert all(s.refresh_ok for s in snaps)
        assert all(s.note is None for s in snaps)

    def test_a_re_render_that_never_lands_is_flagged_not_hidden(self) -> None:
        """A stale snapshot is still stored - but it must not look trustworthy."""
        driver = self._driver(["<html>stale</html>"])
        with patch.object(observer_sheet, "_await_rerender", return_value=False):
            snaps = observer_sheet.capture_across_window(
                driver, window_epoch_ms=int(time_module.time() * 1000), count=1, interval_ms=0
            )
        assert len(snaps) == 1
        assert snaps[0].refresh_ok is False
        assert "may repeat the previous snapshot" in (snaps[0].note or "")

    def test_no_selected_tab_is_recorded_as_unrefreshed(self) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = []
        type(driver).page_source = property(lambda self: "<html/>")
        snaps = observer_sheet.capture_across_window(
            driver, window_epoch_ms=int(time_module.time() * 1000), count=1, interval_ms=0
        )
        assert snaps[0].refresh_ok is False
        assert "without a refresh" in (snaps[0].note or "")
        driver.execute_script.assert_not_called()

    def test_a_refresh_that_raises_still_captures_bytes(self) -> None:
        driver = self._driver(["<html>after error</html>"])
        driver.execute_script.side_effect = WebDriverException("boom")
        snaps = observer_sheet.capture_across_window(
            driver, window_epoch_ms=int(time_module.time() * 1000), count=1, interval_ms=0
        )
        assert snaps[0].html == b"<html>after error</html>"
        assert snaps[0].refresh_ok is False
        assert "WebDriverException" in (snaps[0].note or "")

    def test_an_unreadable_snapshot_is_skipped_not_faked(self) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]

        def _raise(_self: object) -> str:
            raise WebDriverException("gone")

        type(driver).page_source = property(_raise)
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            snaps = observer_sheet.capture_across_window(
                driver, window_epoch_ms=int(time_module.time() * 1000), count=2, interval_ms=0
            )
        assert snaps == []

    def test_offsets_are_measured_from_the_window(self) -> None:
        window = int(time_module.time() * 1000) - 5000
        driver = self._driver(["<html/>"])
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            snaps = observer_sheet.capture_across_window(
                driver, window_epoch_ms=window, count=1, interval_ms=1000
            )
        assert snaps[0].sent_offset_ms >= 5000
        assert snaps[0].captured_offset_ms >= snaps[0].sent_offset_ms
        assert snaps[0].planned_offset_ms == 0


class TestTabMatching:
    """Which day tab names the target date."""

    @pytest.mark.parametrize(
        "text,target,expected",
        [
            ("Friday Fri 11 September Sep", date(2026, 9, 11), True),
            ("Saturday Sat 12 September Sep", date(2026, 9, 11), False),
            ("Friday Fri 11 September Sep", date(2026, 9, 12), False),
            # Same day number, wrong month - a strip spanning a boundary.
            ("Sunday Sun 11 October Oct", date(2026, 9, 11), False),
            # 1 must not match inside 11.
            ("Friday Fri 11 September Sep", date(2026, 9, 1), False),
            ("Tuesday Tue 1 September Sep", date(2026, 9, 1), True),
            ("", date(2026, 9, 11), False),
            ("   \n  ", date(2026, 9, 11), False),
        ],
    )
    def test_matches(self, text: str, target: date, expected: bool) -> None:
        assert observer_sheet._tab_matches(text, target) is expected


class TestParkOnDate:
    """Parking the view, and refusing to guess."""

    @staticmethod
    def _driver(tab_text: str, page: str = "") -> MagicMock:
        driver = MagicMock()
        tab = MagicMock()
        tab.text = tab_text
        driver.find_elements.return_value = [tab]
        type(driver).page_source = property(lambda self: page)
        return driver

    def test_already_parked_clicks_nothing(self) -> None:
        driver = self._driver("Friday Fri 11 September Sep", NORTHGATE_ROW * 20)
        prep = observer_sheet.park_on_date(driver, date(2026, 9, 11))
        assert prep is not None
        assert prep.clicked_tab is False
        assert prep.northgate_row_count == 20
        assert prep.target_date == date(2026, 9, 11)
        driver.execute_script.assert_not_called()

    def test_wrong_date_with_no_matching_tab_refuses_to_capture(self) -> None:
        """Bytes from the wrong date, named for the right one, would be read as evidence."""
        driver = self._driver("Saturday Sat 12 September Sep", "")
        assert observer_sheet.park_on_date(driver, date(2026, 9, 11)) is None

    def test_clicks_the_matching_tab_and_reverifies(self) -> None:
        driver = MagicMock()
        wrong, right = MagicMock(), MagicMock()
        wrong.text = "Saturday Sat 12 September Sep"
        right.text = "Friday Fri 11 September Sep"
        # selected tab (wrong), then the strip, then the selected tab (right).
        driver.find_elements.side_effect = [[wrong], [wrong, right], [right], [right]]
        type(driver).page_source = property(lambda self: NORTHGATE_ROW * 5)
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            prep = observer_sheet.park_on_date(driver, date(2026, 9, 11))
        assert prep is not None
        assert prep.clicked_tab is True
        assert prep.northgate_row_count == 5
        driver.execute_script.assert_called_once()

    def test_a_sheet_with_no_northgate_rows_is_noted_but_still_captured(self) -> None:
        driver = self._driver("Friday Fri 11 September Sep", "<html>nothing useful</html>")
        prep = observer_sheet.park_on_date(driver, date(2026, 9, 11))
        assert prep is not None
        assert prep.northgate_row_count == 0
        assert any("no Northgate rows" in n for n in prep.notes)


class TestWindowInstant:
    def test_is_the_stated_window_not_the_racers_aim(self) -> None:
        now = CTDateTime.now().replace(hour=6, minute=26, second=13, microsecond=987000)
        window = observer_run._window_instant(now)
        assert (window.hour, window.minute, window.second, window.microsecond) == (6, 30, 0, 0)
        assert window.date() == now.date()


class TestWaitUntil:
    def test_returns_immediately_when_the_instant_has_passed(self) -> None:
        started = time_module.perf_counter()
        observer_sheet.wait_until_epoch_ms(int(time_module.time() * 1000) - 10_000)
        assert time_module.perf_counter() - started < 0.5

    def test_waits_for_a_near_instant(self) -> None:
        target = int(time_module.time() * 1000) + 120
        observer_sheet.wait_until_epoch_ms(target)
        assert time_module.time() * 1000 >= target


class TestStaleTabRetry:
    """The page runs its own refresh timers; a detached tab must not cost a snapshot."""

    def test_a_stale_tab_is_refound_and_the_snapshot_still_refreshes(self) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        type(driver).page_source = property(lambda self: "<html>fresh</html>")
        driver.execute_script.side_effect = [StaleElementReferenceException("detached"), None]

        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            snaps = observer_sheet.capture_across_window(
                driver, window_epoch_ms=int(time_module.time() * 1000), count=1, interval_ms=0
            )

        assert len(snaps) == 1
        assert snaps[0].refresh_ok is True
        assert snaps[0].note is None
        assert driver.execute_script.call_count == 2

    def test_stale_on_both_attempts_is_recorded(self) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        type(driver).page_source = property(lambda self: "<html/>")
        driver.execute_script.side_effect = StaleElementReferenceException("detached")

        snaps = observer_sheet.capture_across_window(
            driver, window_epoch_ms=int(time_module.time() * 1000), count=1, interval_ms=0
        )

        assert len(snaps) == 1
        assert snaps[0].refresh_ok is False
        assert "stale on both attempts" in (snaps[0].note or "")
        assert driver.execute_script.call_count == 2

    def test_a_non_stale_error_is_not_retried(self) -> None:
        """Only the benign race is worth a second attempt; a broken browser is not."""
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        type(driver).page_source = property(lambda self: "<html/>")
        driver.execute_script.side_effect = WebDriverException("session deleted")

        snaps = observer_sheet.capture_across_window(
            driver, window_epoch_ms=int(time_module.time() * 1000), count=1, interval_ms=0
        )

        assert driver.execute_script.call_count == 1
        assert "WebDriverException" in (snaps[0].note or "")
