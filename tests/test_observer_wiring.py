"""Tests for the observer job's wiring: which sheet it watches, and what it writes.

The read-only guarantee and the re-read-before-every-snapshot property are
checked in ``tests/test_observer.py``; this file covers target resolution and
the artifact layout.
"""

import json
import logging
from datetime import date, datetime, timedelta
from datetime import time as dtime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app import log_safety
from app.config import Settings
from app.observer import run as observer_run
from app.observer import sheet as observer_sheet
from app.providers.walden_sheet_grid import parse_rows
from app.services.credential_service import WaldenCredentialRequiredError
from app.utils.timezone import CTDateTime


def _booking(booking_id: str, phone: str, when: date, at: dtime) -> SimpleNamespace:
    return SimpleNamespace(
        id=booking_id,
        phone_number=phone,
        request=SimpleNamespace(requested_date=when, requested_time=at),
    )


WINDOW = CTDateTime.now().replace(hour=6, minute=30, second=0, microsecond=0)


def _due(bookings: list[SimpleNamespace]) -> Any:
    return patch.object(
        observer_run.database_service, "get_due_bookings", new=AsyncMock(return_value=bookings)
    )


def _creds(credentials: object | None) -> Any:
    """Patch the credential lookup the observer actually makes.

    None means "this requester has no login on file", which
    require_credentials() signals by raising rather than returning - so the
    absent case has to be a side_effect, not a return value. Patching the
    return value alone is what let the observer ship calling a method that had
    already been removed.
    """
    if credentials is None:
        lookup = AsyncMock(side_effect=WaldenCredentialRequiredError("no login on file"))
    else:
        lookup = AsyncMock(return_value=credentials)
    return patch.object(observer_run.credential_service, "require_credentials", new=lookup)


class TestResolveTarget:
    """Which sheet the observer parks on, and whose login it uses."""

    async def test_watches_the_date_the_due_booking_races_for(self) -> None:
        creds = SimpleNamespace(member_number="m1", password="p1")
        due = [_booking("b1", "+15550001", date(2026, 9, 18), dtime(8, 38))]
        with (
            _due(due),
            _creds(creds),
            patch.object(observer_run.settings, "observer_phone_number", "+15550001"),
        ):
            resolved = await observer_run._resolve_target(WINDOW)
        assert resolved == (date(2026, 9, 18), "m1", "p1")

    async def test_scopes_to_the_configured_requester(self) -> None:
        """Someone else's booking must not redirect the observer's date."""
        creds = SimpleNamespace(member_number="m1", password="p1")
        due = [
            _booking("other", "+15559999", date(2026, 9, 20), dtime(7, 0)),
            _booking("mine", "+15550001", date(2026, 9, 18), dtime(8, 38)),
        ]
        with (
            _due(due),
            _creds(creds),
            patch.object(observer_run.settings, "observer_phone_number", "+15550001"),
        ):
            resolved = await observer_run._resolve_target(WINDOW)
        assert resolved is not None
        assert resolved[0] == date(2026, 9, 18)

    async def test_takes_the_earliest_when_the_requester_has_several(self) -> None:
        creds = SimpleNamespace(member_number="m1", password="p1")
        due = [
            _booking("late", "+15550001", date(2026, 9, 19), dtime(9, 30)),
            _booking("early", "+15550001", date(2026, 9, 18), dtime(8, 38)),
        ]
        with (
            _due(due),
            _creds(creds),
            patch.object(observer_run.settings, "observer_phone_number", "+15550001"),
        ):
            resolved = await observer_run._resolve_target(WINDOW)
        assert resolved is not None
        assert resolved[0] == date(2026, 9, 18)

    async def test_with_nothing_due_it_still_watches_todays_opening_sheet(self) -> None:
        """The control group is the cheap half of this job's value."""
        creds = SimpleNamespace(member_number="m1", password="p1")
        with (
            _due([]),
            _creds(creds),
            patch.object(observer_run.settings, "observer_phone_number", "+15550001"),
            patch.object(observer_run.settings, "days_in_advance", 7),
        ):
            resolved = await observer_run._resolve_target(WINDOW)
        assert resolved is not None
        assert resolved[0] == WINDOW.date() + timedelta(days=7)

    async def test_nothing_due_and_no_watcher_is_nothing_to_watch(self) -> None:
        """The quiet morning: not a credential failure, just no work.

        Previously this returned None and the job exited 1, marking a failed
        Cloud Run execution on every morning nobody had booked.
        """
        with (
            _due([]),
            _creds(None),
            patch.object(observer_run.settings, "observer_phone_number", ""),
            patch.object(observer_run.settings, "user_phone_number", ""),
        ):
            with pytest.raises(observer_run.NothingToWatchError):
                await observer_run._resolve_target(WINDOW)

    async def test_a_configured_watcher_without_a_login_is_still_a_failure(self) -> None:
        """Someone was meant to be watched, so this one is a fault, not a no-op."""
        with (
            _due([]),
            _creds(None),
            patch.object(observer_run.settings, "observer_phone_number", "+15550001"),
        ):
            assert await observer_run._resolve_target(WINDOW) is None

    async def test_a_due_booking_without_a_login_is_still_a_failure(self) -> None:
        """A requester racing this morning with no login on file is news."""
        due = [_booking("b1", "+15550002", date(2026, 9, 18), dtime(8, 38))]
        with (
            _due(due),
            _creds(None),
            patch.object(observer_run.settings, "observer_phone_number", ""),
            patch.object(observer_run.settings, "user_phone_number", ""),
        ):
            assert await observer_run._resolve_target(WINDOW) is None

    async def test_uses_the_credentials_of_the_booking_it_watches(self) -> None:
        """The observer must read the sheet as the member who will race for it."""
        creds = SimpleNamespace(member_number="m2", password="p2")
        due = [_booking("b1", "+15550002", date(2026, 9, 18), dtime(8, 38))]
        lookup = AsyncMock(return_value=creds)
        with (
            _due(due),
            patch.object(observer_run.credential_service, "require_credentials", new=lookup),
            patch.object(observer_run.settings, "observer_phone_number", ""),
            patch.object(observer_run.settings, "user_phone_number", ""),
        ):
            resolved = await observer_run._resolve_target(WINDOW)
        assert resolved == (date(2026, 9, 18), "m2", "p2")
        lookup.assert_awaited_once_with("+15550002")


class TestObserveWithoutCredentials:
    """The whole run, not just the resolution step, when there is no login.

    _resolve_target returning None is the unit of the decision, but the
    property that matters at 06:24 is what observe() does with it: give up
    before the browser starts, and report the morning as unproductive rather
    than raising into the job's exit code. Nothing asserted that end to end -
    the gap CodeRabbit flagged on #195 - so a future change that launched
    Chrome before resolving, or let the refusal propagate, would pass every
    other test in this file.
    """

    async def test_it_gives_up_before_starting_a_browser(self) -> None:
        """No login on file means no Chrome, and a morning reported unproductive."""
        create_driver = MagicMock()
        with (
            _due([]),
            _creds(None),
            patch.object(observer_run.settings, "observer_enabled", True),
            patch.object(observer_run.settings, "observer_phone_number", "+15550001"),
            patch.object(observer_run.sheet, "create_driver", new=create_driver),
        ):
            produced = await observer_run.observe()

        assert produced is False, "a morning with no login produced no evidence"
        create_driver.assert_not_called()

    async def test_a_quiet_morning_exits_cleanly_without_a_browser(self) -> None:
        """Nothing due and nobody configured: a no-op, reported as success.

        The distinction the job's exit code carries: this morning is not a
        failed execution, unlike the test above, where someone was configured
        to be watched and could not be.
        """
        create_driver = MagicMock()
        with (
            _due([]),
            _creds(None),
            patch.object(observer_run.settings, "observer_enabled", True),
            patch.object(observer_run.settings, "observer_phone_number", ""),
            patch.object(observer_run.settings, "user_phone_number", ""),
            patch.object(observer_run.sheet, "create_driver", new=create_driver),
        ):
            produced = await observer_run.observe()

        assert produced is True, "a quiet morning is a clean no-op, not a failure"
        create_driver.assert_not_called()

    def test_main_exits_zero_on_a_quiet_morning(self) -> None:
        """End to end: what Cloud Run actually records for the execution."""
        with (
            _due([]),
            _creds(None),
            patch.object(observer_run.settings, "observer_enabled", True),
            patch.object(observer_run.settings, "observer_phone_number", ""),
            patch.object(observer_run.settings, "user_phone_number", ""),
            patch.object(observer_run.sheet, "create_driver", new=MagicMock()),
        ):
            assert observer_run.main() == 0


class TestStore:
    """Writing the morning up, after the window."""

    @staticmethod
    def _prep() -> observer_sheet.Preparation:
        return observer_sheet.Preparation(
            target_date=date(2026, 9, 18),
            selected_tab_text="Friday Fri 18 September Sep",
            clicked_tab=False,
            northgate_row_count=1052,
            sheet_bytes=703_272,
            ready_at_epoch_ms=-210_000,
        )

    @staticmethod
    def _snaps(n: int) -> list[observer_sheet.Snapshot]:
        return [
            observer_sheet.Snapshot(
                index=i,
                planned_offset_ms=i * 1000,
                sent_offset_ms=i * 1000 + 3,
                settled_offset_ms=i * 1000 + 700,
                captured_offset_ms=i * 1000 + 760,
                html=b"<html/>",
                refresh_ok=True,
            )
            for i in range(n)
        ]

    def test_stores_every_snapshot_plus_the_run_record(self) -> None:
        uploads: list[str] = []

        def _capture(**kwargs: Any) -> str:
            uploads.append(kwargs["object_name"])
            return "gs://b/" + kwargs["object_name"]

        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", side_effect=_capture),
        ):
            observer_run._store("obs/x", self._prep(), self._snaps(9), 0)

        assert uploads[:9] == [f"obs/x/snapshot_+{i * 1000 + 3:04d}ms.html" for i in range(9)]
        assert uploads[9:] == ["obs/x/run.json", "obs/x/manifest.jsonl"]

    def test_one_failed_upload_does_not_cost_the_others(self) -> None:
        calls = {"n": 0}

        def _flaky(**kwargs: Any) -> str:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("503 from GCS")
            return "gs://b/x"

        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", side_effect=_flaky),
        ):
            observer_run._store("p", self._prep(), self._snaps(3), 0)

        # 3 snapshots + run.json + manifest.jsonl were all attempted regardless.
        assert calls["n"] == 5

    def test_the_manifest_records_the_measured_offsets(self) -> None:
        written: dict[str, bytes] = {}

        def _capture(**kwargs: Any) -> str:
            written[kwargs["object_name"]] = kwargs["data"]
            return "gs://b/x"

        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", side_effect=_capture),
        ):
            observer_run._store("p", self._prep(), self._snaps(2), 0)

        rows = [json.loads(line) for line in written["p/manifest.jsonl"].decode().splitlines()]
        assert [r["sentOffsetMs"] for r in rows] == [3, 1003]
        assert [r["settledOffsetMs"] for r in rows] == [700, 1700]
        assert rows[0]["object"] == "snapshot_+0003ms.html"
        assert all(r["refreshOk"] for r in rows)

        record = json.loads(written["p/run.json"].decode())
        assert record["targetDate"] == "2026-09-18"
        assert record["northgateRowCount"] == 1052
        assert record["snapshotsCaptured"] == 2
        assert record["snapshotsStored"] == 2
        assert record["readyAtOffsetMs"] == -210_000

    def test_without_a_bucket_nothing_is_uploaded(self) -> None:
        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value=None),
            patch.object(observer_run.artifacts, "upload_bytes") as upload,
        ):
            stored = observer_run._store("p", self._prep(), self._snaps(2), 0)
        upload.assert_not_called()
        assert stored == 0


class TestStoreReportsWhatItStored:
    """A run that loses every upload must not exit 0.

    The only product of this job is evidence. A morning that captured nine
    snapshots, failed every upload and reported success would look identical to
    a good one until someone went looking for the bytes a Friday later.
    """

    def test_returns_the_number_actually_stored(self) -> None:
        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", return_value="gs://b/x"),
        ):
            stored = observer_run._store("p", TestStore._prep(), TestStore._snaps(9), 0)
        assert stored == 9

    def test_returns_zero_when_every_snapshot_upload_fails(self) -> None:
        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", side_effect=RuntimeError("503")),
        ):
            stored = observer_run._store("p", TestStore._prep(), TestStore._snaps(9), 0)
        assert stored == 0

    def test_a_partial_morning_still_counts(self) -> None:
        """Eight of nine sheets still separates the two models."""
        calls = {"n": 0}

        def _flaky(**kwargs: Any) -> str:
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("503")
            return "gs://b/x"

        with (
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", side_effect=_flaky),
        ):
            stored = observer_run._store("p", TestStore._prep(), TestStore._snaps(9), 0)
        assert stored == 8


class TestRunId:
    """Collision resistance, and the timestamp an operator would look for."""

    def test_carries_the_cloud_run_execution_id(self) -> None:
        with patch.dict("os.environ", {"CLOUD_RUN_EXECUTION": "teetime-observer-abc12"}):
            run_id = observer_run._run_id()
        assert run_id.endswith("_teetime-observer-abc12")
        datetime.strptime(run_id.split("_teetime")[0], "%Y%m%d_%H%M%S")

    def test_falls_back_to_a_random_suffix_off_cloud_run(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            first = observer_run._run_id()
            second = observer_run._run_id()
        assert "local-" in first
        # Two runs in the same second must not collide: objectCreator cannot
        # overwrite, so a shared prefix makes the second run's uploads fail.
        assert first != second

    def test_prefix_nests_the_run_id_under_the_target_date(self) -> None:
        prefix = observer_run._object_prefix(date(2026, 9, 18), "20260918_112400_exec-1")
        assert prefix == "walden/observer/2026-09-18/20260918_112400_exec-1"


class TestReadinessOrdering:
    """The fail-safe ordering: the observer's login must precede the racer's."""

    WINDOW = CTDateTime.now().replace(hour=6, minute=30, second=0, microsecond=0)

    def test_ready_before_the_racer_logs_in_is_routine(self, caplog: Any) -> None:
        ready = self.WINDOW.replace(hour=6, minute=25)
        with caplog.at_level(logging.INFO, logger=observer_run.logger.name):
            observer_run._report_readiness(ready, self.WINDOW)
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_ready_after_the_racer_logs_in_is_an_error(self, caplog: Any) -> None:
        """This inverts the ordering the whole fail-safe rests on."""
        ready = self.WINDOW.replace(hour=6, minute=29)
        with caplog.at_level(logging.INFO, logger=observer_run.logger.name):
            observer_run._report_readiness(ready, self.WINDOW)
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "NEWER session" in errors[0]

    def test_ready_after_the_window_is_an_error(self, caplog: Any) -> None:
        ready = self.WINDOW.replace(hour=6, minute=31)
        with caplog.at_level(logging.INFO, logger=observer_run.logger.name):
            observer_run._report_readiness(ready, self.WINDOW)
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "PAST the window" in errors[0]


class TestRedaction:
    """Requester identities are last-4 only in logs (CWE-532)."""

    def test_keeps_only_the_last_four_characters(self) -> None:
        assert observer_run._redacted("+15125550123") == "...0123"

    def test_short_and_empty_identities(self) -> None:
        assert observer_run._redacted("0123") == "0123"
        assert observer_run._redacted("") == "any requester"

    def test_the_full_identity_never_reaches_the_log(self, caplog: Any) -> None:
        phone = "+15125550123"
        creds = SimpleNamespace(member_number="m1", password="p1")
        with (
            _due([]),
            _creds(creds),
            patch.object(observer_run.settings, "observer_phone_number", phone),
            caplog.at_level(logging.INFO, logger=observer_run.logger.name),
        ):
            import asyncio

            asyncio.run(observer_run._resolve_target(WINDOW))
        assert phone not in caplog.text
        assert "...0123" in caplog.text


class TestWireLoggersAreSilenced:
    """The observer's own basicConfig must not re-expose the login payload (CWE-532).

    selenium logs every command payload at DEBUG, including the send_keys body
    that carries the member number and password. The observer ran at
    LOG_LEVEL=DEBUG on 2026-09-13 and wrote both to Cloud Logging in cleartext.
    """

    @pytest.mark.parametrize("name", log_safety.WIRE_LOGGERS)
    def test_main_pins_each_wire_logger_at_warning(self, name: str) -> None:
        def _close_without_running(coro: Any) -> bool:
            coro.close()  # main() builds observe(); nothing here should drive a browser
            return True

        logging.getLogger(name).setLevel(logging.DEBUG)
        with (
            patch.object(observer_run.settings, "log_level", "DEBUG"),
            patch.object(observer_run.asyncio, "run", side_effect=_close_without_running),
        ):
            observer_run.main()
        assert logging.getLogger(name).level == logging.WARNING

    def test_basic_config_alone_would_leave_them_verbose(self) -> None:
        """Guards the ordering: silencing has to happen after basicConfig."""
        logging.getLogger("selenium").setLevel(logging.DEBUG)
        logging.basicConfig(level=logging.DEBUG, force=True)
        assert logging.getLogger("selenium").level == logging.DEBUG
        log_safety.silence_wire_loggers()
        assert logging.getLogger("selenium").level == logging.WARNING


class TestCadenceSettingsValidation:
    """Both bad cadences fail silently at runtime, so they are rejected at load."""

    def test_defaults_are_nine_snapshots_one_second_apart(self) -> None:
        config = Settings(_env_file=None)
        assert config.observer_snapshot_count == 9
        assert config.observer_snapshot_interval_ms == 1000
        assert config.observer_snapshot_start_offset_ms == -500

    @pytest.mark.parametrize("count", [0, -1])
    def test_a_non_positive_count_is_rejected(self, count: int) -> None:
        """range(0) captures nothing at all."""
        with pytest.raises(ValidationError, match="observer_snapshot_count"):
            Settings(_env_file=None, observer_snapshot_count=count)

    @pytest.mark.parametrize("interval", [0, -1000])
    def test_a_non_positive_interval_is_rejected(self, interval: int) -> None:
        """Every offset would collapse onto the window, recording one instant nine times."""
        with pytest.raises(ValidationError, match="observer_snapshot_interval_ms"):
            Settings(_env_file=None, observer_snapshot_interval_ms=interval)


def _grid_html(*times: str, css: str = "Empty") -> bytes:
    rows = "".join(
        f'<div id="f:teeTimeCourses:0:teeTimeSlots:{i}:slotTee:0:slotTeeDIV" class="{css}">'
        f'<div><label class="custom-time-label">{t}</label></div></div>'
        for i, t in enumerate(times)
    )
    return f"<html><body>{rows}</body></html>".encode()


class TestRecordGrids:
    """The slot grids recorded after the window (issue #216)."""

    WATCHED = date(2026, 10, 3)

    @staticmethod
    def _snapshot(html: bytes) -> observer_sheet.Snapshot:
        return observer_sheet.Snapshot(
            index=0,
            planned_offset_ms=-500,
            sent_offset_ms=-497,
            settled_offset_ms=300,
            captured_offset_ms=350,
            html=html,
            refresh_ok=True,
        )

    @staticmethod
    def _read(when: date, *times: str, unparsed: int = 0) -> observer_sheet.HorizonRead:
        slots, _ = parse_rows(_grid_html(*times))
        return observer_sheet.HorizonRead(
            sheet_date=when,
            landed=True,
            selected_tab_text=when.strftime("%A"),
            html=_grid_html(*times),
            slots=slots,
            unparsed=unparsed,
        )

    async def _run(
        self,
        reads: list[observer_sheet.HorizonRead],
        *,
        days: int = 2,
        save: AsyncMock | None = None,
    ) -> tuple[AsyncMock, MagicMock, dict[str, bytes]]:
        save = save or AsyncMock()
        read_horizon = MagicMock(return_value=reads)
        written: dict[str, bytes] = {}

        def _upload(**kwargs: Any) -> str:
            written[kwargs["object_name"]] = kwargs["data"]
            return "gs://b/" + kwargs["object_name"]

        with (
            patch.object(observer_run.settings, "observer_horizon_days", days),
            patch.object(observer_run.sheet, "read_horizon", new=read_horizon),
            patch.object(
                observer_run.database_service, "ensure_tee_sheet_grid_table", new=AsyncMock()
            ),
            patch.object(observer_run.database_service, "save_tee_sheet_grid", new=save),
            patch.object(observer_run.artifacts, "artifacts_bucket", return_value="bkt"),
            patch.object(observer_run.artifacts, "upload_bytes", side_effect=_upload),
        ):
            await observer_run._record_grids(
                MagicMock(),
                self.WATCHED,
                "walden/observer/2026-10-03/run",
                [self._snapshot(_grid_html("07:15 AM", "07:23 AM", "07:30 AM"))],
            )
        return save, read_horizon, written

    @pytest.mark.asyncio
    async def test_the_watched_date_and_the_days_after_it_are_recorded(self) -> None:
        reads = [
            self._read(date(2026, 10, 4), "07:15 AM"),
            self._read(date(2026, 10, 5), "07:30 AM", "07:38 AM"),
        ]

        save, read_horizon, _ = await self._run(reads)

        assert read_horizon.call_args.args[1] == [date(2026, 10, 4), date(2026, 10, 5)]
        recorded = {call.args[0]: len(call.args[1]) for call in save.await_args_list}
        assert recorded == {self.WATCHED: 3, date(2026, 10, 4): 1, date(2026, 10, 5): 2}
        assert all(call.kwargs["source"] == "observer" for call in save.await_args_list)

    @pytest.mark.asyncio
    async def test_the_summary_says_what_each_date_rendered(self) -> None:
        """horizon.json is where the first answer to "does D+8 render?" lives."""
        reads = [
            self._read(date(2026, 10, 4), "07:15 AM"),
            observer_sheet.HorizonRead(sheet_date=date(2026, 10, 5), landed=True, html=b"<html/>"),
            observer_sheet.HorizonRead(
                sheet_date=date(2026, 10, 6), landed=False, note="could not be confirmed"
            ),
        ]

        _, _, written = await self._run(reads, days=3)

        summary = json.loads(written["walden/observer/2026-10-03/run/horizon.json"])
        assert [(e["date"], e["daysPastWatched"], e["landed"]) for e in summary] == [
            ("2026-10-04", 1, True),
            ("2026-10-05", 2, True),
            ("2026-10-06", 3, False),
        ]
        assert summary[0]["northgateRows"] == 1
        assert summary[0]["first"] == "07:15"
        assert summary[1]["northgateRows"] == 0
        assert summary[2]["note"] == "could not be confirmed"
        assert "walden/observer/2026-10-03/run/horizon/2026-10-04.html" in written

    @pytest.mark.asyncio
    async def test_empty_unconfirmed_and_holey_grids_are_not_offered(self) -> None:
        """Each is evidence worth keeping, and none is a grid to show a member."""
        reads = [
            observer_sheet.HorizonRead(sheet_date=date(2026, 10, 4), landed=True, html=b"<x/>"),
            observer_sheet.HorizonRead(sheet_date=date(2026, 10, 5), landed=False),
            self._read(date(2026, 10, 6), "07:15 AM", unparsed=1),
        ]

        save, _, _ = await self._run(reads, days=3)

        assert [call.args[0] for call in save.await_args_list] == [self.WATCHED]

    @pytest.mark.asyncio
    async def test_a_horizon_of_zero_reads_nothing_more(self) -> None:
        save, read_horizon, written = await self._run([], days=0)

        read_horizon.assert_not_called()
        assert [call.args[0] for call in save.await_args_list] == [self.WATCHED]
        assert written == {}

    @pytest.mark.asyncio
    async def test_a_database_failure_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        save = AsyncMock(side_effect=RuntimeError("connection is closed"))

        with caplog.at_level(logging.ERROR):
            await self._run([self._read(date(2026, 10, 4), "07:15 AM")], save=save)

        assert "could not record the slot grids" in caplog.text


class TestObserveReadsAheadLast:
    """The horizon read comes after the evidence, and cannot change the verdict."""

    @staticmethod
    def _prep() -> observer_sheet.Preparation:
        return observer_sheet.Preparation(
            target_date=date(2026, 10, 3),
            selected_tab_text="Saturday Sat 3 October Oct",
            clicked_tab=True,
            northgate_row_count=72,
            sheet_bytes=700_000,
            ready_at_epoch_ms=0,
        )

    async def _observe(self, record_grids: AsyncMock, events: list[str]) -> bool:
        snapshot = TestRecordGrids._snapshot(_grid_html("09:08 AM"))
        driver = MagicMock()
        driver.quit.side_effect = lambda: events.append("quit")

        def _store(*_args: Any) -> int:
            events.append("store")
            return 9

        async def _record(*_args: Any) -> None:
            events.append("record_grids")
            await record_grids()

        with (
            patch.object(observer_run.settings, "observer_enabled", True),
            patch.object(
                observer_run,
                "_resolve_target",
                new=AsyncMock(return_value=(date(2026, 10, 3), "m", "p")),
            ),
            patch.object(observer_run.sheet, "create_driver", return_value=driver),
            patch.object(observer_run.sheet, "log_in", return_value=True),
            patch.object(observer_run.sheet, "open_tee_sheet", return_value=True),
            patch.object(observer_run.sheet, "park_on_date", return_value=self._prep()),
            patch.object(observer_run.sheet, "capture_across_window", return_value=[snapshot]),
            patch.object(observer_run, "_store", side_effect=_store),
            patch.object(observer_run, "_record_grids", side_effect=_record),
        ):
            return await observer_run.observe()

    @pytest.mark.asyncio
    async def test_the_window_is_stored_before_anything_reads_ahead(self) -> None:
        events: list[str] = []

        produced = await self._observe(AsyncMock(), events)

        assert produced is True
        assert events == ["store", "record_grids", "quit"]

    @pytest.mark.asyncio
    async def test_a_failed_read_ahead_leaves_a_good_morning_good(self) -> None:
        events: list[str] = []

        produced = await self._observe(AsyncMock(side_effect=RuntimeError("boom")), events)

        assert produced is True
        assert events == ["store", "record_grids", "quit"]


class TestHorizonSetting:
    def test_defaults_to_a_week_past_the_watched_date(self) -> None:
        assert Settings(_env_file=None).observer_horizon_days == 7

    @pytest.mark.parametrize("value", [0, 30])
    def test_accepts_the_ends_of_its_range(self, value: int) -> None:
        assert Settings(_env_file=None, observer_horizon_days=value).observer_horizon_days == value

    @pytest.mark.parametrize("value", [-1, 31])
    def test_rejects_a_horizon_outside_it(self, value: int) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, observer_horizon_days=value)
