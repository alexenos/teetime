"""Settings tolerates environment it no longer reads.

A setting removed from ``Settings`` can still be set where the service runs:
an old ``.env``, or an environment variable on a deployed revision that this
repository cannot inspect. Removing ``max_tee_times_per_day`` (#313) is safe
only because a leftover value is ignored rather than refused at startup.
"""

from pathlib import Path

import pytest

from app.config import Settings


def test_a_removed_setting_left_in_the_environment_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holds by construction: environment variables are read per field only."""
    monkeypatch.setenv("MAX_TEE_TIMES_PER_DAY", "2")

    config = Settings(_env_file=None)

    assert not hasattr(config, "max_tee_times_per_day")


def test_a_removed_setting_left_in_the_env_file_is_ignored(tmp_path: Path) -> None:
    """Holds only while ``Settings.Config.extra`` is "ignore"; "forbid" fails here."""
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_TEE_TIMES_PER_DAY=2\n", encoding="utf-8")

    config = Settings(_env_file=env_file)

    assert not hasattr(config, "max_tee_times_per_day")
