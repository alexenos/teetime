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
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.observer import run as observer_run
from app.observer import sheet as observer_sheet
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
    return patch.object(
        observer_run.credential_service, "resolve", new=AsyncMock(return_value=credentials)
    )


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

    async def test_no_credentials_means_no_run(self) -> None:
        with _due([]), _creds(None):
            assert await observer_run._resolve_target(WINDOW) is None

    async def test_uses_the_credentials_of_the_booking_it_watches(self) -> None:
        """The observer must read the sheet as the member who will race for it."""
        creds = SimpleNamespace(member_number="m2", password="p2")
        due = [_booking("b1", "+15550002", date(2026, 9, 18), dtime(8, 38))]
        resolve = AsyncMock(return_value=creds)
        with (
            _due(due),
            patch.object(observer_run.credential_service, "resolve", new=resolve),
            patch.object(observer_run.settings, "observer_phone_number", ""),
            patch.object(observer_run.settings, "user_phone_number", ""),
        ):
            resolved = await observer_run._resolve_target(WINDOW)
        assert resolved == (date(2026, 9, 18), "m2", "p2")
        resolve.assert_awaited_once_with("+15550002")


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


class TestCadenceSettingsValidation:
    """Both bad cadences fail silently at runtime, so they are rejected at load."""

    def test_defaults_are_nine_snapshots_one_second_apart(self) -> None:
        config = Settings(_env_file=None)
        assert config.observer_snapshot_count == 9
        assert config.observer_snapshot_interval_ms == 1000

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
