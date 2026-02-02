"""
DOM Parsing Tests using captured HTML fixtures.

These tests validate that our DOM selectors work correctly against
real HTML from the Walden Golf website, without needing live access.
"""

import pytest
from pathlib import Path

from bs4 import BeautifulSoup

from tests.fixtures.dom_schema import (
    WaldenDOMSchema,
    get_course_index_from_element_id,
    is_northgate_slot,
    is_walden_slot,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def login_page_html() -> BeautifulSoup:
    """Load the login page HTML fixture."""
    html_path = FIXTURES_DIR / "walden_login_page.html"
    if not html_path.exists():
        pytest.skip("Login page fixture not found. Run capture_html_snapshots.py first.")
    return BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")


@pytest.fixture
def tee_time_page_html() -> BeautifulSoup:
    """Load the tee time page HTML fixture."""
    html_path = FIXTURES_DIR / "walden_tee_time_loaded.html"
    if not html_path.exists():
        pytest.skip("Tee time page fixture not found. Run capture_html_snapshots.py first.")
    return BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")


class TestLoginPageSelectors:
    """Tests for login page DOM selectors."""

    def test_member_input_exists(self, login_page_html: BeautifulSoup):
        """Member number input field should exist."""
        elements = login_page_html.select(WaldenDOMSchema.LOGIN_MEMBER_INPUT)
        assert len(elements) == 1
        assert elements[0].get("type") == "text"

    def test_password_input_exists(self, login_page_html: BeautifulSoup):
        """Password input field should exist."""
        elements = login_page_html.select(WaldenDOMSchema.LOGIN_PASSWORD_INPUT)
        assert len(elements) == 1
        assert elements[0].get("type") == "password"

    def test_submit_button_exists(self, login_page_html: BeautifulSoup):
        """Submit button should exist."""
        elements = login_page_html.select(WaldenDOMSchema.LOGIN_SUBMIT_BUTTON)
        assert len(elements) >= 1


class TestCourseDropdownSelectors:
    """Tests for course dropdown DOM selectors."""

    def test_course_dropdown_exists(self, tee_time_page_html: BeautifulSoup):
        """Course dropdown should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.COURSE_DROPDOWN)
        assert len(elements) == 1
        assert "ui-selectcheckboxmenu" in elements[0].get("class", [])

    def test_course_dropdown_has_courses(self, tee_time_page_html: BeautifulSoup):
        """Course dropdown should contain course checkboxes."""
        # Look for the hidden checkboxes within the dropdown
        northgate = tee_time_page_html.select("input[type='checkbox'][value='2']")
        walden = tee_time_page_html.select("input[type='checkbox'][value='1']")
        fast_five = tee_time_page_html.select("input[type='checkbox'][value='3']")

        assert len(northgate) >= 1, "Northgate checkbox not found"
        assert len(walden) >= 1, "Walden checkbox not found"
        assert len(fast_five) >= 1, "Fast Five checkbox not found"


class TestTeeTimeSlotSelectors:
    """Tests for tee time slot DOM selectors."""

    def test_datascroller_exists(self, tee_time_page_html: BeautifulSoup):
        """DataScroller component should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.DATASCROLLER)
        assert len(elements) >= 1

    def test_datascroller_list_exists(self, tee_time_page_html: BeautifulSoup):
        """DataScroller list should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.DATASCROLLER_LIST)
        assert len(elements) >= 1

    def test_slot_items_exist(self, tee_time_page_html: BeautifulSoup):
        """Should have multiple slot items."""
        elements = tee_time_page_html.select(WaldenDOMSchema.DATASCROLLER_ITEM)
        assert len(elements) >= 50, f"Expected at least 50 slots, found {len(elements)}"

    def test_slot_time_labels_exist(self, tee_time_page_html: BeautifulSoup):
        """Each slot should have a time label."""
        elements = tee_time_page_html.select(WaldenDOMSchema.SLOT_TIME_LABEL)
        assert len(elements) >= 50

        # Check time format (e.g., "07:30 AM", "04:34 PM")
        import re
        time_pattern = re.compile(r"\d{2}:\d{2}\s*(AM|PM)")
        for el in elements[:10]:  # Check first 10
            time_text = el.get_text(strip=True)
            assert time_pattern.match(time_text), f"Invalid time format: {time_text}"

    def test_reserve_buttons_exist(self, tee_time_page_html: BeautifulSoup):
        """Should have reserve buttons for available slots."""
        elements = tee_time_page_html.select(WaldenDOMSchema.RESERVE_BUTTON)
        assert len(elements) >= 1, "No reserve buttons found"

        # Each button should have "Reserve" text
        for el in elements[:5]:
            assert "Reserve" in el.get_text(), f"Button doesn't say Reserve: {el.get_text()}"


class TestCourseIdentification:
    """Tests for identifying which course a slot belongs to."""

    def test_get_course_index_northgate(self):
        """Should extract course index 0 for Northgate slots."""
        element_id = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:slotTeeDIV"
        assert get_course_index_from_element_id(element_id) == "0"

    def test_get_course_index_walden(self):
        """Should extract course index 1 for Walden slots."""
        element_id = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:1:teeTimeSlots:12:slotTee:0:reserve_button"
        assert get_course_index_from_element_id(element_id) == "1"

    def test_get_course_index_invalid(self):
        """Should return None for IDs without course info."""
        assert get_course_index_from_element_id("some_random_id") is None
        assert get_course_index_from_element_id("") is None

    def test_is_northgate_slot(self):
        """Should correctly identify Northgate slots."""
        northgate_id = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:slotTeeDIV"
        walden_id = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:1:teeTimeSlots:12:slotTee:0:slotTeeDIV"

        assert is_northgate_slot(northgate_id) is True
        assert is_northgate_slot(walden_id) is False

    def test_is_walden_slot(self):
        """Should correctly identify Walden slots."""
        northgate_id = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:0:teeTimeSlots:67:slotTee:0:slotTeeDIV"
        walden_id = "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:1:teeTimeSlots:12:slotTee:0:slotTeeDIV"

        assert is_walden_slot(northgate_id) is False
        assert is_walden_slot(walden_id) is True

    def test_course_headings_in_html(self, tee_time_page_html: BeautifulSoup):
        """Should find course headings for both courses."""
        headings = tee_time_page_html.select(WaldenDOMSchema.COURSE_HEADING)
        assert len(headings) >= 2

        heading_texts = [h.get_text(strip=True) for h in headings]
        assert any("Northgate" in t for t in heading_texts), "Northgate heading not found"
        assert any("Walden" in t for t in heading_texts), "Walden heading not found"


class TestSlotStatusClasses:
    """Tests for slot status identification."""

    def test_empty_slots_exist(self, tee_time_page_html: BeautifulSoup):
        """Should find Empty (available) slots."""
        elements = tee_time_page_html.select(f"div.{WaldenDOMSchema.SLOT_STATUS_EMPTY}")
        assert len(elements) >= 1, "No empty slots found"

    def test_reserved_slots_exist(self, tee_time_page_html: BeautifulSoup):
        """Should find Reserved slots."""
        elements = tee_time_page_html.select(f"div.{WaldenDOMSchema.SLOT_STATUS_RESERVED}")
        assert len(elements) >= 1, "No reserved slots found"

    def test_slot_status_extraction(self, tee_time_page_html: BeautifulSoup):
        """Should be able to extract slot status from div class."""
        slots = tee_time_page_html.select(WaldenDOMSchema.SLOT_DIV)
        assert len(slots) >= 50

        status_counts = {
            "Empty": 0,
            "Reserved": 0,
            "Weather delay": 0,
            "Block": 0,
            "ui-state-disabled": 0,
            "other": 0,
        }

        for slot in slots:
            classes = slot.get("class", [])
            class_str = " ".join(classes) if isinstance(classes, list) else classes

            if "Empty" in class_str:
                status_counts["Empty"] += 1
            elif "Reserved" in class_str:
                status_counts["Reserved"] += 1
            elif "Weather delay" in class_str:
                status_counts["Weather delay"] += 1
            elif "Block" in class_str:
                status_counts["Block"] += 1
            elif "ui-state-disabled" in class_str:
                status_counts["ui-state-disabled"] += 1
            else:
                status_counts["other"] += 1

        # Log counts for debugging
        print(f"\nSlot status counts: {status_counts}")

        # Should have a mix of statuses
        assert status_counts["Empty"] + status_counts["Reserved"] >= 10


class TestDatePicker:
    """Tests for date picker selectors."""

    def test_date_input_exists(self, tee_time_page_html: BeautifulSoup):
        """Date input field should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.DATE_INPUT)
        assert len(elements) >= 1

        # Should have a date value
        value = elements[0].get("value", "")
        assert "/" in value, f"Expected date format MM/DD/YYYY, got: {value}"

    def test_date_picker_trigger_exists(self, tee_time_page_html: BeautifulSoup):
        """Date picker trigger button should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.DATE_PICKER_TRIGGER)
        assert len(elements) >= 1

    def test_horizontal_date_picker_exists(self, tee_time_page_html: BeautifulSoup):
        """Horizontal date picker should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.HORIZONTAL_DATE_PICKER)
        assert len(elements) >= 1

    def test_horizontal_date_links_exist(self, tee_time_page_html: BeautifulSoup):
        """Should have multiple date links in horizontal picker."""
        picker = tee_time_page_html.select_one(WaldenDOMSchema.HORIZONTAL_DATE_PICKER)
        if picker:
            links = picker.select("a.ui-link")
            assert len(links) >= 5, "Expected at least 5 date links"


class TestDialogs:
    """Tests for dialog selectors."""

    def test_dialog_exists(self, tee_time_page_html: BeautifulSoup):
        """Dialog elements should exist (may be hidden)."""
        elements = tee_time_page_html.select(WaldenDOMSchema.DIALOG)
        # Dialogs exist but may be hidden
        assert len(elements) >= 1

    def test_confirm_dialog_exists(self, tee_time_page_html: BeautifulSoup):
        """Confirm dialog should exist."""
        elements = tee_time_page_html.select(WaldenDOMSchema.CONFIRM_DIALOG)
        assert len(elements) >= 1


class TestCourseIdentificationFromFixtures:
    """
    Tests that validate course identification using real element IDs from fixtures.

    These tests ensure the ID-based course detection works for all element types
    that _is_northgate_slot might receive: reserve buttons, slot links, available
    spans, and slot items.
    """

    def test_all_northgate_reserve_buttons_identified(self, tee_time_page_html: BeautifulSoup):
        """All reserve buttons in Northgate section should be identified as Northgate."""
        reserve_buttons = tee_time_page_html.select("a[id*='reserve_button']")
        assert len(reserve_buttons) >= 1, "No reserve buttons found in fixture"

        northgate_count = 0
        walden_count = 0

        for btn in reserve_buttons:
            btn_id = btn.get("id", "")
            if "teeTimeCourses:0" in btn_id:
                assert is_northgate_slot(btn_id), f"Northgate button not identified: {btn_id}"
                northgate_count += 1
            elif "teeTimeCourses:1" in btn_id:
                assert not is_northgate_slot(btn_id), f"Walden button wrongly identified as Northgate: {btn_id}"
                walden_count += 1

        print(f"\nReserve buttons - Northgate: {northgate_count}, Walden: {walden_count}")
        assert northgate_count >= 1, "Expected at least one Northgate reserve button"

    def test_all_slot_divs_identified_correctly(self, tee_time_page_html: BeautifulSoup):
        """All slot DIVs should be correctly identified by course."""
        slot_divs = tee_time_page_html.select("div[id*='slotTeeDIV']")
        assert len(slot_divs) >= 50, f"Expected at least 50 slot divs, found {len(slot_divs)}"

        northgate_count = 0
        walden_count = 0
        unknown_count = 0

        for div in slot_divs:
            div_id = div.get("id", "")
            course_index = get_course_index_from_element_id(div_id)

            if course_index == "0":
                assert is_northgate_slot(div_id), f"Northgate slot not identified: {div_id}"
                assert not is_walden_slot(div_id), f"Northgate slot wrongly identified as Walden: {div_id}"
                northgate_count += 1
            elif course_index == "1":
                assert not is_northgate_slot(div_id), f"Walden slot wrongly identified as Northgate: {div_id}"
                assert is_walden_slot(div_id), f"Walden slot not identified: {div_id}"
                walden_count += 1
            else:
                unknown_count += 1

        print(f"\nSlot DIVs - Northgate: {northgate_count}, Walden: {walden_count}, Unknown: {unknown_count}")
        assert northgate_count >= 20, "Expected at least 20 Northgate slots"
        assert walden_count >= 20, "Expected at least 20 Walden slots"
        assert unknown_count == 0, f"Found {unknown_count} slots with unknown course"

    def test_available_spans_parent_identification(self, tee_time_page_html: BeautifulSoup):
        """Available spans should have identifiable parent slot DIVs."""
        available_spans = tee_time_page_html.select("span.custom-free-slot-span")

        for span in available_spans[:10]:  # Check first 10
            # Walk up to find the slot DIV with course info
            parent = span.parent
            found_course = False
            for _ in range(10):  # Max 10 levels up
                if parent is None:
                    break
                parent_id = parent.get("id", "")
                if "teeTimeCourses:" in parent_id:
                    course_index = get_course_index_from_element_id(parent_id)
                    assert course_index in ("0", "1"), f"Invalid course index in: {parent_id}"
                    found_course = True
                    break
                parent = parent.parent

            assert found_course, "Could not find course info in parent hierarchy"

    def test_element_id_patterns(self, tee_time_page_html: BeautifulSoup):
        """Verify the element ID patterns we depend on exist in the real HTML."""
        # Find all elements with IDs containing teeTimeCourses
        all_elements_with_course_id = tee_time_page_html.select("[id*='teeTimeCourses']")
        assert len(all_elements_with_course_id) >= 100, "Expected many elements with course IDs"

        # Verify pattern consistency
        import re
        pattern = re.compile(r'teeTimeCourses:(\d+)')

        course_indices = set()
        for el in all_elements_with_course_id:
            el_id = el.get("id", "")
            match = pattern.search(el_id)
            if match:
                course_indices.add(match.group(1))

        assert "0" in course_indices, "Northgate course index (0) not found"
        assert "1" in course_indices, "Walden course index (1) not found"
        print(f"\nFound course indices: {sorted(course_indices)}")

    def test_clickable_elements_have_course_info(self, tee_time_page_html: BeautifulSoup):
        """
        All clickable elements (buttons, links) in slots should have course info
        either in their ID or in a parent's ID.
        """
        # These are the element types that _find_empty_slots returns
        clickable_selectors = [
            "a[id*='reserve_button']",  # Reserve buttons
            "a.slot-link",               # Slot links
        ]

        for selector in clickable_selectors:
            elements = tee_time_page_html.select(selector)
            if not elements:
                continue

            for el in elements[:5]:  # Check first 5 of each type
                el_id = el.get("id", "")

                # Either the element itself or a parent should have course info
                has_course_info = "teeTimeCourses:" in el_id

                if not has_course_info:
                    # Check parents
                    parent = el.parent
                    for _ in range(10):
                        if parent is None:
                            break
                        parent_id = parent.get("id", "")
                        if "teeTimeCourses:" in parent_id:
                            has_course_info = True
                            break
                        parent = parent.parent

                assert has_course_info, (
                    f"Clickable element has no course info in hierarchy: "
                    f"selector={selector}, id={el_id}"
                )
