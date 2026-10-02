"""
Tests for scripts/check_report_names.py, the pre-commit name check the
race-report Routine runs on its own report (race-report skill §8a).

The tee sheet is the anonymised fixture, so every "name" here is a placeholder
like ``Member, Aa`` - which is all the check needs: it treats whatever the
sheet names as forbidden, without knowing whether it is real.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_report_names.py"
_spec = importlib.util.spec_from_file_location("check_report_names", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_report_names = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_report_names
_spec.loader.exec_module(check_report_names)

_SHEET = Path(__file__).resolve().parent / "fixtures" / "walden_tee_time_loaded.html"


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    run = tmp_path / "artifacts" / "walden" / "postrace" / "20261002_113024_for_20261009"
    run.mkdir(parents=True)
    shutil.copy(_SHEET, run / "tee_sheet.html")
    return tmp_path / "artifacts"


def _report(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "report.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestSheetNames:
    def test_reads_holders_in_both_orders(self, artifacts: Path) -> None:
        names = check_report_names.sheet_names(artifacts)
        assert "Member, Aa" in names
        assert "Aa Member" in names

    def test_skips_placeholders_that_name_nobody(self) -> None:
        assert check_report_names._variants("(TBD)") == set()
        assert check_report_names._variants("ACCOUNT, GUEST") == set()


class TestFind:
    def test_label_is_clean(self) -> None:
        assert check_report_names.find("08:38 held by Rival 1.", {"Member, Aa"}) == []

    def test_name_in_a_quoted_log_line_is_caught(self) -> None:
        hits = check_report_names.find("ok\n> `Member: member, aa is restricted`", {"Member, Aa"})
        assert hits == [(2, "m~10")]

    def test_word_boundary_and_handles(self) -> None:
        forms = {"@friendly", "Bob"}
        assert check_report_names.find("Bobby wrote to @friendlyish", forms) == []
        assert [n for n, _ in check_report_names.find("for @friendly, Bob said", forms)] == [1, 1]

    def test_output_never_contains_the_name(self) -> None:
        ((_, mask),) = check_report_names.find("Member, Aa", {"Member, Aa"})
        assert "Aa" not in mask


class TestMain:
    def test_report_naming_a_sheet_holder_fails(
        self, tmp_path: Path, artifacts: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _report(tmp_path, "08:38 was held by Aa Member's foursome.\n")
        assert check_report_names.main([str(report), "--artifacts", str(artifacts)]) == 1
        assert "Aa" not in capsys.readouterr().out

    def test_labelled_report_passes(self, tmp_path: Path, artifacts: Path) -> None:
        report = _report(tmp_path, "08:38 was held by Rival 1's foursome.\n")
        assert check_report_names.main([str(report), "--artifacts", str(artifacts)]) == 0

    def test_labels_file_adds_forms_not_on_any_sheet(self, tmp_path: Path) -> None:
        labels = tmp_path / "labels.json"
        labels.write_text(
            json.dumps({"people": [{"label": "member A", "forms": ["@examplehandle"]}]}),
            encoding="utf-8",
        )
        report = _report(tmp_path, '"for @examplehandle book 9/23" received\n')
        assert check_report_names.main([str(report), "--labels", str(labels)]) == 1

    def test_nothing_to_check_against_is_not_a_pass(self, tmp_path: Path) -> None:
        report = _report(tmp_path, "anything\n")
        assert check_report_names.main([str(report)]) == 2


class TestReviewFindings:
    """Each case let a name through before #238's review round."""

    def test_name_wrapped_across_lines(self) -> None:
        assert check_report_names.find("held by Jane\nDoe at 08:38", {"Jane Doe"}) == [(1, "J~8")]

    def test_bare_name_straight_after_at(self) -> None:
        assert check_report_names.find("asked for @Bob", {"Bob"}) == [(1, "B~3")]

    def test_unreadable_labels_file_is_not_a_pass(self, tmp_path: Path, artifacts: Path) -> None:
        labels = tmp_path / "labels.json"
        labels.write_text("{not json", encoding="utf-8")
        report = _report(tmp_path, "08:38 was held by Rival 1's foursome.\n")
        args = [str(report), "--artifacts", str(artifacts), "--labels", str(labels)]
        assert check_report_names.main(args) == 2

    def test_missing_artifacts_directory_is_not_a_pass(self, tmp_path: Path) -> None:
        labels = tmp_path / "labels.json"
        labels.write_text(json.dumps({"people": [{"forms": ["Bob"]}]}), encoding="utf-8")
        report = _report(tmp_path, "clean\n")
        args = [str(report), "--artifacts", str(tmp_path / "absent"), "--labels", str(labels)]
        assert check_report_names.main(args) == 2

    def test_unparseable_sheet_is_not_skipped(
        self, artifacts: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_: bytes) -> list[object]:
            raise ValueError("unparseable")

        monkeypatch.setattr(check_report_names.observer_observations, "parse_sheet", boom)
        with pytest.raises(check_report_names.SourceError):
            check_report_names.sheet_names(artifacts)
