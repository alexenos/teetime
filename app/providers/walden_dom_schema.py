"""
Centralized DOM schema for the Walden Golf / Northstar Technologies booking site.

All CSS selectors and XPath expressions used by WaldenGolfProvider are defined here
as named constants, grouped by functional area. Fallback chains (where multiple
selectors are tried in priority order) are represented as tuples of strings.

This module is the single source of truth for DOM element identification.
When the Walden Golf site changes its markup, update selectors ONLY in this file.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LoginSelectors:
    """Selectors for the Liferay login form."""

    member_input_name: str = "_com_liferay_login_web_portlet_LoginPortlet_login"
    password_input_name: str = "_com_liferay_login_web_portlet_LoginPortlet_password"
    submit_button: str = 'button[type="submit"]'


@dataclass(frozen=True)
class CourseSelectionSelectors:
    """Selectors for the course selection UI (checkbox dropdown and standard dropdown)."""

    # Checkbox dropdown trigger buttons (tried in order)
    dropdown_triggers: tuple[str, ...] = (
        "[class*='select'][class*='course']",
        "div[class*='multiselect']",
        "button[class*='dropdown']",
        ".course-dropdown",
        "[aria-label*='course' i]",
        "[placeholder*='course' i]",
    )
    # XPath fallback for dropdown trigger
    dropdown_trigger_xpaths: tuple[str, ...] = (
        "//*[contains(text(), '{course_name}')][self::button or self::div[contains(@class, 'trigger')]]",
        "//*[contains(text(), '{course_name}')][self::a or self::span]",
        "//*[contains(text(), '{course_name}')][contains(@class, 'select') or contains(@class, 'dropdown')]",
        "//*[contains(text(), '{course_name}')][self::div]",
    )
    # Checkbox items within the dropdown panel
    checkbox_items_css: tuple[str, ...] = (
        "input[type='checkbox']",
        "li[class*='option']",
        "div[class*='option']",
        "label[class*='checkbox']",
    )
    checkbox_items_xpaths: tuple[str, ...] = (
        "//li[.//input[@type='checkbox']]",
        "//div[contains(@class, 'option')]",
        "//label[contains(@class, 'check')]",
    )
    # Close button for checkbox dropdown
    close_button: str = "[class*='close'], .x, button[aria-label='close']"
    # Standard <select> dropdown fallbacks
    standard_dropdowns: tuple[str, ...] = (
        "select[id*='course']",
        "select[name*='course']",
        "select[id*='Course']",
        "select[name*='Course']",
        "select.course-select",
        "#courseSelect",
    )
    # Course verification
    selected_options: str = "select option:checked, select option[selected]"
    # Course verification XPath (case-insensitive text matching)
    verification_xpath_template: str = (
        "//*[contains(translate(text(), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'{course_name_lower}')]"
    )


@dataclass(frozen=True)
class DateSelectionSelectors:
    """Selectors for date selection (input fields, calendar picker, day tabs)."""

    # Date input fields (tried in order)
    date_inputs: tuple[str, ...] = (
        "input[type='text'][id*='date']",
        "input[type='date']",
        "input[id*='date']",
        "input[name*='date']",
        "input[class*='date']",
        "input[placeholder*='date' i]",
        "input[placeholder*='mm/dd' i]",
        ".datepicker input",
        "[data-date] input",
    )
    # Search/submit button after date entry
    search_submit: str = (
        "button[type='submit'], input[type='submit'], button.search, .btn-search"
    )
    # Calendar trigger buttons
    calendar_triggers: str = (
        ".calendar-trigger, .datepicker-trigger, [class*='calendar'], "
        "button[aria-label*='calendar' i], .ui-datepicker-trigger, "
        "span.icon-calendar, i.fa-calendar"
    )
    # Calendar popup detection (wait for any of these)
    calendar_popup: str = (
        ".ui-datepicker, .datepicker, [class*='calendar-popup'], "
        ".ui-datepicker-calendar, select[class*='month'], select[class*='year']"
    )
    # Month dropdown selectors
    month_dropdown: str = (
        "select.ui-datepicker-month, select[class*='month'], "
        "select[data-handler='selectMonth'], select[name*='month']"
    )
    # Year dropdown selectors
    year_dropdown: str = (
        "select.ui-datepicker-year, select[class*='year'], "
        "select[data-handler='selectYear'], select[name*='year']"
    )
    # Calendar header for reading current month/year
    calendar_headers: tuple[str, ...] = (
        ".ui-datepicker-title",
        ".datepicker-title",
        "[class*='calendar-header']",
        "[class*='datepicker-header']",
    )
    # Forward navigation arrows (tried in order)
    nav_next: tuple[str, ...] = (
        "a.ui-datepicker-next",
        "button.ui-datepicker-next",
        "[data-handler='next']",
        ".ui-datepicker-next",
        "a[title='Next']",
        "button[title='Next']",
        "span.ui-icon-circle-triangle-e",
        "[class*='next']",
        "a[class*='next']",
        "button[class*='next']",
    )
    # Backward navigation arrows (tried in order)
    nav_prev: tuple[str, ...] = (
        "a.ui-datepicker-prev",
        "button.ui-datepicker-prev",
        "[data-handler='prev']",
        ".ui-datepicker-prev",
        "a[title='Prev']",
        "button[title='Prev']",
        "span.ui-icon-circle-triangle-w",
        "[class*='prev']",
        "a[class*='prev']",
        "button[class*='prev']",
    )
    # Day element XPath templates (use .format(day=N))
    day_xpaths: tuple[str, ...] = (
        "//td[@data-date='{day}']",
        "//a[text()='{day}']",
        "//td[contains(@class, 'day') and text()='{day}']",
        "//td[normalize-space(text())='{day}']",
    )
    # Classes indicating a day belongs to another month
    other_month_classes: tuple[str, ...] = (
        "ui-datepicker-other-month",
        "disabled",
    )
    # Tee time presence indicator (confirms date selection loaded slots)
    tee_time_presence: str = (
        ".custom-free-slot-span, .teetime-row, [class*='tee-time'], "
        "li.ui-datascroller-item"
    )
    # Day tab selectors (alternative date selection method)
    day_tabs: str = (
        ".day-tab, [class*='day-tab'], a[href*='day'], [data-day], "
        ".teetime-day-tab, .nav-tabs a"
    )


@dataclass(frozen=True)
class SlotDiscoverySelectors:
    """Selectors for discovering and parsing tee time slots on the datascroller."""

    # Individual slot items in the datascroller list
    slot_items: str = "li.ui-datascroller-item"
    # Datascroller container (for scrolling to load more items)
    datascroller_content: str = ".ui-datascroller-content, .ui-datascroller-list"
    # Completely empty slot marker
    empty_slot: str = "div.Empty"
    # Reserve button within an empty slot (tried in order)
    reserve_buttons: tuple[str, ...] = (
        "a[id*='reserve_button']",
        "a.slot-link",
    )
    # Available slot span (indicates open spots)
    available_span: str = "span.custom-free-slot-span"
    # Available slot clickable link
    available_link: str = "a.custom-free-slot-link"
    # Reserved slot div (partially booked)
    reserved_slot: str = "div.Reserved"
    # Page loaded indicator (generic - used after navigation)
    page_loaded: str = (
        ".custom-free-slot-span, .teetime-row, [class*='tee-time'], form"
    )
    # Course section container
    course_section: str = ".course-section, [class*='course']"
    # Reserve buttons XPath (table-based layout fallback)
    reserve_buttons_xpath: str = (
        ".//a[contains(text(), 'Reserve')] | .//button[contains(text(), 'Reserve')]"
    )
    # Available links XPath (table-based layout fallback)
    available_links_xpath: str = ".//a[contains(text(), 'Available')]"
    # Table time cell (for extracting time from table rows)
    table_time_cell: str = "td:first-child, .time-cell"
    # Row ancestor XPath (for finding the row container from a span)
    row_ancestor_xpath: str = "./ancestor::tr"


@dataclass(frozen=True)
class BookingModalSelectors:
    """Selectors for the booking modal/dialog that appears after clicking Reserve.

    After clicking a Reserve button, a modal/dialog may appear containing the
    player count selector, player table, and Book Now button. If detected,
    all subsequent booking operations should be scoped to this modal element
    to avoid matching elements on the underlying tee sheet page.
    """

    # Modal/dialog container (wait for any of these after clicking Reserve)
    modal_container: str = (
        ".modal, .dialog, [class*='popup'], form[class*='booking'], [class*='confirm']"
    )


@dataclass(frozen=True)
class PlayerCountSelectors:
    """Selectors for the player count button group WITHIN the booking modal.

    IMPORTANT: These selectors MUST be scoped to the booking modal element,
    not the full page. The generic .ui-selectonebutton class is a PrimeFaces
    widget used by BOTH the time period filter (ALL/MORNING/AFTERNOON/AVAILABLE
    with radio values 0-3) and the player count button group (with radio values
    1-4). Searching the full page matches the time period filter first,
    causing booking failures (see Issue #105).
    """

    # Button group container (tried in order, MUST search within modal)
    button_group: tuple[str, ...] = (
        ".reservation-players",
        ".ui-selectonebutton",
        "[class*='players-sel']",
    )
    # Radio input template (use .format(value=num_players))
    radio_input_template: str = "input[type='radio'][value='{value}']"
    # XPath to get parent button div from radio input
    button_parent_xpath: str = "./.."
    # Class indicating a button is disabled
    disabled_class: str = "ui-state-disabled"
    # Candidate buttons within the group (fallback text matching strategy)
    candidate_buttons: str = ".ui-button, button, a, span"
    # Standard <select> dropdown fallbacks for player count
    dropdown_fallbacks: tuple[str, ...] = (
        "select[id*='player']",
        "select[id*='golfer']",
        "select[name*='player']",
        "select[name*='golfer']",
        "select[id*='numPlayers']",
        "select[id*='numberOfPlayers']",
    )
    # Player row verification (wait for these after selecting count)
    player_rows_wait: str = (
        "[id*='playersTable'] tbody tr, table[id*='player'] tbody tr"
    )
    # Player row selectors (tried in order for verification)
    player_rows: tuple[str, ...] = (
        "[id*='playersTable'] tbody tr[data-ri]",
        "[id*='player'] tbody tr[data-ri]",
        "table[id*='player'] tbody tr",
        ".player-row",
        "[class*='player-row']",
    )


@dataclass(frozen=True)
class TBDGuestSelectors:
    """Selectors for adding TBD Registered Guests to player slots."""

    # Player row selectors (tried in order, includes extra fallback)
    player_rows: tuple[str, ...] = (
        "[id*='playersTable'] tbody tr[data-ri]",
        "[id*='player'] tbody tr[data-ri]",
        "table[id*='player'] tbody tr",
        ".player-row",
        "[class*='player-row']",
        "form table tbody tr",
    )
    # Player rows wait selector
    player_rows_wait: str = (
        "[id*='playersTable'] tbody tr, table[id*='player'] tbody tr"
    )
    # TBD button CSS selectors (tried per row, in order)
    tbd_button_css: tuple[str, ...] = (
        "a[id*='tbd']",
        "span[id*='tbd']",
        "button[id*='tbd']",
        "[class*='btn-tbd']",
        "a[class*='tbd']",
        "span[class*='tbd']",
        "button[class*='tbd']",
        "a[id*='TBD']",
        "span[id*='TBD']",
        "button[id*='TBD']",
        "[class*='TBD']",
        "a.ui-commandlink",
        "button.ui-button",
    )
    # TBD button XPath (tried per row)
    tbd_button_xpath: str = (
        ".//a[contains(text(), 'TBD')] | "
        ".//span[contains(text(), 'TBD')] | "
        ".//button[contains(text(), 'TBD')] | "
        ".//*[contains(@title, 'TBD')] | "
        ".//*[contains(@aria-label, 'TBD')]"
    )
    # Generic clickable elements scan (last resort)
    clickable_elements: str = "a, button, span[onclick], div[onclick]"
    # Player name input fallbacks (for direct text entry)
    player_name_inputs: tuple[str, ...] = (
        "input[id*='player_input']",
        "input[id*='player']",
        "input[name*='player']",
        "input[type='text']",
        "input.ui-autocomplete-input",
    )


@dataclass(frozen=True)
class BookingCompletionSelectors:
    """Selectors for the final booking confirmation step."""

    # Book Now button by ID (preferred, most specific)
    book_now_by_id: str = "a[id*='bookTeeTimeAction']"
    # Book Now button wait selector (includes text-based fallback)
    book_now_wait: str = (
        "a[id*='bookTeeTimeAction'], a:contains('Book Now'), button:contains('Book')"
    )
    # Book Now button XPath fallbacks
    book_now_xpaths: tuple[str, ...] = (
        "//a[contains(., 'Book Now')]",
        "//a[contains(., 'Book')]",
        "//button[contains(., 'Confirm')]",
        "//button[contains(., 'Submit')]",
        "//button[contains(., 'Book')]",
        "//input[@type='submit']",
    )
    # Success indicators XPath
    success_indicators_xpath: str = (
        "//*[contains(text(), 'success') or "
        "contains(text(), 'confirm') or "
        "contains(text(), 'thank')]"
    )


@dataclass(frozen=True)
class ErrorMessageSelectors:
    """Selectors for error/alert message containers."""

    containers: tuple[str, ...] = (
        ".ui-messages-error",
        ".ui-message-error",
        ".ui-growl-message-error",
        ".error",
        ".errors",
        "[class*='error']",
        ".alert",
        ".alert-danger",
        "[role='alert']",
        "[aria-live='assertive']",
        "[aria-live='polite']",
    )


@dataclass(frozen=True)
class CancellationSelectors:
    """Selectors for the reservation cancellation flow."""

    # Reservations page presence
    dashboard_presence: str = "form, .reservations, [class*='reservation']"
    # Reservations form
    reservations_form: str = "form[name*='memberReservations']"
    # Reservation table rows
    reservation_rows: str = "table tbody tr"
    # Cancel button/link (tried as comma-separated CSS)
    cancel_link: str = (
        "a[aria-label='Cancel Reservation'], "
        "a[title='Cancel Reservation'], "
        "a[class*='cancel'], "
        "button[class*='cancel']"
    )
    # Confirm cancellation CSS selectors (tried in order)
    confirm_css: tuple[str, ...] = (
        "button[class*='confirm']",
        "button[class*='yes']",
        "input[type='submit'][value*='Yes']",
        "input[type='submit'][value*='Confirm']",
        ".modal button[class*='primary']",
    )
    # Confirm cancellation XPath fallbacks (tried in order)
    confirm_xpaths: tuple[str, ...] = (
        "//button[contains(text(), 'Yes')]",
        "//button[contains(text(), 'Confirm')]",
        "//button[contains(text(), 'OK')]",
        "//a[contains(text(), 'Yes')]",
        "//a[contains(text(), 'Confirm')]",
        "//*[contains(@class, 'ui-dialog')]//button[contains(text(), 'Yes')]",
    )


@dataclass(frozen=True)
class CourseFilteringSelectors:
    """Selectors used by _is_northgate_slot for course identification."""

    # Course name headers (searched within parent elements)
    course_name_headers: str = "h1, h2, h3, h4, .course-name, .course-header"
    # Parent traversal XPath
    parent_xpath: str = "./.."


@dataclass(frozen=True)
class DebugSelectors:
    """Selectors used for diagnostic/debug logging."""

    # Clickable elements in a row (for _log_row_element_state)
    row_clickables: str = "a, button, span[onclick], input, select"
    # Table context elements
    table_context: str = "[id*='player'], [class*='player'], table"
    # Table rows
    table_rows: str = "tr"


# ---- Module-level singleton instances ----
# Import and use as: from app.providers.walden_dom_schema import DOM
# Then reference: DOM.LOGIN.member_input_name, DOM.PLAYER_COUNT.button_group, etc.


@dataclass(frozen=True)
class WaldenDOMSchema:
    """Top-level container grouping all selector categories."""

    LOGIN: LoginSelectors = LoginSelectors()
    COURSE_SELECTION: CourseSelectionSelectors = CourseSelectionSelectors()
    DATE_SELECTION: DateSelectionSelectors = DateSelectionSelectors()
    SLOT_DISCOVERY: SlotDiscoverySelectors = SlotDiscoverySelectors()
    BOOKING_MODAL: BookingModalSelectors = BookingModalSelectors()
    PLAYER_COUNT: PlayerCountSelectors = PlayerCountSelectors()
    TBD_GUESTS: TBDGuestSelectors = TBDGuestSelectors()
    BOOKING_COMPLETION: BookingCompletionSelectors = BookingCompletionSelectors()
    ERROR_MESSAGES: ErrorMessageSelectors = ErrorMessageSelectors()
    CANCELLATION: CancellationSelectors = CancellationSelectors()
    COURSE_FILTERING: CourseFilteringSelectors = CourseFilteringSelectors()
    DEBUG: DebugSelectors = DebugSelectors()


# Single import point: `from app.providers.walden_dom_schema import DOM`
DOM = WaldenDOMSchema()
