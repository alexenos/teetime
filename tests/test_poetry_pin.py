"""The Dockerfile and every CI workflow install the same Poetry (#153).

Poetry 1.7.1 and 2.x disagreed on whether `poetry install` on this tree was an
error (#152), and CI passed only because of the pin. A Poetry in CI that
differs from the one the image is built with lets CI pass a tree the deploy
build rejects, or the reverse. These tests pin the Dockerfile and each
`snok/install-poetry` step under `.github/workflows/` to one exact 2.x version,
and check that CI's venv cache is keyed on it.

Not covered: dev containers, which run whatever Poetry their image ships
(`scripts/setup_remote_env.sh` does not install one).
"""

import re
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO / "Dockerfile"
_WORKFLOWS = sorted((_REPO / ".github" / "workflows").glob("*.y*ml"))
_ENV_REF = re.compile(r"\$\{\{\s*env\.(\w+)\s*\}\}")


def _dockerfile_versions() -> set[str]:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    # The package itself, not a plugin such as poetry-plugin-export.
    installs = re.findall(r"pip install[^\n]*(?<![\w-])poetry(?![\w-])[^\n]*", text)
    assert installs, "the Dockerfile does not pip install poetry"
    versions: set[str] = set()
    for line in installs:
        match = re.search(r"(?<![\w-])poetry==(\d+\.\d+\.\d+)(?![\w.-])", line)
        assert match, f"Dockerfile installs poetry without an exact pin: {line!r}"
        versions.add(match.group(1))
    return versions


def _resolve(value: str, env: dict[str, Any]) -> str:
    """Substitute workflow-level ``${{ env.X }}`` references."""
    return _ENV_REF.sub(lambda m: str(env.get(m.group(1), m.group(0))), value)


def _workflow_poetry_steps() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(label, install step, job) for every snok/install-poetry step."""
    found = []
    for path in _WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        env = workflow.get("env", {}) or {}
        for name, job in workflow.get("jobs", {}).items():
            job_env = {**env, **(job.get("env", {}) or {})}
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("snok/install-poetry"):
                    found.append((f"{path.name}:{name}", step, {**job, "_env": job_env}))
    return found


def _workflow_versions() -> set[str]:
    steps = _workflow_poetry_steps()
    assert steps, "no workflow installs poetry"
    versions: set[str] = set()
    for label, step, job in steps:
        version = _resolve(str(step.get("with", {}).get("version", "")), job["_env"])
        assert re.fullmatch(
            r"\d+\.\d+\.\d+", version
        ), f"{label} installs poetry without an exact version: {version!r}"
        versions.add(version)
    return versions


def test_dockerfile_and_ci_pin_the_same_poetry() -> None:
    docker = _dockerfile_versions()
    ci = _workflow_versions()
    assert len(docker | ci) == 1, f"Dockerfile pins {docker}, CI pins {ci}"


def test_poetry_pin_is_2x() -> None:
    (version,) = _dockerfile_versions()
    assert version.split(".")[0] == "2", f"Poetry pin {version} is not 2.x"


def test_ci_venv_cache_is_keyed_on_the_poetry_version() -> None:
    """A pin change must miss the cache, so CI installs cold under the new Poetry."""
    for label, step, job in _workflow_poetry_steps():
        version = _resolve(str(step.get("with", {}).get("version", "")), job["_env"])
        for cache in job.get("steps", []):
            if not str(cache.get("uses", "")).startswith("actions/cache"):
                continue
            key = _resolve(str(cache.get("with", {}).get("key", "")), job["_env"])
            assert version in key, f"{label}: venv cache key {key!r} omits Poetry {version}"
