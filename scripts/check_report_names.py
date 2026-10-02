"""
Fail if a race report names a person the morning's evidence names.

The repository is public, and CLAUDE.md forbids member names, phone numbers
and Telegram handles anywhere in it. A race report is written from logs and
tee sheets that carry all three, and the race-report Routine merges its own
report with no human reading it first. This is the check that runs before
that commit. See ``.claude/skills/race-report/SKILL.md`` §8a.

Two sources of forbidden text:

1. **Every name on every tee sheet under ``--artifacts``** - holders and TBD
   placeholders alike, read with the observer's own parser. This needs no
   list maintained by anyone, so it catches a rival nobody has registered.
2. **Every form in the labels file**, if one is given: the JSON held in Secret
   Manager as ``MEMBER_PSEUDONYM_LABELS``. That covers what is not on a sheet -
   Telegram handles, bare first names, member numbers seen in logs.

A sheet name ``Last, First`` is also checked as ``First Last``. Matching is
case-insensitive and on word boundaries.

Output never prints a match. It prints the file, the line, and a mask (first
letter and length), so this script's own output is safe to paste anywhere.

Usage::

    gcloud secrets versions access latest --secret=MEMBER_PSEUDONYM_LABELS > /tmp/labels.json
    python scripts/check_report_names.py operations/race-reports/2026-10-02.md \\
        --artifacts ./artifacts --labels /tmp/labels.json

Exit status: 0 clean, 1 a forbidden form was found, 2 nothing to check against.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

_OBSERVATIONS = Path(__file__).resolve().parent / "observer_observations.py"
_spec = importlib.util.spec_from_file_location("observer_observations", _OBSERVATIONS)
assert _spec is not None and _spec.loader is not None
observer_observations = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = observer_observations
_spec.loader.exec_module(observer_observations)

# Placeholders the club renders in name position that identify nobody.
_NOT_A_PERSON = {"tbd", "guest", "account, guest", "guest account"}


def _variants(name: str) -> set[str]:
    """``Last, First`` and ``First Last`` for a sheet name; the name alone otherwise."""
    name = " ".join(name.strip("() ").split())
    if not name or name.lower() in _NOT_A_PERSON:
        return set()
    forms = {name}
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        if last and first:
            forms.add(f"{first} {last}")
    return forms


def sheet_names(artifacts: Path) -> set[str]:
    """Every holder and TBD name on every ``*.html`` sheet under ``artifacts``."""
    names: set[str] = set()
    for page in artifacts.rglob("*.html"):
        try:
            slots = observer_observations.parse_sheet(page.read_bytes())
        except Exception:  # a page that is not a tee sheet has nothing to add
            continue
        for slot in slots:
            for name in (*slot.holders, *slot.tbd):
                names |= _variants(name)
    return names


def label_forms(labels: dict) -> set[str]:
    """Every form listed under ``people`` in the labels JSON."""
    forms: set[str] = set()
    for person in labels.get("people", []):
        for form in person.get("forms", []):
            forms |= _variants(form) if "," in form else {form.strip()}
    return {form for form in forms if form}


def _pattern(form: str) -> re.Pattern[str]:
    # \b fails before "@", so a handle anchors on "not a word character" instead.
    return re.compile(rf"(?<![\w@]){re.escape(form)}(?!\w)", re.IGNORECASE)


def _mask(text: str) -> str:
    return f"{text[0]}~{len(text)}"


def find(text: str, forms: set[str]) -> list[tuple[int, str]]:
    """``(line number, mask)`` for every occurrence of every form."""
    hits: list[tuple[int, str]] = []
    patterns = [_pattern(form) for form in sorted(forms, key=len, reverse=True)]
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern in patterns:
            for match in pattern.finditer(line):
                hits.append((number, _mask(match.group(0))))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path, help="report(s) to check")
    parser.add_argument("--artifacts", type=Path, help="downloaded artifacts directory")
    parser.add_argument("--labels", type=Path, help="MEMBER_PSEUDONYM_LABELS JSON")
    args = parser.parse_args(argv)

    forms: set[str] = set()
    if args.artifacts and args.artifacts.is_dir():
        found = sheet_names(args.artifacts)
        print(f"{len(found)} name forms from tee sheets under {args.artifacts}")
        forms |= found
    if args.labels:
        try:
            found = label_forms(json.loads(args.labels.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"labels file unreadable ({exc.__class__.__name__}); continuing without it")
        else:
            print(f"{len(found)} name forms from {args.labels}")
            forms |= found
    if not forms:
        print("nothing to check against: no tee sheets and no labels file")
        return 2

    failed = False
    for path in args.files:
        hits = find(path.read_text(encoding="utf-8"), forms)
        for number, mask in hits:
            print(f"{path}:{number}: names a person ({mask})")
        failed |= bool(hits)
    if not failed:
        print("clean: no forbidden name form found")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
