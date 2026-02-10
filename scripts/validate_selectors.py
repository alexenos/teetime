#!/usr/bin/env python3
"""
Validate DOM selectors from walden_provider.py against captured HTML fixtures.

This script:
1. Extracts all CSS selectors from walden_provider.py
2. Tests each selector against the captured HTML fixtures
3. Reports which selectors work and which are broken

Usage:
    python scripts/validate_selectors.py
"""

import json
from pathlib import Path

from bs4 import BeautifulSoup

# Selectors to validate against captured HTML fixtures.
# This includes both working selectors (from WaldenDOMSchema) and potentially
# broken selectors (e.g., PrimeVue p-* selectors) to detect regressions and
# identify which fallback strategies are needed.
SELECTORS = {
    "login": {
        "member_input": "input[name='_com_liferay_login_web_portlet_LoginPortlet_login']",
        "password_input": "input[name='_com_liferay_login_web_portlet_LoginPortlet_password']",
        "submit_button": "button[type='submit']",
    },
    "course_dropdown": {
        "dropdown_trigger_1": "[class*='select'][class*='course']",
        "dropdown_trigger_2": "div[class*='multiselect']",
        "dropdown_trigger_3": "button[class*='dropdown']",
        "dropdown_trigger_4": ".course-dropdown",
        "dropdown_trigger_5": "[aria-label*='course']",
        "dropdown_trigger_6": "[placeholder*='course']",
        "multiselect": "div.p-multiselect",
        "multiselect_label": "span.p-multiselect-label",
        "multiselect_trigger": "div.p-multiselect-trigger",
    },
    "course_options": {
        "checkbox_item": "li.p-multiselect-item",
        "checkbox_label": "span.p-checkbox-label",
        "checkbox": "div.p-checkbox",
        "northgate_option": "[aria-label*='Northgate']",
    },
    "date_picker": {
        "datepicker_1": "[class*='datepicker']",
        "datepicker_2": "span.p-datepicker-trigger",
        "datepicker_3": "input.p-inputtext",
        "calendar": "[class*='calendar']",
        "calendar_header": ".p-datepicker-header",
        "calendar_next": "button.p-datepicker-next",
        "calendar_prev": "button.p-datepicker-prev",
        "calendar_day": "td span:not(.p-disabled)",
    },
    "tee_time_slots": {
        "datascroller_list": "ul.ui-datascroller-list",
        "slot_item": "li.ui-datascroller-item",
        "empty_slot": "div.Empty",
        "reserved_slot": "div.Reserved",
        "available_span": "span.custom-free-slot-span",
        "slot_time": "span[class*='time']",
        "slot_info": "div[class*='slot-info']",
    },
    "booking_controls": {
        "reserve_button": "a[id*='reserve_button']",
        "reserve_link": "a[href*='reserve']",
        "player_count_select": "select[id*='player']",
        "confirm_button": "button[id*='confirm']",
        "submit_booking": "button[type='submit']",
    },
    "course_filtering": {
        "course_header": "[class*='course-header']",
        "course_name": "[class*='course-name']",
        "northgate_indicator": "[class*='northgate']",
        "course_section": "div[data-course]",
    },
    "primefaces_components": {
        "pf_datascroller": ".ui-datascroller",
        "pf_datascroller_content": ".ui-datascroller-content",
        "pf_datascroller_loader": ".ui-datascroller-loader",
        "pf_panel": ".ui-panel",
        "pf_dialog": ".ui-dialog",
        "pf_growl": ".ui-growl",
    },
}


def load_html(fixture_name: str) -> BeautifulSoup | None:
    """Load an HTML fixture and return BeautifulSoup object."""
    fixtures_dir = Path(__file__).parent.parent / "tests" / "fixtures"
    html_path = fixtures_dir / f"{fixture_name}.html"

    if not html_path.exists():
        return None

    html = html_path.read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


def test_selector(soup: BeautifulSoup, selector: str) -> tuple[int, list[str]]:
    """Test a CSS selector against HTML and return match count and sample text."""
    try:
        elements = soup.select(selector)
        samples = []
        for el in elements[:3]:  # First 3 matches
            text = el.get_text(strip=True)[:50]
            classes = el.get("class", [])
            class_str = ".".join(classes) if classes else ""
            tag = el.name
            samples.append(f"<{tag} class='{class_str}'>{text}...")
        return len(elements), samples
    except Exception as e:
        return -1, [f"ERROR: {e}"]


def validate_selectors():
    """Main validation routine."""
    print("=" * 70)
    print("DOM Selector Validation Report")
    print("=" * 70)

    # Load fixtures
    fixtures = {
        "login": load_html("walden_login_page"),
        "tee_time": load_html("walden_tee_time_loaded"),
        "tee_time_final": load_html("walden_tee_time_final"),
    }

    for name, soup in fixtures.items():
        if soup is None:
            print(f"WARNING: Fixture '{name}' not found")

    results = {
        "working": [],
        "broken": [],
        "errors": [],
    }

    # Test each category of selectors
    for category, selectors in SELECTORS.items():
        print(f"\n{'=' * 70}")
        print(f"Category: {category.upper()}")
        print("=" * 70)

        # Choose appropriate fixture for this category
        if category == "login":
            soup = fixtures.get("login")
        else:
            soup = fixtures.get("tee_time") or fixtures.get("tee_time_final")

        if soup is None:
            print("  SKIPPED: No fixture available")
            continue

        for name, selector in selectors.items():
            count, samples = test_selector(soup, selector)

            if count > 0:
                status = "[OK] FOUND"
                results["working"].append((category, name, selector, count))
            elif count == 0:
                status = "[X] NOT FOUND"
                results["broken"].append((category, name, selector))
            else:
                status = "[!] ERROR"
                results["errors"].append((category, name, selector, samples[0]))

            print(f"\n  {name}:")
            print(f"    Selector: {selector}")
            print(f"    Status: {status} ({count} matches)")
            if samples and count > 0:
                for sample in samples:
                    print(f"    Sample: {sample}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print(f"\n[OK] Working selectors: {len(results['working'])}")
    print(f"[X]  Broken selectors:  {len(results['broken'])}")
    print(f"[!]  Error selectors:   {len(results['errors'])}")

    if results["broken"]:
        print("\n" + "-" * 70)
        print("BROKEN SELECTORS (need attention):")
        print("-" * 70)
        for category, name, selector in results["broken"]:
            print(f"  [{category}] {name}: {selector}")

    if results["errors"]:
        print("\n" + "-" * 70)
        print("ERROR SELECTORS (invalid syntax?):")
        print("-" * 70)
        for category, name, selector, error in results["errors"]:
            print(f"  [{category}] {name}: {selector}")
            print(f"    Error: {error}")

    # Save report
    report_path = Path(__file__).parent.parent / "tests" / "fixtures" / "selector_report.json"
    report = {
        "working": [
            {"category": c, "name": n, "selector": s, "count": cnt}
            for c, n, s, cnt in results["working"]
        ],
        "broken": [{"category": c, "name": n, "selector": s} for c, n, s in results["broken"]],
        "errors": [
            {"category": c, "name": n, "selector": s, "error": e}
            for c, n, s, e in results["errors"]
        ],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport saved to: {report_path}")


if __name__ == "__main__":
    validate_selectors()
