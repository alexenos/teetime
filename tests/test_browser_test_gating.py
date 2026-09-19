"""Tests for the CI gate on the browser integration suite.

``tests/test_async_chain_integration.py`` skips its seven tests when Chrome
cannot start. That is right on a developer machine and wrong in CI, where a
runner image that drops Chrome would shrink the suite to nothing and still
report success (#159). ``_browser_required`` is the switch between the two, so
it is checked here rather than left to the environment that happens to be
running.
"""

import pytest

from tests.test_async_chain_integration import _browser_required


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_ci_makes_the_browser_required(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.delenv("ALLOW_BROWSER_TEST_SKIP", raising=False)
    monkeypatch.setenv("CI", value)
    assert _browser_required() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_without_ci_a_missing_browser_still_skips(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.delenv("ALLOW_BROWSER_TEST_SKIP", raising=False)
    monkeypatch.setenv("CI", value)
    assert _browser_required() is False


def test_unset_ci_is_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_BROWSER_TEST_SKIP", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert _browser_required() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_escape_hatch_wins_over_ci(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A CI-flagged environment with no browser can opt back into skipping.

    The remote dev container is the case: it carries Playwright's Chromium but
    no ``google-chrome`` on ``PATH``, so Selenium cannot start one.
    """
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("ALLOW_BROWSER_TEST_SKIP", value)
    assert _browser_required() is False


def test_escape_hatch_off_does_not_disarm_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("ALLOW_BROWSER_TEST_SKIP", "0")
    assert _browser_required() is True
