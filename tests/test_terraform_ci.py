"""CI checks terraform before the pipeline that applies it does (#229, item 1).

``cloudbuild.yaml`` runs ``terraform apply -auto-approve`` on every merge to
``main``. Until the ``terraform`` job in ``.github/workflows/test.yml`` existed,
nothing parsed ``terraform/`` before that apply: a syntax error or a bad type
reached ``main`` and was found by the step that reconciles the infrastructure
the 06:28 CT race runs on.

These tests pin that the job exists and runs both checks, and that the
Terraform version it checks with is the one Cloud Build applies with. A
validate pass under a different minor version says less about the apply.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO / ".github" / "workflows" / "test.yml"
_CLOUDBUILD = _REPO / "cloudbuild.yaml"


@pytest.fixture(scope="module")
def terraform_job() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "terraform" in jobs, "the Tests workflow has no terraform job"
    job: dict[str, Any] = jobs["terraform"]
    return job


def _commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def _terraform_image_versions(cloudbuild_text: str) -> set[str]:
    """The tag of every ``hashicorp/terraform`` step image in a Cloud Build config.

    Every step is inspected, and a tag that is not a version (``latest``, or no
    tag at all) fails rather than being skipped: skipping it would let one step
    drift away from the version CI checks with while the comparison still passed.
    """
    config = yaml.safe_load(cloudbuild_text)
    versions: set[str] = set()
    for step in config.get("steps", []):
        image = str(step.get("name", ""))
        if not image.startswith("hashicorp/terraform"):
            continue
        _, _, tag = image.partition(":")
        assert re.fullmatch(
            r"\d+\.\d+(\.\d+)?", tag
        ), f"cloudbuild.yaml step image {image!r} does not pin a terraform version"
        versions.add(tag)
    return versions


def _cloudbuild_terraform_versions() -> set[str]:
    return _terraform_image_versions(_CLOUDBUILD.read_text(encoding="utf-8"))


def _major_minor(version: str) -> str:
    match = re.search(r"(\d+)\.(\d+)", version)
    assert match, f"no major.minor in {version!r}"
    return f"{match.group(1)}.{match.group(2)}"


def test_the_job_runs_in_the_terraform_directory(terraform_job: dict[str, Any]) -> None:
    assert terraform_job["defaults"]["run"]["working-directory"] == "terraform"


def test_the_job_checks_formatting(terraform_job: dict[str, Any]) -> None:
    assert any(
        cmd.startswith("terraform fmt") and "-check" in cmd and "-recursive" in cmd
        for cmd in _commands(terraform_job)
    ), "the terraform job no longer runs `terraform fmt -check -recursive`"


def test_the_job_validates_without_a_backend(terraform_job: dict[str, Any]) -> None:
    commands = _commands(terraform_job)
    init = [i for i, cmd in enumerate(commands) if cmd.startswith("terraform init")]
    validate = [i for i, cmd in enumerate(commands) if cmd.startswith("terraform validate")]
    assert init, "the terraform job no longer runs `terraform init`"
    assert validate, "the terraform job no longer runs `terraform validate`"
    assert init[0] < validate[0], "validate needs providers, so init must run first"
    # The job has no credentials. Initializing the GCS backend would fail, and
    # must not be made to work by giving CI access to the state.
    assert "-backend=false" in commands[init[0]]


def test_ci_checks_with_the_version_cloud_build_applies_with(
    terraform_job: dict[str, Any],
) -> None:
    applied = _cloudbuild_terraform_versions()
    assert len(applied) == 1, f"cloudbuild.yaml uses more than one terraform image: {applied}"
    setup = [
        step
        for step in terraform_job["steps"]
        if str(step.get("uses", "")).startswith("hashicorp/setup-terraform@")
    ]
    assert len(setup) == 1, "the terraform job does not install terraform with setup-terraform"
    checked = str(setup[0]["with"]["terraform_version"])
    assert _major_minor(checked) == _major_minor(applied.pop())


def test_the_version_comparison_reads_major_and_minor() -> None:
    assert _major_minor("~1.6.0") == "1.6"
    assert _major_minor("1.6") == "1.6"
    assert _major_minor("1.10.3") == "1.10"
    with pytest.raises(AssertionError):
        _major_minor("latest")


def test_every_cloud_build_terraform_step_is_read() -> None:
    pinned = "steps:\n  - name: 'hashicorp/terraform:1.6'\n  - name: 'hashicorp/terraform:1.6'\n"
    assert _terraform_image_versions(pinned) == {"1.6"}
    mixed = "steps:\n  - name: 'hashicorp/terraform:1.6'\n  - name: 'hashicorp/terraform:1.7'\n"
    assert _terraform_image_versions(mixed) == {"1.6", "1.7"}


@pytest.mark.parametrize("unpinned", ["hashicorp/terraform:latest", "hashicorp/terraform"])
def test_an_unpinned_terraform_step_fails_rather_than_being_skipped(unpinned: str) -> None:
    config = f"steps:\n  - name: 'hashicorp/terraform:1.6'\n  - name: '{unpinned}'\n"
    with pytest.raises(AssertionError, match="does not pin a terraform version"):
        _terraform_image_versions(config)
