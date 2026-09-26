"""When a burst member really leaves, on what connection, and what that says.

Until 2026-09-25 every burst member dialled a new TCP and TLS connection at its
own instant - httpx expires an idle connection after 5s and staging ends
~90s before the window - so each Reserve reached the wire ~55ms after the send
time the ledger recorded, and nothing in the ledger could show it. These tests
cover the instrumentation that now records it, the pre-warm that removes it,
and the measurements built on top: the gate bracket, the write-drift summary
and the CPU counters behind the second vCPU.

The transport tests run against a real HTTP server on localhost, because an
in-memory transport never opens a connection and emits no trace events - the
very thing under test.
"""

import concurrent.futures
import http.server
import json
import random
import socketserver
import threading
import time as time_module
from collections.abc import Iterator
from datetime import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.providers.walden_http import (
    AbConfig,
    DirectHttpTimeoutError,
    FormState,
    PrimeFacesSession,
    TransportTiming,
)
from app.providers.walden_http_booker import (
    RESERVE_ACCEPTED,
    RESERVE_REFUSED,
    DirectBookingResult,
    ReserveObservation,
    _log_gate_bracket,
    describe_burst_plan,
    gate_brackets,
    summarize_burst_writes,
)
from app.providers.walden_provider import WaldenGolfProvider
from app.utils import cpu_telemetry

_FORM = "f"
_PARTIAL = (
    "<?xml version='1.0' encoding='UTF-8'?><partial-response><changes>"
    f'<update id="{_FORM}"><![CDATA[<div>ok</div>]]></update>'
    '<update id="j:javax.faces.ViewState:0"><![CDATA[vs-2]]></update>'
    "</changes></partial-response>"
)
_CONFIG = AbConfig(source="reserve", form=_FORM, update=_FORM)


class _Club(http.server.BaseHTTPRequestHandler):
    """Answers HEAD with a dated 200 and POST with a partial-response, late on purpose."""

    protocol_version = "HTTP/1.1"
    post_delay_s = 0.05

    def do_HEAD(self) -> None:  # noqa: N802 - the name http.server dispatches on
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time_module.sleep(type(self).post_delay_s)
        body = _PARTIAL.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Quiet."""


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _LocalClub:
    """A running local server, and sessions pointed at it."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.sessions: list[PrimeFacesSession] = []

    def session(self) -> PrimeFacesSession:
        """A session with its own pool - the production client, not an injected one."""
        form = FormState(form_id=_FORM, action_url=f"{self.base}/reserve", fields=[])
        session = PrimeFacesSession(form, {}, base_url=f"{self.base}/group/pages/tee-time")
        self.sessions.append(session)
        return session


@pytest.fixture
def club() -> Iterator[_LocalClub]:
    """A club on localhost for the duration of one test."""
    server = _Server(("127.0.0.1", 0), _Club)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = _LocalClub(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield local
    finally:
        for session in local.sessions:
            session.close()
        server.shutdown()
        server.server_close()
        _Club.post_delay_s = 0.05


class TestTransportTiming:
    """send_detached reports when its bytes left and whether it had to dial."""

    def test_a_cold_request_dials_and_a_warm_one_does_not(self, club: _LocalClub) -> None:
        session = club.session()

        first = session.send_detached(_CONFIG)
        second = session.send_detached(_CONFIG)

        assert first.transport is not None and second.transport is not None
        assert first.transport.opened_connection is True
        assert first.transport.connect_ms is not None and first.transport.connect_ms >= 0
        assert first.sent_at_ms is not None and first.received_at_ms is not None
        assert first.transport.wrote_at_ms is not None
        assert first.sent_at_ms <= first.transport.wrote_at_ms <= first.received_at_ms
        assert second.transport.opened_connection is False
        assert second.transport.connect_ms is None
        assert first.transport.local_port == second.transport.local_port

    def test_prewarm_gives_every_member_its_own_warm_connection(self, club: _LocalClub) -> None:
        """The whole point: five members at once, five warm connections, no dialling."""
        session = club.session()

        report = session.prewarm(5)
        barrier = threading.Barrier(5)

        def member(_index: int) -> TransportTiming | None:
            barrier.wait(timeout=5)
            return session.send_detached(_CONFIG).transport

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            transports = list(pool.map(member, range(5)))

        assert report["failed"] == 0
        assert report["opened"] + report["reused"] == 5
        assert report["idleAfter"] >= 5
        assert all(t is not None and t.opened_connection is False for t in transports)
        assert len({t.local_port for t in transports if t is not None}) == 5

    def test_a_late_thread_cannot_ride_a_connection_another_just_released(
        self, club: _LocalClub
    ) -> None:
        """Every pre-warm request holds its connection until all of them have one.

        Found while writing these tests: released together but unheld, a thread
        scheduled a few ms late finds an earlier request's connection already
        back in the pool and rides it, leaving the pool one connection short per
        late thread - a member that dials at its own instant after all. Here each
        request starts up to 20ms late, far longer than one takes on localhost.
        """
        for _trial in range(5):
            session = club.session()
            unjittered = session._client.stream

            def jittered(*args: Any, **kwargs: Any) -> Any:
                time_module.sleep(random.uniform(0, 0.02))
                return unjittered(*args, **kwargs)

            session._client.stream = jittered  # type: ignore[method-assign]
            report = session.prewarm(5)

            assert report["failed"] == 0
            assert report["idleAfter"] == 5

    def test_a_timed_out_request_still_says_when_it_left(self, club: _LocalClub) -> None:
        """A burst member that never answered is still placed on the timeline."""
        _Club.post_delay_s = 0.5
        session = club.session()

        with pytest.raises(DirectHttpTimeoutError) as raised:
            session.send_detached(_CONFIG, timeout_s=0.1)

        transport = raised.value.transport
        assert transport is not None
        assert transport.wrote_at_ms is not None
        assert transport.opened_connection is True

    def test_an_in_memory_transport_reports_nothing_rather_than_guessing(self) -> None:
        """No trace events means "not measured", never "did not dial"."""
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=_PARTIAL))
        )
        form = FormState(form_id=_FORM, action_url="https://club.test/reserve", fields=[])
        session = PrimeFacesSession(form, {}, base_url="https://club.test/x", client=client)

        response = session.send_detached(_CONFIG)

        assert response.transport == TransportTiming()
        assert session.idle_connections() is None


def _observation(
    attempt: int,
    verdict: str,
    *,
    wrote: int | None,
    sent: int | None = None,
    planned: int | None = None,
    lead: int = 15,
    slot: time = time(8, 38),
    opened: bool | None = False,
    port: int | None = None,
) -> ReserveObservation:
    """A burst member's ledger row, built directly."""
    observation = ReserveObservation(
        attempt=attempt,
        slot_time=slot,
        source="s",
        view_state="v",
        verdict=verdict,
        reason="r",
        sent_ms_past_window=sent if sent is not None else wrote,
        burst_index=attempt - 1,
        planned_send_ms_past_window=planned,
        lead_ms=lead,
        wrote_ms_past_window=wrote,
        opened_connection=opened,
        local_port=port,
    )
    return observation


class TestGateBracket:
    """The latest refusal before the first grant, on the plan's own scale."""

    def test_refusals_then_a_grant_bracket_the_gate(self) -> None:
        rows = [
            _observation(4, RESERVE_REFUSED, wrote=995),
            _observation(1, RESERVE_REFUSED, wrote=980),
            _observation(3, RESERVE_ACCEPTED, wrote=990),
            _observation(2, RESERVE_REFUSED, wrote=985),
        ]

        (bracket,) = gate_brackets(rows)

        # Written +985 and +990, plus the 15ms lead: arrived +1000 and +1005.
        assert bracket["lastRefusedBeforeMs"] == 1000
        assert bracket["grantedMs"] == 1005
        assert bracket["refusedBefore"] == 2
        assert bracket["askedAfter"] == 1
        assert bracket["frame"] == "write+lead"
        assert (bracket["firstAskMs"], bracket["lastAskMs"]) == (995, 1010)

    def test_a_grant_on_the_first_ask_is_only_an_upper_bound(self) -> None:
        (bracket,) = gate_brackets([_observation(1, RESERVE_ACCEPTED, wrote=800)])

        assert bracket["grantedMs"] == 815
        assert bracket["lastRefusedBeforeMs"] is None

    def test_no_grant_brackets_nothing(self) -> None:
        rows = [_observation(i, RESERVE_REFUSED, wrote=960 + 5 * i) for i in (1, 2, 3)]

        (bracket,) = gate_brackets(rows)

        assert bracket["grantedMs"] is None
        assert bracket["refusedBefore"] == 3

    def test_send_times_stand_in_and_say_so(self) -> None:
        rows = [
            _observation(1, RESERVE_REFUSED, wrote=None, sent=990),
            _observation(2, RESERVE_ACCEPTED, wrote=None, sent=1000),
        ]

        (bracket,) = gate_brackets(rows)

        assert bracket["frame"] == "send+lead"
        assert (bracket["lastRefusedBeforeMs"], bracket["grantedMs"]) == (1005, 1015)

    def test_serial_asks_are_not_part_of_the_bracket(self) -> None:
        serial = _observation(1, RESERVE_ACCEPTED, wrote=4000)
        serial.burst_index = None

        assert gate_brackets([serial]) == []

    def test_each_slot_is_bracketed_on_its_own(self) -> None:
        rows = [
            _observation(1, RESERVE_REFUSED, wrote=990, slot=time(8, 38)),
            _observation(2, RESERVE_ACCEPTED, wrote=990, slot=time(9, 23)),
        ]

        brackets = {b["slot"]: b for b in gate_brackets(rows)}

        assert brackets["08:38 AM"]["grantedMs"] is None
        assert brackets["09:23 AM"]["grantedMs"] == 1005

    def test_the_log_line_names_the_bracket(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("INFO", logger="app.providers.walden_http_booker")
        (bracket,) = gate_brackets(
            [
                _observation(1, RESERVE_REFUSED, wrote=985),
                _observation(2, RESERVE_ACCEPTED, wrote=990),
            ]
        )

        _log_gate_bracket(bracket)

        assert "GATE_BRACKET: 08:38 AM opened in (+1000, +1005]ms past 06:30:00" in caplog.text


class TestWriteSummary:
    """Did members leave on plan, on warm connections?"""

    def test_drift_is_measured_against_the_plan(self) -> None:
        rows = [
            _observation(1, RESERVE_REFUSED, wrote=800, planned=800, port=1),
            _observation(2, RESERVE_REFUSED, wrote=821, planned=820, port=2),
            _observation(3, RESERVE_REFUSED, wrote=845, planned=840, port=3),
            _observation(4, RESERVE_ACCEPTED, wrote=861, planned=860, opened=True, port=4),
        ]

        summary = summarize_burst_writes(rows)

        assert summary["members"] == 4
        assert summary["warm"] == 3
        assert summary["dialled"] == 1
        assert summary["driftMsMax"] == 5
        assert summary["lateOver2Ms"] == 1
        assert summary["distinctConnections"] == 4

    def test_an_unmeasured_burst_reports_none_rather_than_zero(self) -> None:
        rows = [_observation(1, RESERVE_REFUSED, wrote=None, sent=None, opened=None)]

        summary = summarize_burst_writes(rows)

        assert summary["driftMsMax"] is None
        assert summary["distinctConnections"] is None
        assert summary["warm"] == summary["dialled"] == 0


class TestPlanDescription:
    def test_the_default_plan_reads_the_way_it_was_specified(self) -> None:
        offsets = list(range(-190, -30, 20)) + list(range(-30, 31, 5)) + list(range(50, 171, 20))

        assert describe_burst_plan(offsets, base_ms=1005) == (
            "+815..+975 every 20ms, +975..+1035 every 5ms, +1035..+1175 every 20ms (28 members)"
        )

    def test_one_member_and_none(self) -> None:
        assert describe_burst_plan([0], base_ms=1005) == "+1005 (1 member)"
        assert describe_burst_plan([]) == "no members"


class TestCpuTelemetry:
    """Parsers for the kernel files, and the arithmetic on top."""

    def test_cgroup_v2_cpu_stat(self) -> None:
        text = (
            "usage_usec 1500000\nuser_usec 1000000\nsystem_usec 500000\n"
            "nr_periods 40\nnr_throttled 3\nthrottled_usec 120000\n"
        )
        stats = cpu_telemetry.parse_cpu_stat(text)
        assert stats["usage_usec"] == 1_500_000
        assert stats["nr_throttled"] == 3
        assert stats["throttled_usec"] == 120_000

    def test_quota_readers(self) -> None:
        assert cpu_telemetry.parse_cpu_max("200000 100000\n") == 2.0
        assert cpu_telemetry.parse_cpu_max("max 100000\n") is None
        assert cpu_telemetry.parse_cpu_max("garbage") is None
        assert cpu_telemetry.parse_cfs_quota("100000\n", "100000\n") == 1.0
        assert cpu_telemetry.parse_cfs_quota("-1\n", "100000\n") is None

    def test_pressure_and_schedstat(self) -> None:
        psi = "some avg10=0.00 avg60=0.10 avg300=0.05 total=12345\nfull avg10=0.00 total=99\n"
        assert cpu_telemetry.parse_psi_some_total_us(psi) == 12345
        assert cpu_telemetry.parse_psi_some_total_us("full total=1\n") is None
        assert cpu_telemetry.parse_schedstat_run_delay_ns("1000 2500 7\n") == 2500
        assert cpu_telemetry.parse_schedstat_run_delay_ns("") is None

    def test_delta_arithmetic_and_unmeasured_fields(self) -> None:
        before = cpu_telemetry.CpuSnapshot(
            wall_s=10.0,
            process_cpu_s=1.0,
            cgroup_usage_us=1_000_000,
            nr_periods=10,
            nr_throttled=1,
            throttled_us=5_000,
            psi_some_us=None,
            run_delay_ns=2_000_000,
            threads=3,
        )
        after = cpu_telemetry.CpuSnapshot(
            wall_s=10.5,
            process_cpu_s=1.2,
            cgroup_usage_us=1_600_000,
            nr_periods=15,
            nr_throttled=3,
            throttled_us=25_000,
            psi_some_us=4_000,
            run_delay_ns=2_500_000,
            threads=30,
        )

        delta = cpu_telemetry.cpu_delta(before, after)

        assert delta["wallMs"] == 500.0
        assert delta["processCpuMs"] == 200.0
        assert delta["containerCpuMs"] == 600.0
        assert delta["busyCpus"] == 1.2
        assert delta["throttledPeriods"] == 2
        assert delta["throttledMs"] == 20.0
        assert delta["cpuPressureMs"] is None
        assert delta["runQueueDelayMs"] == 0.5
        assert delta["threads"] == 30

    def test_the_live_readers_never_raise_on_this_platform(self) -> None:
        """Whatever this machine exposes, the answer is a value or None, not an error."""
        snapshot = cpu_telemetry.take_snapshot()
        environment = cpu_telemetry.cpu_environment()

        assert snapshot.wall_s > 0
        assert set(environment) == {"visibleCpus", "usableCpus", "cgroupCpuLimit"}


class TestRunRecord:
    """The run's own measurements are stored beside its ledger."""

    def test_run_json_carries_the_timing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = WaldenGolfProvider("test_member", "test_password")
        monkeypatch.setenv("DEBUG_ARTIFACTS_BUCKET", "artifacts")
        result = DirectBookingResult()
        result.attempt_log = [_observation(1, RESERVE_REFUSED, wrote=990)]
        result.timing = {
            "burstAimMs": 1005,
            "burstWrites": {"members": 28},
            "gateBrackets": [{"slot": "08:38 AM", "grantedMs": 1005}],
        }

        with patch.object(provider, "_upload_bytes_to_gcs", return_value="gs://x") as upload:
            provider._capture_race_ledger(result, 1_790_335_799_999)

        runs = [
            call
            for call in upload.call_args_list
            if call.kwargs["object_name"].endswith("/run.json")
        ]
        assert len(runs) == 1
        record = json.loads(runs[0].kwargs["data"])
        assert record["targetTimestampMs"] == 1_790_335_799_999
        assert record["timing"]["burstAimMs"] == 1005
        assert record["timing"]["gateBrackets"][0]["grantedMs"] == 1005
