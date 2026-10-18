"""``operations/features.md`` must stay in step with ``app/``.

The tech-debt Routine judges legacy code against that manifest (#287), and files
an issue when a manifest path is gone or when code under ``app/`` belongs to no
feature. Those issues arrive on a Saturday, up to a week after the PR that caused
them. This fails the PR instead.

Three checks, all over the feature tables only. The Removed table is exempt from
the first: its paths are where the code *was*.

* every ``app/`` or ``terraform/`` path a row names exists
* every ``app/**/*.py`` file is named by some row, or sits under a directory a row
  names
* every row says, in the "Why a user cares" column, what it means to a member
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _ROOT / "operations" / "features.md"

# A Paths cell names files in backticks next to prose: "`/webhooks/telegram` in
# `app/api/webhooks.py`". Only tokens rooted at these directories are paths.
_PATH_TOKEN = re.compile(r"`((?:app|terraform)/[^`\s]*)`")


def _feature_rows() -> list[dict[str, str]]:
    """One dict per row of the feature tables, keyed by column header.

    The Removed table is skipped. A feature table is recognised by its header row,
    whose first cell is ``Feature`` and which names a ``Paths`` column.
    """
    rows: list[dict[str, str]] = []
    in_removed = False
    header: list[str] = []
    for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_removed = line.strip() == "## Removed"
            header = []
            continue
        if in_removed or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == "Feature" and "Paths" in cells:
            header = cells
        elif header and len(cells) == len(header) and not set(cells[0]) <= {"-"}:
            rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _declared_paths() -> set[str]:
    """Every path a feature row names, with a directory always ending in a slash."""
    return {
        path + "/" if (_ROOT / path).is_dir() and not path.endswith("/") else path
        for row in _feature_rows()
        for path in _PATH_TOKEN.findall(row["Paths"])
    }


def test_manifest_has_feature_rows() -> None:
    """A parser that finds nothing would make the checks below pass vacuously."""
    rows = _feature_rows()
    assert len(rows) >= 10, f"parsed only {len(rows)} feature rows from {_MANIFEST}"
    no_paths = [r["Feature"] for r in rows if not _PATH_TOKEN.findall(r["Paths"])]
    assert not no_paths, f"rows that name no app/ or terraform/ path: {no_paths}"


def test_every_feature_says_why_a_user_cares() -> None:
    """The column answers "as a user, why do I care?"; an empty cell is a gap."""
    empty = [r["Feature"] for r in _feature_rows() if not r["Why a user cares"]]
    assert not empty, f"features with no 'Why a user cares': {empty}"


def test_every_manifest_path_exists() -> None:
    missing = sorted(p for p in _declared_paths() if not (_ROOT / p).exists())
    assert not missing, (
        f"operations/features.md names paths that do not exist: {missing}. "
        "If the feature was removed on purpose, move its row to the Removed table."
    )


def test_every_app_module_belongs_to_a_feature() -> None:
    declared = _declared_paths()
    directories = {p for p in declared if p.endswith("/")}
    unclassified = []
    for path in sorted((_ROOT / "app").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if path.name in ("__init__.py", "__main__.py"):
            continue
        if rel in declared or any(rel.startswith(d) for d in directories):
            continue
        unclassified.append(rel)
    assert not unclassified, (
        f"{unclassified} belong to no feature in operations/features.md. "
        "Add a row, or add the file to an existing row's Paths."
    )
