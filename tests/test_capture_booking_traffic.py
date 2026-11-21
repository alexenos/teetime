"""Tests for scripts/capture_booking_traffic.py: missing POST bodies are counted (#474)."""

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from selenium.common.exceptions import WebDriverException

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "capture_booking_traffic.py"
_spec = importlib.util.spec_from_file_location("capture_booking_traffic", _PATH)
assert _spec is not None and _spec.loader is not None
cbt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cbt
# The script calls load_dotenv() at import. Unpatched, a developer's .env would
# land in os.environ for every test collected after this one.
with patch("dotenv.load_dotenv") as _load_dotenv:
    _spec.loader.exec_module(cbt)
_IMPORT_LOADED_DOTENV = _load_dotenv.called


def _sent(request_id: str, method: str = "POST", post_data: str | None = None) -> dict[str, Any]:
    """A Network.requestWillBeSent event, as drain_performance_log stores it."""
    request: dict[str, Any] = {"url": f"https://example.test/{request_id}", "method": method}
    if post_data is not None:
        request["postData"] = post_data
    return {
        "method": "Network.requestWillBeSent",
        "params": {"requestId": request_id, "request": request},
    }


class FakeDriver:
    """Answers Network.getRequestPostData from a table; anything else raises."""

    def __init__(self, bodies: dict[str, str], errors: dict[str, Exception]) -> None:
        self.bodies = bodies
        self.errors = errors
        self.asked: list[str] = []

    def execute_cdp_cmd(self, cmd: str, params: dict[str, Any]) -> dict[str, Any]:
        assert cmd == "Network.getRequestPostData"
        request_id = params["requestId"]
        self.asked.append(request_id)
        if request_id in self.errors:
            raise self.errors[request_id]
        return {"postData": self.bodies[request_id]}


def test_fetched_and_failed_bodies_are_counted_and_failures_marked() -> None:
    events = [
        _sent("ok"),
        _sent("gone"),
        _sent("dead"),
        _sent("has-body", post_data="already=here"),
        _sent("get", method="GET"),
        {"method": "Network.responseReceived", "params": {"requestId": "ok"}},
    ]
    driver = FakeDriver(
        bodies={"ok": "a=1"},
        errors={
            "gone": WebDriverException("No resource with given identifier found"),
            "dead": ConnectionError("CDP session closed"),
        },
    )

    counts = cbt.fetch_post_bodies(driver, events)

    assert counts == {"attempted": 3, "fetched": 1, "failed": 2}
    assert driver.asked == ["ok", "gone", "dead"]
    requests = {ev["params"]["requestId"]: ev["params"]["request"] for ev in events[:5]}
    assert requests["ok"]["postData"] == "a=1"
    assert "postDataError" not in requests["ok"]
    assert requests["gone"]["postDataError"] == "WebDriverException"
    assert requests["dead"]["postDataError"] == "ConnectionError"
    assert "postData" not in requests["gone"]
    assert requests["has-body"]["postData"] == "already=here"


def test_a_failure_does_not_stop_later_fetches() -> None:
    events = [_sent("first"), _sent("second")]
    driver = FakeDriver(bodies={"second": "b=2"}, errors={"first": RuntimeError("boom")})

    counts = cbt.fetch_post_bodies(driver, events)

    assert counts == {"attempted": 2, "fetched": 1, "failed": 1}
    assert events[1]["params"]["request"]["postData"] == "b=2"


def test_the_error_type_is_kept_not_its_message() -> None:
    """The message could quote request content; only the type goes into the capture."""
    events = [_sent("x")]
    driver = FakeDriver(bodies={}, errors={"x": RuntimeError("secret=hunter2")})

    cbt.fetch_post_bodies(driver, events)

    assert events[0]["params"]["request"]["postDataError"] == "RuntimeError"
    assert "hunter2" not in repr(events)


def test_importing_the_script_here_does_not_load_dotenv() -> None:
    """The patch above intercepted the script's import-time load_dotenv()."""
    assert _IMPORT_LOADED_DOTENV


@pytest.mark.parametrize("reply", [{}, {"postData": ""}, {"postData": None}])
def test_an_empty_reply_counts_as_a_failure_not_a_fetch(reply: dict[str, Any]) -> None:
    """A reply with no body recovered nothing, so it must not read as fetched."""

    class EmptyDriver:
        def execute_cdp_cmd(self, cmd: str, params: dict[str, Any]) -> dict[str, Any]:
            return reply

    events = [_sent("x")]

    counts = cbt.fetch_post_bodies(EmptyDriver(), events)

    assert counts == {"attempted": 1, "fetched": 0, "failed": 1}
    assert events[0]["params"]["request"]["postDataError"] == "empty"
    assert "postData" not in events[0]["params"]["request"]


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (
            {"attempted": 0, "fetched": 0, "failed": 0},
            "POST bodies: none were missing from the log.",
        ),
        (
            {"attempted": 2, "fetched": 2, "failed": 0},
            "POST bodies: fetched 2 of 2 missing from the log.",
        ),
        (
            {"attempted": 3, "fetched": 1, "failed": 2},
            "POST bodies: fetched 1 of 3 missing from the log; 2 could not be fetched "
            "and are absent from this capture (see postDataError on those requests).",
        ),
    ],
)
def test_the_operator_is_told_how_many_bodies_are_missing(
    counts: dict[str, int], expected: str
) -> None:
    assert cbt.describe_post_body_fetch(counts) == expected
