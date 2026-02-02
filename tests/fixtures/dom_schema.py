"""
DOM Schema for Walden Golf Website

This module defines the actual DOM selectors discovered from live HTML snapshots
captured on 2026-02-01. Use these selectors as the source of truth for testing
and for the walden_provider.py refactoring.

Key findings from HTML analysis:
1. Site uses PrimeFaces (ui-*) components, NOT PrimeVue (p-*)
2. Course dropdown is ui-selectcheckboxmenu, not p-multiselect
3. Courses are differentiated by `teeTimeCourses:0` (Northgate) vs `teeTimeCourses:1` (Walden)
4. Slot status is in div class: "Empty", "Reserved", "Weather delay", "Block", "ui-state-disabled"
5. Time is in label.custom-time-label within each slot
6. Reserve button has id containing "reserve_button"
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WaldenDOMSchema:
    """
    Centralized DOM selectors for Walden Golf website.

    All selectors have been validated against live HTML snapshots.
    When the website changes, update selectors HERE and run validation.
    """

    # ========== LOGIN PAGE ==========
    LOGIN_MEMBER_INPUT = "input[name='_com_liferay_login_web_portlet_LoginPortlet_login']"
    LOGIN_PASSWORD_INPUT = "input[name='_com_liferay_login_web_portlet_LoginPortlet_password']"
    LOGIN_SUBMIT_BUTTON = "button[type='submit']"

    # ========== COURSE DROPDOWN (ui-selectcheckboxmenu) ==========
    # The course selector is NOT a p-multiselect, it's a PrimeFaces selectcheckboxmenu
    COURSE_DROPDOWN = "div.ui-selectcheckboxmenu.course-sel"
    COURSE_DROPDOWN_LABEL = "label.ui-selectcheckboxmenu-label"
    COURSE_DROPDOWN_TRIGGER = "div.ui-selectcheckboxmenu-trigger"

    # Course checkboxes (hidden, inside the dropdown)
    COURSE_CHECKBOX_NORTHGATE = "input[id*='j_idt101:0']"  # value="2"
    COURSE_CHECKBOX_WALDEN = "input[id*='j_idt101:1']"     # value="1"
    COURSE_CHECKBOX_FAST_FIVE = "input[id*='j_idt101:2']"  # value="3"

    # ========== DATE PICKER ==========
    DATE_INPUT = "input.hasDatepicker[id*='j_idt104_input']"
    DATE_PICKER_TRIGGER = "button.ui-datepicker-trigger"
    DATE_PICKER_POPUP = "div.ui-datepicker"
    DATE_PICKER_PREV = "a.ui-datepicker-prev"
    DATE_PICKER_NEXT = "a.ui-datepicker-next"
    DATE_PICKER_DAY = "td[data-handler='selectDay'] a"

    # Horizontal date picker (alternative navigation)
    HORIZONTAL_DATE_PICKER = "div.horizontal-date-picker"
    HORIZONTAL_DATE_LINK = "a.ui-link"
    HORIZONTAL_DATE_SELECTED = "a.selected-date"
    HORIZONTAL_PREV_WEEK = "span[id*='j_idt120']"  # Double left arrow
    HORIZONTAL_PREV_DAY = "span[id*='j_idt122']"   # Single left arrow
    HORIZONTAL_NEXT_DAY = "span[id*='j_idt130']"   # Single right arrow
    HORIZONTAL_NEXT_WEEK = "span[id*='j_idt132']"  # Double right arrow

    # ========== HOLES SELECTOR ==========
    HOLES_DROPDOWN = "div.ui-selectonemenu.holesDropDown"
    HOLES_DROPDOWN_LABEL = "label.ui-selectonemenu-label"

    # ========== TIME PERIOD SELECTOR ==========
    TIME_PERIOD_BUTTONS = "div.ui-selectonebutton.timePeriodSel"
    TIME_PERIOD_ALL = "input[value='0']"
    TIME_PERIOD_MORNING = "input[value='1']"
    TIME_PERIOD_AFTERNOON = "input[value='2']"
    TIME_PERIOD_AVAILABLE = "input[value='3']"

    # ========== COURSE SECTIONS ==========
    # Each course has its own section identified by teeTimeCourses index
    COURSE_VIEWS_DIV = "div.courseViewsDIV"
    COURSE_SLOTS_CONTAINER = "span.courseSlots"
    COURSE_HEADING = "label.course-slots-heading"
    COURSE_RESTRICTION = "label.course-restriction"

    # Course identification by index in element IDs
    # teeTimeCourses:0 = Northgate
    # teeTimeCourses:1 = Walden on Lake Conroe
    NORTHGATE_COURSE_INDEX = "0"
    WALDEN_COURSE_INDEX = "1"

    # ========== TEE TIME SLOTS (DataScroller) ==========
    DATASCROLLER = "div.ui-datascroller"
    DATASCROLLER_CONTENT = "div.ui-datascroller-content"
    DATASCROLLER_LIST = "ul.ui-datascroller-list"
    DATASCROLLER_ITEM = "li.ui-datascroller-item"
    DATASCROLLER_LOADER = "div.ui-datascroller-loader"

    # Slot container div (has status class)
    SLOT_DIV = "div[id*='slotTeeDIV']"

    # Slot status classes (on the slotTeeDIV div)
    SLOT_STATUS_EMPTY = "Empty"           # Available for booking
    SLOT_STATUS_RESERVED = "Reserved"     # Already booked
    SLOT_STATUS_WEATHER = "Weather delay" # Weather delay
    SLOT_STATUS_BLOCK = "Block"           # Blocked
    SLOT_STATUS_DISABLED = "ui-state-disabled"  # Disabled

    # Slot time
    SLOT_TIME_LABEL = "label.custom-time-label"
    SLOT_TIME_DIV = "div.time-div"

    # Slot content
    SLOT_AREA_DIV = "div[id*='slotAreaDIV']"
    SLOT_HEADING = "span.tee-heading"
    SLOT_SUBHEADING = "span.tee-subheading"

    # ========== RESERVE BUTTON ==========
    RESERVE_BUTTON = "a[id*='reserve_button']"
    RESERVE_BUTTON_CLASS = "a.custom-res-btn"

    # ========== RESERVED SLOT PLAYER INFO ==========
    PLAYER_NAME_SPAN = "span.member-name"
    AVAILABLE_SLOT_SPAN = "span.custom-free-slot-span"

    # ========== DIALOGS ==========
    DIALOG = "div.ui-dialog"
    CONFIRM_DIALOG = "div.ui-confirm-dialog"
    DIALOG_TITLE = "span.ui-dialog-title"
    DIALOG_CLOSE = "a.ui-dialog-titlebar-close"

    # ========== MESSAGES ==========
    GROWL = "div.ui-growl"
    GROWL_MESSAGE = "div.ui-growl-message"


# Selector validation mapping
# Maps selector names to their expected fixture and minimum match count
SELECTOR_VALIDATION = {
    # Login page selectors
    "LOGIN_MEMBER_INPUT": ("walden_login_page", 1),
    "LOGIN_PASSWORD_INPUT": ("walden_login_page", 1),
    "LOGIN_SUBMIT_BUTTON": ("walden_login_page", 1),

    # Tee time page selectors
    "COURSE_DROPDOWN": ("walden_tee_time_loaded", 1),
    "DATASCROLLER_LIST": ("walden_tee_time_loaded", 1),
    "DATASCROLLER_ITEM": ("walden_tee_time_loaded", 50),  # Should have many slots
    "RESERVE_BUTTON": ("walden_tee_time_loaded", 1),      # At least one available slot
    "SLOT_TIME_LABEL": ("walden_tee_time_loaded", 50),
    "COURSE_HEADING": ("walden_tee_time_loaded", 2),      # Northgate + Walden
}


def get_course_index_from_element_id(element_id: str) -> str | None:
    """
    Extract the course index from an element ID.

    Example:
        "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:0:teeTimeSlots:67:..."
        Returns "0" (Northgate)

        "_teeTimePortlet_WAR_northstarportlet_:teeTimeForm:teeTimeCourses:1:teeTimeSlots:12:..."
        Returns "1" (Walden)
    """
    import re
    match = re.search(r'teeTimeCourses:(\d+)', element_id)
    if match:
        return match.group(1)
    return None


def is_northgate_slot(element_id: str) -> bool:
    """
    Determine if an element belongs to the Northgate course.

    This is the RELIABLE way to filter slots - by checking the course index
    in the element ID, not by walking DOM parents or checking class names.
    """
    return get_course_index_from_element_id(element_id) == WaldenDOMSchema.NORTHGATE_COURSE_INDEX


def is_walden_slot(element_id: str) -> bool:
    """
    Determine if an element belongs to the Walden on Lake Conroe course.
    """
    return get_course_index_from_element_id(element_id) == WaldenDOMSchema.WALDEN_COURSE_INDEX
