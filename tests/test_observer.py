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
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from selenium.common.exceptions import (
    NoSuchElementException,
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


class _Icon:
    def __init__(self, klass: str) -> None:
        self.klass = klass

    def get_attribute(self, name: str) -> str:
        assert name == "class"
        return self.klass


class _Control:
    """One of the strip's forward links, identified by the icon it carries."""

    def __init__(self, icon: str) -> None:
        self.icon = _Icon(icon)

    def find_elements(self, by: str, selector: str) -> list[_Icon]:
        return [self.icon]


class _Tab:
    """A day tab, rendered as the club renders one: "Sunday Sun 20 September Sep"."""

    def __init__(self, day: date) -> None:
        self.day = day
        self.text = f"{day:%A} {day:%a} {day.day} {day:%B} {day:%b}"


class _StripDriver:
    """A stand-in for the club's date strip, which pages rather than scrolls.

    Renders seven day tabs from ``start`` and shifts one day forward per click of
    the single-day control - the real markup's behaviour, and the only way a date
    past the strip's horizon can be selected. ``pages=False`` models a strip that
    answers the click without moving.
    """

    def __init__(self, start: date, *, pages: bool = True, controls: bool = True) -> None:
        self.start = start
        self.pages = pages
        self.controls = controls
        self.selected = start
        self.advances = 0
        self.tab_clicks = 0
        self.page_source = NORTHGATE_ROW * 7

    def _span(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(7)]

    def find_elements(self, by: str, selector: str) -> list[object]:
        if selector == observer_sheet.DOM.DATE_SELECTION.day_tab_links:
            return [_Tab(day) for day in self._span()]
        if selector == observer_sheet.DOM.DATE_SELECTION.selected_day_tab:
            return [_Tab(self.selected)] if self.selected in self._span() else []
        if selector == observer_sheet.DOM.DATE_SELECTION.strip_forward_links:
            if not self.controls:
                return []
            return [_Control("fa fa-angle-double-right"), _Control("fa fa-angle-right")]
        return []

    def find_element(self, by: str, selector: str) -> object:
        """Paging leaves no tab selected, so the click is awaited on this."""
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]

    def execute_script(self, script: str, element: object) -> None:
        if isinstance(element, _Control):
            self.advances += 1
            assert "fa-angle-right" in element.icon.klass, "must page a day, not a week"
            if self.pages:
                self.start += timedelta(days=1)
        elif isinstance(element, _Tab):
            self.tab_clicks += 1
            self.selected = element.day


class TestReachingADatePastTheStrip:
    """Issue #199: the target is always the eighth day of a seven-day strip."""

    TODAY = date(2026, 9, 13)
    TARGET = date(2026, 9, 20)

    def test_the_target_is_never_in_the_strip_as_first_rendered(self) -> None:
        """The premise. Without paging there is nothing to click, ever."""
        driver = _StripDriver(self.TODAY)
        assert not observer_sheet._click_day_tab(driver, self.TARGET)

    def test_paging_forward_once_reaches_it(self) -> None:
        driver = _StripDriver(self.TODAY)
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            prep = observer_sheet.park_on_date(driver, self.TARGET)
        assert prep is not None
        assert prep.target_date == self.TARGET
        assert prep.clicked_tab is True
        assert prep.northgate_row_count == 7
        assert driver.advances == 1, "one page forward is all an ordinary morning needs"
        assert driver.tab_clicks == 1
        assert driver.selected == self.TARGET

    def test_a_strip_that_will_not_move_is_reported_not_hammered(self) -> None:
        driver = _StripDriver(self.TODAY, pages=False)
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            assert observer_sheet.park_on_date(driver, self.TARGET) is None
        assert driver.advances == 1, "a strip that did not move must not be clicked again"
        assert driver.tab_clicks == 0

    def test_no_forward_control_refuses_rather_than_guessing(self) -> None:
        driver = _StripDriver(self.TODAY, controls=False)
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            assert observer_sheet.park_on_date(driver, self.TARGET) is None
        assert driver.tab_clicks == 0

    def test_paging_is_bounded_when_the_date_never_appears(self) -> None:
        """A strip that pages forever must not eat the pre-window budget."""
        driver = _StripDriver(self.TODAY)
        with patch.object(observer_sheet, "_await_rerender", return_value=True):
            assert observer_sheet.park_on_date(driver, date(2027, 5, 1)) is None
        assert driver.advances == observer_sheet.MAX_STRIP_ADVANCES

    def test_the_day_control_is_preferred_over_the_week_jump(self) -> None:
        week, day = _Control("fa fa-angle-double-right"), _Control("fa fa-angle-right")
        assert observer_sheet._single_day_control([week, day]) is day

    def test_an_unrecognised_pair_falls_back_to_the_last_control(self) -> None:
        """Either control brings a date seven days out into view, so guess."""
        first, last = _Control("fa fa-chevron"), _Control("fa fa-other")
        assert observer_sheet._single_day_control([first, last]) is last


class TestTheStripAsTheClubRendersIt:
    """The selectors against a real captured sheet, not a stand-in.

    ``walden_tee_time_final.html`` was captured on 2026-02-01 for target date
    2026-02-08 - the same seven-days-out target every morning has. It is issue
    #199 preserved in a fixture: the sheet the racer recorded as "final" spans
    1-7 February and carries no tab for the 8th, so the observer's day-tab
    search could not have succeeded on that morning either.
    """

    CAPTURED = date(2026, 2, 1)
    TARGET = date(2026, 2, 8)

    @pytest.fixture
    def sheet(self) -> BeautifulSoup:
        path = pathlib.Path(__file__).parent / "fixtures" / "walden_tee_time_final.html"
        if not path.exists():
            pytest.skip("tee sheet fixture not found")
        return BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")

    def test_the_strip_spans_seven_days_from_the_captured_day(self, sheet: BeautifulSoup) -> None:
        tabs = sheet.select(observer_sheet.DOM.DATE_SELECTION.day_tab_links)
        assert len(tabs) == 7
        for offset, tab in enumerate(tabs):
            assert observer_sheet._tab_matches(tab.get_text(), self.CAPTURED + timedelta(offset))

    def test_the_target_date_has_no_tab(self, sheet: BeautifulSoup) -> None:
        """The whole of the bug: there was never anything to click."""
        tabs = sheet.select(observer_sheet.DOM.DATE_SELECTION.day_tab_links)
        assert not any(observer_sheet._tab_matches(tab.get_text(), self.TARGET) for tab in tabs)

    def test_only_the_captured_day_is_selected(self, sheet: BeautifulSoup) -> None:
        selected = sheet.select(observer_sheet.DOM.DATE_SELECTION.selected_day_tab)
        assert len(selected) == 1
        assert observer_sheet._tab_matches(selected[0].get_text(), self.CAPTURED)

    def test_the_strip_offers_an_enabled_forward_control(self, sheet: BeautifulSoup) -> None:
        """The way out, and that the day and week jumps are distinguishable."""
        controls = sheet.select(observer_sheet.DOM.DATE_SELECTION.strip_forward_links)
        assert len(controls) == 2
        icons = [" ".join(control.select_one("i")["class"]) for control in controls]
        assert any("fa-angle-right" in icon for icon in icons)
        assert any("fa-angle-double-right" in icon for icon in icons)

    def test_the_backward_controls_are_disabled_and_so_are_not_links(
        self, sheet: BeautifulSoup
    ) -> None:
        """Why matching only `<a>` is what keeps a disabled control unclicked."""
        backward = sheet.select_one(".backward-controls")
        assert backward is not None
        assert backward.select("a") == []
        assert len(backward.select("span.ui-state-disabled")) == 2


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
