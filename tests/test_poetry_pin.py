"""The Dockerfile and CI install the same Poetry (#153).

Poetry 1.7.1 and 2.x disagreed on whether `poetry install` on this tree was an
error (#152), and CI passed only because of the pin. A Poetry in CI that
differs from the one the image is built with lets CI pass a tree the deploy
build rejects, or the reverse. These tests pin every install to one exact
version, and to a 2.x one.
"""

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO / "Dockerfile"
_WORKFLOW = _REPO / ".github" / "workflows" / "test.yml"


def _dockerfile_versions() -> set[str]:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    installs = re.findall(r"pip install[^\n]*\bpoetry\b[^\n]*", text)
    assert installs, "the Dockerfile does not pip install poetry"
    versions: set[str] = set()
    for line in installs:
        match = re.search(r"\bpoetry==(\d+\.\d+\.\d+)\b", line)
        assert match, f"Dockerfile installs poetry without an exact pin: {line!r}"
        versions.add(match.group(1))
    return versions


def _workflow_versions() -> set[str]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    versions: set[str] = set()
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if not str(step.get("uses", "")).startswith("snok/install-poetry"):
                continue
            version = str(step.get("with", {}).get("version", ""))
            assert re.fullmatch(
                r"\d+\.\d+\.\d+", version
            ), f"job {name!r} installs poetry without an exact version: {version!r}"
            versions.add(version)
    assert versions, "the Tests workflow installs no poetry"
    return versions


def test_dockerfile_and_ci_pin_the_same_poetry() -> None:
    docker = _dockerfile_versions()
    ci = _workflow_versions()
    assert len(docker | ci) == 1, f"Dockerfile pins {docker}, CI pins {ci}"


def test_poetry_pin_is_2x() -> None:
    (version,) = _dockerfile_versions()
    assert version.split(".")[0] == "2", f"Poetry pin {version} is not 2.x"
