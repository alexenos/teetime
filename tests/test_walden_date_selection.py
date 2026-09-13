"""Tests for the shared date-selection routine (app/providers/walden_date_selection.py).

The calendar tests came from ``test_walden_provider.py`` with the code they
cover; the strip tests are new with issue #199. The class at the end is the
load-bearing one: it checks the selectors against a sheet the club actually
served, rather than against a stand-in built from the same assumptions as the
code.
"""

import pathlib
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)

from app.providers import walden_date_selection as wds

PREFIX = "TEST"


@pytest.fixture
def waits() -> MagicMock:
    """A wait strategy that returns instantly; timing is not what is under test."""
    return MagicMock()


class TestCalendarCurrentMonth:
    def test_from_dropdowns(self) -> None:
        """A 0-indexed month value is converted to 1-indexed."""
        driver = MagicMock()
        month_elem, year_elem = MagicMock(), MagicMock()
        driver.find_elements.side_effect = lambda by, sel: (
            [month_elem] if "month" in sel else [year_elem] if "year" in sel else []
        )

        with patch("app.providers.walden_date_selection.Select") as select_class:
            month_select, year_select = MagicMock(), MagicMock()
            month_select.first_selected_option.get_attribute.return_value = "0"
            year_select.first_selected_option.get_attribute.return_value = "2026"
            select_class.side_effect = lambda elem: (
                month_select if elem is month_elem else year_select
            )

            assert wds.calendar_current_month(driver) == (1, 2026)

    def test_from_header_text(self) -> None:
        driver = MagicMock()
        header = MagicMock(text="January 2026")
        driver.find_elements.side_effect = lambda by, sel: (
            [] if "month" in sel or "year" in sel else [header] if "title" in sel else []
        )

        assert wds.calendar_current_month(driver) == (1, 2026)

    def test_returns_none_when_unreadable(self) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = []

        assert wds.calendar_current_month(driver) == (None, None)


class TestNavigateCalendarToMonth:
    def test_same_month_needs_no_navigation(self, waits: MagicMock) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = []

        with patch.object(wds, "calendar_current_month", return_value=(1, 2026)):
            result = wds.navigate_calendar_to_month(
                driver, date(2026, 1, 25), wait_strategy=waits, log_prefix=PREFIX
            )

        assert result is True

    def test_via_dropdowns(self, waits: MagicMock) -> None:
        driver = MagicMock()
        month_elem, year_elem = MagicMock(), MagicMock()
        driver.find_elements.side_effect = lambda by, sel: (
            [month_elem] if "month" in sel else [year_elem] if "year" in sel else []
        )

        with patch("app.providers.walden_date_selection.Select") as select_class:
            month_select, year_select = MagicMock(), MagicMock()
            select_class.side_effect = lambda elem: (
                month_select if elem is month_elem else year_select
            )

            result = wds.navigate_calendar_to_month(
                driver, date(2026, 2, 1), wait_strategy=waits, log_prefix=PREFIX
            )

        assert result is True
        year_select.select_by_value.assert_called_with("2026")
        # February is 1 when the widget counts months from zero.
        month_select.select_by_value.assert_called_with("1")

    def test_via_next_arrow(self, waits: MagicMock) -> None:
        driver = MagicMock()
        next_button = MagicMock()
        next_button.is_displayed.return_value = True
        next_button.is_enabled.return_value = True
        driver.find_elements.side_effect = lambda by, sel: (
            [next_button] if "next" in sel.lower() else []
        )

        with patch.object(wds, "calendar_current_month", return_value=(1, 2026)):
            result = wds.navigate_calendar_to_month(
                driver, date(2026, 2, 1), wait_strategy=waits, log_prefix=PREFIX
            )

        assert result is True
        assert next_button.click.call_count == 1

    def test_via_prev_arrow(self, waits: MagicMock) -> None:
        driver = MagicMock()
        prev_button = MagicMock()
        prev_button.is_displayed.return_value = True
        prev_button.is_enabled.return_value = True
        driver.find_elements.side_effect = lambda by, sel: (
            [prev_button] if "prev" in sel.lower() else []
        )

        with patch.object(wds, "calendar_current_month", return_value=(1, 2026)):
            result = wds.navigate_calendar_to_month(
                driver, date(2025, 12, 15), wait_strategy=waits, log_prefix=PREFIX
            )

        assert result is True
        assert prev_button.click.call_count == 1

    def test_fails_when_no_nav_button(self, waits: MagicMock) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = []

        with patch.object(wds, "calendar_current_month", return_value=(1, 2026)):
            result = wds.navigate_calendar_to_month(
                driver, date(2026, 3, 1), wait_strategy=waits, log_prefix=PREFIX
            )

        assert result is False


class TestSelectViaCalendar:
    def test_navigates_before_picking_a_day(self, waits: MagicMock) -> None:
        driver = MagicMock()
        trigger = MagicMock()
        trigger.is_displayed.return_value = True
        driver.find_elements.side_effect = lambda by, sel: (
            [trigger] if "calendar" in sel.lower() else []
        )
        target = date(2026, 2, 1)

        with patch.object(wds, "navigate_calendar_to_month", return_value=True) as navigate:
            wds.select_via_calendar(driver, target, wait_strategy=waits, log_prefix=PREFIX)

        navigate.assert_called_once_with(driver, target, wait_strategy=waits, log_prefix=PREFIX)

    def test_no_trigger_is_not_an_error(self, waits: MagicMock) -> None:
        driver = MagicMock()
        driver.find_elements.return_value = []

        assert (
            wds.select_via_calendar(
                driver, date(2026, 2, 1), wait_strategy=waits, log_prefix=PREFIX
            )
            is False
        )


class TestSelectDateOrdering:
    """Which strategy runs when — the racer's ordering must not have changed."""

    @staticmethod
    def _no_date_inputs() -> MagicMock:
        driver = MagicMock()
        driver.find_element.side_effect = NoSuchElementException()
        return driver

    def test_calendar_success_short_circuits_the_strip(self, waits: MagicMock) -> None:
        driver = self._no_date_inputs()
        with (
            patch.object(wds, "select_via_calendar", return_value=True),
            patch.object(wds, "select_via_strip") as strip,
        ):
            result = wds.select_date(
                driver, date(2026, 2, 1), wait_strategy=waits, log_prefix=PREFIX
            )
        assert result is True
        strip.assert_not_called()

    def test_a_failed_calendar_falls_back_to_the_strip(self, waits: MagicMock) -> None:
        """The fallback the racer never had: it used to abandon the booking here."""
        driver = self._no_date_inputs()
        with (
            patch.object(wds, "select_via_calendar", return_value=False),
            patch.object(wds, "select_via_strip", return_value=True) as strip,
        ):
            result = wds.select_date(
                driver, date(2026, 2, 1), wait_strategy=waits, log_prefix=PREFIX
            )
        assert result is True
        strip.assert_called_once()

    def test_both_failing_is_reported(self, waits: MagicMock) -> None:
        driver = self._no_date_inputs()
        with (
            patch.object(wds, "select_via_calendar", return_value=False),
            patch.object(wds, "select_via_strip", return_value=False),
        ):
            assert (
                wds.select_date(driver, date(2026, 2, 1), wait_strategy=waits, log_prefix=PREFIX)
                is False
            )


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
        assert wds.tab_matches(text, target) is expected


class Icon:
    def __init__(self, klass: str) -> None:
        self.klass = klass

    def get_attribute(self, name: str) -> str:
        return self.klass


class Control:
    """One of the strip's forward links, identified by the icon it carries."""

    def __init__(self, icon: str) -> None:
        self.icon = Icon(icon)

    def find_elements(self, by: str, selector: str) -> list[Icon]:
        return [self.icon]


class Tab:
    """A day tab, rendered as the club renders one: "Sunday Sun 20 September Sep"."""

    def __init__(self, day: date) -> None:
        self.day = day
        self.text = f"{day:%A} {day:%a} {day.day} {day:%B} {day:%b}"


class StripDriver:
    """A stand-in for the club's date strip, which pages rather than scrolls.

    Renders seven day tabs from ``start`` and shifts one day forward per click of
    the single-day control - the real markup's behaviour, and the only way a date
    past the strip's horizon can be selected. ``pages=False`` models a strip that
    answers the click without moving. It offers no date input and no calendar
    trigger, so ``select_date`` falls through to the strip exactly as it would on
    a sheet whose calendar failed to open.
    """

    def __init__(self, start: date, *, pages: bool = True, controls: bool = True) -> None:
        self.start = start
        self.pages = pages
        self.controls = controls
        self.selected = start
        self.advances = 0
        self.tab_clicks = 0
        self.page_source = "teeTimeCourses:0:teeTimeSlots:" * 7

    def _span(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(7)]

    def find_elements(self, by: str, selector: str) -> list[object]:
        if selector == wds.DOM.DATE_SELECTION.day_tab_links:
            return [Tab(day) for day in self._span()]
        if selector == wds.DOM.DATE_SELECTION.selected_day_tab:
            return [Tab(self.selected)] if self.selected in self._span() else []
        if selector == wds.DOM.DATE_SELECTION.strip_forward_links and self.controls:
            return [Control("fa fa-angle-double-right"), Control("fa fa-angle-right")]
        return []

    def find_element(self, by: str, selector: str) -> object:
        """Paging leaves no tab selected, so the click is awaited on this."""
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]

    def execute_script(self, script: str, element: object) -> None:
        if isinstance(element, Control):
            self.advances += 1
            assert "fa-angle-right" in element.icon.klass, "must page a day, not a week"
            if self.pages:
                self.start += timedelta(days=1)
        elif isinstance(element, Tab):
            self.tab_clicks += 1
            self.selected = element.day


class TestReachingADatePastTheStrip:
    """Issue #199: the target is always the eighth day of a seven-day strip."""

    TODAY = date(2026, 9, 13)
    TARGET = date(2026, 9, 20)

    def test_the_target_is_never_in_the_strip_as_first_rendered(self) -> None:
        """The premise. Without paging there is nothing to click, ever."""
        driver = StripDriver(self.TODAY)
        assert not wds._click_day_tab(driver, self.TARGET, log_prefix=PREFIX)

    def test_paging_forward_once_reaches_it(self) -> None:
        driver = StripDriver(self.TODAY)
        with patch.object(wds, "await_rerender", return_value=True):
            assert wds.select_via_strip(driver, self.TARGET, log_prefix=PREFIX)
        assert driver.advances == 1, "one page forward is all an ordinary morning needs"
        assert driver.tab_clicks == 1
        assert driver.selected == self.TARGET

    def test_select_date_reaches_it_when_there_is_no_calendar(self, waits: MagicMock) -> None:
        """End to end through the shared entry point, as the observer calls it."""
        driver = StripDriver(self.TODAY)
        with patch.object(wds, "await_rerender", return_value=True):
            result = wds.select_date(driver, self.TARGET, wait_strategy=waits, log_prefix=PREFIX)
        assert result is True
        assert driver.selected == self.TARGET

    def test_a_strip_that_will_not_move_is_reported_not_hammered(self) -> None:
        driver = StripDriver(self.TODAY, pages=False)
        with patch.object(wds, "await_rerender", return_value=True):
            assert not wds.select_via_strip(driver, self.TARGET, log_prefix=PREFIX)
        assert driver.advances == 1, "a strip that did not move must not be clicked again"
        assert driver.tab_clicks == 0

    def test_no_forward_control_refuses_rather_than_guessing(self) -> None:
        driver = StripDriver(self.TODAY, controls=False)
        with patch.object(wds, "await_rerender", return_value=True):
            assert not wds.select_via_strip(driver, self.TARGET, log_prefix=PREFIX)
        assert driver.tab_clicks == 0

    def test_paging_is_bounded_when_the_date_never_appears(self) -> None:
        """A strip that pages forever must not eat the pre-window budget."""
        driver = StripDriver(self.TODAY)
        with patch.object(wds, "await_rerender", return_value=True):
            assert not wds.select_via_strip(driver, date(2027, 5, 1), log_prefix=PREFIX)
        assert driver.advances == wds.MAX_STRIP_ADVANCES

    def test_a_stale_forward_control_is_refound_and_the_page_still_advances(self) -> None:
        """The sheet's own refresh timers detach elements; that must not cost a morning."""
        driver = StripDriver(self.TODAY)
        clicks: list[object] = []
        real_click = driver.execute_script

        def flaky(script: str, element: object) -> None:
            clicks.append(element)
            if len(clicks) == 1:
                raise StaleElementReferenceException("detached by the page's own refresh")
            real_click(script, element)

        driver.execute_script = flaky  # type: ignore[method-assign]
        with patch.object(wds, "await_rerender", return_value=True):
            assert wds.select_via_strip(driver, self.TARGET, log_prefix=PREFIX)
        assert driver.selected == self.TARGET
        assert driver.advances == 1, "the retried click must page the strip exactly once"

    def test_stale_on_both_attempts_gives_up(self) -> None:
        driver = StripDriver(self.TODAY)

        def always_stale(script: str, element: object) -> None:
            raise StaleElementReferenceException("detached")

        driver.execute_script = always_stale  # type: ignore[method-assign]
        with patch.object(wds, "await_rerender", return_value=True):
            assert not wds.select_via_strip(driver, self.TARGET, log_prefix=PREFIX)

    def test_the_day_control_is_preferred_over_the_week_jump(self) -> None:
        week, day = Control("fa fa-angle-double-right"), Control("fa fa-angle-right")
        assert wds._single_day_control([week, day]) is day

    def test_an_unrecognised_pair_falls_back_to_the_last_control(self) -> None:
        """Either control brings a date seven days out into view, so guess."""
        first, last = Control("fa fa-chevron"), Control("fa fa-other")
        assert wds._single_day_control([first, last]) is last


class TestTheSheetAsTheClubRendersIt:
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
        tabs = sheet.select(wds.DOM.DATE_SELECTION.day_tab_links)
        assert len(tabs) == 7
        for offset, tab in enumerate(tabs):
            assert wds.tab_matches(tab.get_text(), self.CAPTURED + timedelta(offset))

    def test_the_target_date_has_no_tab(self, sheet: BeautifulSoup) -> None:
        """The whole of the bug: there was never anything to click."""
        tabs = sheet.select(wds.DOM.DATE_SELECTION.day_tab_links)
        assert not any(wds.tab_matches(tab.get_text(), self.TARGET) for tab in tabs)

    def test_only_the_captured_day_is_selected(self, sheet: BeautifulSoup) -> None:
        selected = sheet.select(wds.DOM.DATE_SELECTION.selected_day_tab)
        assert len(selected) == 1
        assert wds.tab_matches(selected[0].get_text(), self.CAPTURED)

    def test_the_strip_offers_an_enabled_forward_control(self, sheet: BeautifulSoup) -> None:
        """The way out, and that the day and week jumps are distinguishable."""
        controls = sheet.select(wds.DOM.DATE_SELECTION.strip_forward_links)
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

    def test_no_date_input_matches_so_the_calendar_is_always_reached(
        self, sheet: BeautifulSoup
    ) -> None:
        """Why the racer logs "No date input found" every single morning.

        The club's date field is a readonly PrimeFaces input whose id, name and
        class carry no "date" - its class says "hasDatepicker", which the
        case-sensitive `[class*='date']` does not match.
        """
        for selector in wds.DOM.DATE_SELECTION.date_inputs:
            assert sheet.select(selector) == [], selector

    def test_the_calendar_popup_check_is_satisfied_before_anything_opens(
        self, sheet: BeautifulSoup
    ) -> None:
        """So the popup wait proves nothing; the day lookup is what proves it.

        The club ships an empty `#ui-datepicker-div` in every sheet. The day
        XPaths cannot match inside it - the whole page has no `<td>` at all -
        so a found day element really does mean a calendar opened.
        """
        assert sheet.select(wds.DOM.DATE_SELECTION.calendar_popup) != []
        assert sheet.select_one("#ui-datepicker-div").get_text(strip=True) == ""
        assert sheet.find_all("td") == []
