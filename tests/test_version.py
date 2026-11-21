"""The application version has one value, recorded in two places.

`pyproject.toml` carries it for packaging; `app.__version__` carries it at
runtime, because the image installs with `--no-root` and so has no package
metadata to read. operations/versioning.md says when and how it changes.
"""

import re
import tomllib
from pathlib import Path

from app import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_app_version_matches_pyproject() -> None:
    with PYPROJECT.open("rb") as f:
        declared = tomllib.load(f)["tool"]["poetry"]["version"]
    assert __version__ == declared


def test_version_is_major_minor_patch() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__
