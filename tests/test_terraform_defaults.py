"""The race's timing settings have one default, in two places, that must agree.

Cloud Build applies terraform with four ``-var`` flags and nothing else, so every
other variable deploys at its default in ``terraform/variables.tf`` - and that is
the value the racer actually runs, whatever ``app/config.py`` says. #173 moved
the aim margin to 0 in ``app/config.py`` on 2026-09-04 and left terraform at 30,
so every race from then until 2026-09-25 aimed at +1030 while the code, the
tests and the race-report skill all said +1000. Nothing noticed for three weeks.

This pins the pairs that decide when the burst fires, and checks that each is
actually wired into the environment the booking path reads.
"""

import re
from pathlib import Path

import pytest

from app.config import Settings

_TERRAFORM = Path(__file__).resolve().parent.parent / "terraform"

# Terraform variable -> (Settings attribute, environment variable in booking_env).
_TIMING_SETTINGS = {
    "walden_window_opens_offset_ms": (
        "walden_window_opens_offset_ms",
        "WALDEN_WINDOW_OPENS_OFFSET_MS",
    ),
    "walden_reserve_aim_margin_ms": (
        "walden_reserve_aim_margin_ms",
        "WALDEN_RESERVE_AIM_MARGIN_MS",
    ),
    "walden_burst_start_before_aim_ms": (
        "walden_burst_start_before_aim_ms",
        "WALDEN_BURST_START_BEFORE_AIM_MS",
    ),
    "walden_burst_end_after_aim_ms": (
        "walden_burst_end_after_aim_ms",
        "WALDEN_BURST_END_AFTER_AIM_MS",
    ),
    "walden_burst_dense_half_width_ms": (
        "walden_burst_dense_half_width_ms",
        "WALDEN_BURST_DENSE_HALF_WIDTH_MS",
    ),
    "walden_burst_dense_spacing_ms": (
        "walden_burst_dense_spacing_ms",
        "WALDEN_BURST_DENSE_SPACING_MS",
    ),
    "walden_burst_sparse_spacing_ms": (
        "walden_burst_sparse_spacing_ms",
        "WALDEN_BURST_SPARSE_SPACING_MS",
    ),
    "walden_burst_prewarm_connections": (
        "walden_burst_prewarm_connections",
        "WALDEN_BURST_PREWARM_CONNECTIONS",
    ),
}


def terraform_defaults(path: Path) -> dict[str, str]:
    """Each ``variable`` block's ``default``, as the literal text after ``=``.

    Heredoc descriptions are skipped rather than scanned, because their prose is
    free to mention the word "default" at the start of a line.
    """
    defaults: dict[str, str] = {}
    current: str | None = None
    heredoc_end: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        opened = re.match(r'^variable "([^"]+)" \{', line)
        if opened:
            current = opened.group(1)
            continue
        if current is None:
            continue
        heredoc = re.search(r"<<-?(\w+)\s*$", line)
        if heredoc:
            heredoc_end = heredoc.group(1)
            continue
        default = re.match(r"^\s*default\s*=\s*(.+?)\s*$", line)
        if default and current not in defaults:
            defaults[current] = default.group(1)
        if line.startswith("}"):
            current = None
    return defaults


def _as_python(literal: str) -> object:
    """A terraform default literal as the value Settings would parse from it."""
    if literal in ("true", "false"):
        return literal == "true"
    if literal.startswith('"') and literal.endswith('"'):
        return literal[1:-1]
    return int(literal)


@pytest.fixture(scope="module")
def defaults() -> dict[str, str]:
    """terraform/variables.tf's defaults, parsed once."""
    return terraform_defaults(_TERRAFORM / "variables.tf")


@pytest.mark.parametrize("variable", sorted(_TIMING_SETTINGS))
def test_terraform_and_the_app_agree(
    variable: str, defaults: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed value and the code's value are the same value."""
    attribute, env_name = _TIMING_SETTINGS[variable]
    monkeypatch.delenv(env_name, raising=False)

    assert variable in defaults, f"{variable} has no default in terraform/variables.tf"
    assert _as_python(defaults[variable]) == getattr(Settings(_env_file=None), attribute)


@pytest.mark.parametrize("variable", sorted(_TIMING_SETTINGS))
def test_every_timing_setting_reaches_the_racer(variable: str) -> None:
    """A default that is never wired into the environment is a default nobody runs.

    booking_env in terraform/main.tf is the map both the service and the racer
    job (terraform/racer.tf) read, so a setting present there reaches both.
    """
    _attribute, env_name = _TIMING_SETTINGS[variable]
    main_tf = (_TERRAFORM / "main.tf").read_text(encoding="utf-8")

    assert re.search(
        rf"^\s*{env_name}\s*=\s*.*var\.{variable}\b", main_tf, re.MULTILINE
    ), f"{env_name} is not set from var.{variable} in terraform/main.tf's booking_env"


def test_the_deployed_aim_is_gate_plus_five(defaults: dict[str, str]) -> None:
    """The number the whole plan hangs off, stated as the deployment sees it."""
    gate = int(defaults["walden_window_opens_offset_ms"])
    margin = int(defaults["walden_reserve_aim_margin_ms"])

    assert gate + margin == 1005


def test_the_racer_has_two_vcpus(defaults: dict[str, str]) -> None:
    """Two since 2026-09-25; run.json's burstCpu says whether it is earning its keep."""
    assert _as_python(defaults["racer_cpu"]) == "2"


def test_the_parser_skips_heredoc_prose(tmp_path: Path) -> None:
    """A line of description starting with "default" is not the variable's default."""
    tf = tmp_path / "variables.tf"
    tf.write_text(
        'variable "x" {\n'
        "  description = <<-EOT\n"
        "    default = 99 would be wrong to read\n"
        "  EOT\n"
        "  type    = number\n"
        "  default = 5\n"
        "}\n",
        encoding="utf-8",
    )

    assert terraform_defaults(tf) == {"x": "5"}
