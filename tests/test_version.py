"""The application version lives in `app.__version__` alone.

`pyproject.toml` also has a `version`, which Poetry requires. It is frozen
rather than bumped: the Dockerfile copies `pyproject.toml` before
`poetry install`, so a change to it on every deploy would rebuild the cached
dependency layer (#424). operations/versioning.md says when the version changes.
"""

import re
import tomllib
from pathlib import Path

from app import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
FROZEN_PYPROJECT_VERSION = "1.0.0"


def test_version_is_major_minor_patch() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_pyproject_version_is_frozen() -> None:
    with PYPROJECT.open("rb") as f:
        declared = tomllib.load(f)["tool"]["poetry"]["version"]
    assert (
        declared == FROZEN_PYPROJECT_VERSION
    ), "Bump app/__init__.py, not pyproject.toml: see operations/versioning.md"
