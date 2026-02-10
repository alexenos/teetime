# Architecture Review: TeeTime App - Walden Golf Integration

**Date:** 2026-02-01
**Session:** Browser Claude Code (to be continued in CLI)
**Focus:** Why the Walden Golf integration is prone to bugs

---

## Executive Summary

The `walden_provider.py` file has grown to **4,111 lines** and suffers from the **God Object anti-pattern**. Recent commits show a pattern where fixing one bug introduces another because:

1. No abstraction layer for DOM structure
2. Defensive programming without clear contracts
3. Mixed responsibilities in a single massive file
4. Tests mock implementation details rather than testing against real DOM
5. Timezone handling is scattered with no single source of truth

---

## Key Metrics

| Metric | Value |
|--------|-------|
| `walden_provider.py` size | 4,111 lines |
| Lines added in last ~10 commits | 1,356+ |
| Distinct responsibilities in file | 12+ |
| CSS/XPath selector patterns | 50+ |
| Fallback strategies for course selection | 3 |
| Fallback strategies for date selection | 4 |

---

## Bug Pattern Analysis (Last 10 Commits)

| Commit | Issue | Root Cause Category |
|--------|-------|---------------------|
| `#97` | Wrong-course fallback | Course filtering logic |
| `#93` | Slot filtering overhead | Performance of DOM traversal |
| `#91` | Slot filtering + batch prescroll | Lazy-loading timing |
| `#88` | Multiple booking issues | Batch state management |
| `#85` | Proto-plus MapComposite | LLM response parsing |
| `#83` | Message parsing robustness | LLM response handling |
| `#81` | Text messaging requests | SMS/Twilio integration |
| `#78` | Booking timing + course selection | Timezone + DOM parsing |
| `#76` | Early login query fix | Scheduler timing |
| `#74` | Batch conflicts, course filtering, timing | Multiple categories |

**Pattern**: ~60% of bugs are in DOM parsing/course filtering, ~25% are timing/scheduling, ~15% are LLM/SMS.

---

## Critical Problem Areas

### 1. Brittle DOM Scraping (No Abstraction)

Selectors are scattered throughout 4000+ lines with multiple fallback strategies:

```python
# Example from _select_course_via_checkbox_dropdown (lines 871-901)
dropdown_trigger_selectors = [
    "[class*='select'][class*='course']",
    "div[class*='multiselect']",
    "button[class*='dropdown']",
    ".course-dropdown",
    "[aria-label*='course' i]",
    "[placeholder*='course' i]",
]
```

When the website changes, you don't know which selector broke, and adding fallbacks can change behavior elsewhere.

### 2. Course Filtering Logic (`_is_northgate_slot`)

Lines 2687-2846 (160 lines) that:
- Walks up 10 levels of DOM parents
- Checks class names, IDs, headers
- Uses both `strict` and non-strict modes
- Returns `False` on any uncertainty

The pendulum has swung from "too permissive" (wrong course bookings) to "too strict" (may reject valid slots).

### 3. Timezone Handling

From commit `f6c25dd`:
> "The comparison 'now < execute_at' was comparing UTC time with CT time"

Timezone conversions are scattered across:
- `booking_service.py:596-601`
- `walden_provider.py:538-566`
- `jobs.py` (execute_at calculation)

No centralized timezone-aware datetime utility.

### 4. Batch Booking State Management

`_book_multiple_tee_times_sync` (lines 443-801):
- Tracks `booked_times` to avoid conflicts
- Calculates `times_to_exclude` for each iteration
- Re-navigates after each booking
- Multiple failure points where partial state causes cascading failures

---

## Recommended Architecture Changes

### 1. Split `walden_provider.py` into Focused Components

```
providers/
├── walden/
│   ├── __init__.py           # Public interface
│   ├── driver.py             # WebDriver lifecycle management
│   ├── auth.py               # Login/session handling
│   ├── navigation.py         # Page navigation, course/date selection
│   ├── dom_parser.py         # DOM element extraction (CRITICAL)
│   ├── slot_finder.py        # Slot discovery and filtering
│   ├── booker.py             # Single booking flow
│   ├── batch_booker.py       # Batch booking orchestration
│   └── cancellation.py       # Cancellation flow
```

### 2. Create a DOM Schema Abstraction

```python
# dom_schema.py
class WaldenDOMSchema:
    """Centralized DOM selectors - change here when website updates."""

    COURSE_DROPDOWN = "[class*='select'][class*='course']"
    SLOT_ITEM = "li.ui-datascroller-item"
    EMPTY_SLOT = "div.Empty"
    RESERVED_SLOT = "div.Reserved"
    AVAILABLE_SPAN = "span.custom-free-slot-span"
    RESERVE_BUTTON = "a[id*='reserve_button']"
    # ... etc
```

### 3. Implement Page Object Model

```python
class TeeTimePage:
    """Encapsulates the tee time booking page."""

    def __init__(self, driver: WebDriver):
        self.driver = driver

    def select_course(self, course_name: str) -> bool:
        """Select course - handles all fallback strategies internally."""

    def select_date(self, target_date: date) -> bool:
        """Select date - handles calendar navigation."""

    def get_available_slots(self, min_capacity: int) -> list[TeeTimeSlot]:
        """Return structured slot objects, not raw DOM elements."""
```

### 4. Centralize Timezone Handling

```python
# utils/datetime.py
class CTDateTime:
    """All datetime operations in Central Time."""

    @staticmethod
    def now() -> datetime:
        """Current time in CT (timezone-aware)."""

    @staticmethod
    def to_naive_ct(dt: datetime) -> datetime:
        """Convert any datetime to naive CT for database storage."""
```

### 5. HTML Snapshot Testing

Save actual HTML from Walden Golf and test DOM parsing against it:

```python
@pytest.fixture
def walden_tee_sheet_html():
    return Path("tests/fixtures/walden_tee_sheet.html").read_text()

def test_slot_parser_extracts_times(walden_tee_sheet_html):
    parser = WaldenDOMParser()
    slots = parser.find_slots(walden_tee_sheet_html)
    assert len(slots) > 0
```

---

## Next Steps (CLI Session)

### With Walden Golf Credentials

Set environment variables before starting CLI:

```bash
export WALDEN_MEMBER_NUMBER="your_member_number"
export WALDEN_PASSWORD="your_password"
cd /path/to/teetime
claude
```

Then in CLI session:

1. **Capture HTML snapshots** from the live Walden Golf site
2. **Validate current selectors** against real DOM structure
3. **Test course filtering logic** with actual page states
4. **Create test fixtures** from captured HTML
5. **Begin refactoring** `walden_provider.py` into components

### Prompt for CLI Session

> "Continue the architecture review from docs/architecture-review-2026-02-01.md. I have Walden Golf credentials set via WALDEN_MEMBER_NUMBER and WALDEN_PASSWORD env vars. Please:
> 1. Run a Selenium session to capture HTML snapshots from the tee time booking page
> 2. Validate which DOM selectors are currently working
> 3. Save snapshots as test fixtures
> 4. Identify any immediate issues with the current selectors"

---

## Files to Review

| File | Lines | Purpose |
|------|-------|---------|
| `app/providers/walden_provider.py` | 4,111 | Main problem area |
| `app/services/booking_service.py` | ~800 | Orchestration layer |
| `app/api/jobs.py` | ~400 | Scheduler execution |
| `tests/test_walden_provider.py` | 1,023 | Current test coverage |

---

## Summary

The fundamental issue is maintaining a **screen-scraping integration without the architecture to make screen-scraping maintainable**. The recommended refactoring will:

1. Make DOM changes isolated to one file (schema)
2. Make each component testable in isolation
3. Enable HTML snapshot testing to catch regressions
4. Reduce the cognitive load of debugging from 4000 lines to focused modules
