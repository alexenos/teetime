"""The cookie-consent banner must not stop a login (2026-11-12)."""

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    WebDriverException,
)

from app.observer import sheet
from app.providers import walden_consent
from app.providers.walden_consent import (
    HIDE_CONSENT_BANNER_SCRIPT,
    click_past_overlay,
    hide_consent_banner,
)
from app.providers.walden_provider import WaldenGolfProvider

INTERCEPTED = ElementClickInterceptedException(
    "element click intercepted: Element <button> is not clickable at point (609, 666). "
    'Other element would receive the click: <div id="ccBanner">\n  (Session info: chrome=156)'
)

HOME_URL = "https://www.waldengolf.com/web/pages/home"
LOGIN_URL = "https://www.waldengolf.com/web/pages/login"


def _driver(url: str = HOME_URL) -> MagicMock:
    driver = MagicMock()
    driver.current_url = url
    return driver


class TestClickPastOverlay:
    def test_a_clean_click_does_not_touch_js(self) -> None:
        driver, button = _driver(), MagicMock()

        click_past_overlay(driver, button, log_prefix="T")

        button.click.assert_called_once_with()
        driver.execute_script.assert_not_called()

    def test_an_intercepted_click_is_dispatched_via_js_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        driver, button = _driver(), MagicMock()
        button.click.side_effect = INTERCEPTED

        with caplog.at_level(logging.WARNING, logger=walden_consent.__name__):
            click_past_overlay(driver, button, log_prefix="LOGIN")

        driver.execute_script.assert_called_once_with("arguments[0].click();", button)
        assert "LOGIN: click intercepted" in caplog.text
        assert "Session info" not in caplog.text

    def test_any_other_click_failure_still_raises(self) -> None:
        driver, button = _driver(), MagicMock()
        button.click.side_effect = ElementNotInteractableException("hidden")

        with pytest.raises(ElementNotInteractableException):
            click_past_overlay(driver, button, log_prefix="T")
        driver.execute_script.assert_not_called()


class TestHideConsentBanner:
    def test_installs_the_stylesheet_on_every_new_document(self) -> None:
        driver = _driver()

        hide_consent_banner(driver)

        driver.execute_cdp_cmd.assert_called_once_with(
            "Page.addScriptToEvaluateOnNewDocument", {"source": HIDE_CONSENT_BANNER_SCRIPT}
        )
        assert "#ccBanner" in HIDE_CONSENT_BANNER_SCRIPT
        assert "display: none !important" in HIDE_CONSENT_BANNER_SCRIPT

    @pytest.mark.parametrize(
        "create",
        [
            lambda: WaldenGolfProvider("member", "password")._create_driver(),
            sheet.create_driver,
        ],
        ids=["racer", "observer"],
    )
    def test_both_drivers_install_it(self, create: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHROMEDRIVER_PATH", sys.executable)
        with patch("selenium.webdriver.Chrome") as chrome:
            create()  # type: ignore[operator]

        sources = [c.args[1]["source"] for c in chrome.return_value.execute_cdp_cmd.call_args_list]
        assert HIDE_CONSENT_BANNER_SCRIPT in sources


class TestRacerLogin:
    @pytest.fixture
    def provider(self) -> WaldenGolfProvider:
        return WaldenGolfProvider("member", "password")

    @pytest.fixture
    def racer_task(self, monkeypatch: pytest.MonkeyPatch) -> str:
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", "teetime-racer-abc12")
        monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", "3")
        return "teetime-racer-abc12_3"

    @staticmethod
    def _captured_context(capture: MagicMock, driver: MagicMock) -> str:
        capture.assert_called_once()
        captured_driver, context = capture.call_args.args
        assert captured_driver is driver
        return str(context)

    def test_logs_in_past_an_intercepted_submit(self, provider: WaldenGolfProvider) -> None:
        driver = _driver()
        submit = MagicMock()
        submit.click.side_effect = INTERCEPTED
        driver.find_element.return_value = submit

        with patch("app.providers.walden_provider.WebDriverWait"):
            assert provider._perform_login(driver) is True

        driver.execute_script.assert_called_once_with("arguments[0].click();", submit)

    def test_a_webdriver_failure_captures_the_page(
        self, provider: WaldenGolfProvider, racer_task: str
    ) -> None:
        driver = _driver()
        driver.find_element.side_effect = WebDriverException("gone")

        with (
            patch("app.providers.walden_provider.WebDriverWait"),
            patch.object(provider, "_capture_diagnostic_info") as capture,
        ):
            assert provider._perform_login(driver) is False

        context = self._captured_context(capture, driver)
        # Every racer task logs in at the same scheduled second, so the prefix
        # must carry the task or a sibling's upload is refused (2026-09-15).
        assert context.startswith("login_failed_")
        assert context.endswith(f"_{racer_task}")

    def test_a_rejected_password_captures_nothing(self, provider: WaldenGolfProvider) -> None:
        driver = _driver(LOGIN_URL)

        with (
            patch("app.providers.walden_provider.WebDriverWait"),
            patch.object(provider, "_walden_rejected_login", return_value=True),
            patch.object(provider, "_capture_diagnostic_info") as capture,
        ):
            assert provider._perform_login(driver) is False

        capture.assert_not_called()

    def test_a_failure_that_is_not_a_rejection_captures_the_page(
        self, provider: WaldenGolfProvider, racer_task: str
    ) -> None:
        driver = _driver(LOGIN_URL)

        with (
            patch("app.providers.walden_provider.WebDriverWait"),
            patch.object(provider, "_walden_rejected_login", return_value=False),
            patch.object(provider, "_capture_diagnostic_info") as capture,
        ):
            assert provider._perform_login(driver) is False

        context = self._captured_context(capture, driver)
        # Every racer task logs in at the same scheduled second, so the prefix
        # must carry the task or a sibling's upload is refused (2026-09-15).
        assert context.startswith("login_failed_")
        assert context.endswith(f"_{racer_task}")


class TestObserverLogin:
    def test_logs_in_past_an_intercepted_submit(self) -> None:
        driver = _driver()
        submit = MagicMock()
        submit.click.side_effect = INTERCEPTED
        driver.find_element.return_value = submit

        with patch("app.observer.sheet.WebDriverWait"):
            assert sheet.log_in(driver, "member", "password") is True

        driver.execute_script.assert_called_once_with("arguments[0].click();", submit)
