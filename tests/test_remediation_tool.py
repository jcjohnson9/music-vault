from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from music_vault.core import paths
from music_vault.core.db import MusicVaultDB
from music_vault.metadata.musicbrainz_enricher import (
    MetadataCandidate,
    MetadataProviderError,
)
from music_vault.metadata.remediation import RemediationService
from music_vault.metadata.remediation_schema import (
    PROVIDER_CACHE_TABLE,
    REMEDIATION_ITEMS_TABLE,
    REMEDIATION_JOBS_TABLE,
)
from tools.dev import remediate_library_metadata as tool


VALID_JOB_ID = "a" * 32
PRIVATE_TITLE = "SYNTHETIC-PRIVATE-TITLE-DO-NOT-PRINT"
PRIVATE_PATH = "C:/synthetic/private/media.mp3"


@pytest.fixture
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "runtime"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(root))
    paths._resolved_project_root.cache_clear()
    yield root
    paths._resolved_project_root.cache_clear()


def _invoke(capsys: pytest.CaptureFixture[str], *argv: str):
    result = tool.main(list(argv))
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    return result, json.loads(output[0]), output[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_default_status_does_not_create_a_missing_database_or_data_directory(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
):
    assert not (runtime_root / "data").exists()

    result, payload, _raw = _invoke(capsys)

    assert result == 0
    assert payload == {
        "action": "status",
        "current_schema_version": 4,
        "database_exists": False,
        "job": None,
        "migration_required": False,
        "ok": True,
        "schema_version": None,
    }
    assert not (runtime_root / "data").exists()


def test_status_reports_schema_three_without_migration_backup_or_write(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
):
    data = runtime_root / "data"
    data.mkdir()
    database_path = data / "music_vault.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel(value) VALUES('unchanged')")
        connection.execute("PRAGMA user_version=3")
    before = (
        _sha256(database_path),
        database_path.stat().st_size,
        database_path.stat().st_mtime_ns,
    )

    result, payload, _raw = _invoke(capsys, "status")

    assert result == 0
    assert payload["schema_version"] == 3
    assert payload["current_schema_version"] == 4
    assert payload["migration_required"] is True
    assert payload["job"] is None
    assert (
        _sha256(database_path),
        database_path.stat().st_size,
        database_path.stat().st_mtime_ns,
    ) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
    assert not (data / "backups").exists()
    assert not (data / "metadata_reports").exists()


def test_analyze_uses_production_service_and_status_reopens_read_only(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
):
    result, analyzed, raw = _invoke(capsys, "analyze")

    assert result == 0
    assert analyzed["action"] == "analyze"
    assert analyzed["status"] == "ready"
    assert analyzed["total"] == analyzed["analyzed"] == 0
    assert analyzed["provider_requests"] == 0
    assert analyzed["provider_cache_hits"] == 0
    assert "title" not in raw.casefold()
    assert "path" not in raw.casefold()

    database_path = runtime_root / "data" / "music_vault.sqlite3"
    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            f"SELECT COUNT(*) FROM {REMEDIATION_JOBS_TABLE}"
        ).fetchone()[0] == 1
        assert connection.execute(
            f"SELECT COUNT(*) FROM {PROVIDER_CACHE_TABLE}"
        ).fetchone()[0] == 0
    report = runtime_root / "data" / "metadata_reports" / analyzed["job_id"]
    assert {item.name for item in report.iterdir()} == {
        "items.json",
        "metrics.json",
        "summary.json",
    }

    before_database = (
        _sha256(database_path),
        database_path.stat().st_size,
        database_path.stat().st_mtime_ns,
    )
    before_reports = {
        item.name: (_sha256(item), item.stat().st_mtime_ns)
        for item in report.iterdir()
    }
    result, status, _raw = _invoke(capsys, "status")
    assert result == 0
    assert status["job"]["job_id"] == analyzed["job_id"]
    assert (
        _sha256(database_path),
        database_path.stat().st_size,
        database_path.stat().st_mtime_ns,
    ) == before_database
    assert {
        item.name: (_sha256(item), item.stat().st_mtime_ns)
        for item in report.iterdir()
    } == before_reports


def test_confirmed_apply_file_write_opt_in_and_rollback_reach_production_service(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
):
    result, first_job, _raw = _invoke(capsys, "analyze")
    assert result == 0

    result, applied, _raw = _invoke(
        capsys,
        "apply-high-confidence",
        "--job-id",
        first_job["job_id"],
        "--confirm-live-apply",
    )
    assert result == 0
    assert applied["status"] == "complete"
    assert applied["file_write_enabled"] is False
    assert applied["file_written"] == 0

    result, rolled_back, _raw = _invoke(
        capsys,
        "rollback",
        "--job-id",
        first_job["job_id"],
        "--confirm-rollback",
    )
    assert result == 0
    assert rolled_back["status"] == "rolled_back"

    result, second_job, _raw = _invoke(capsys, "analyze")
    assert result == 0
    assert second_job["job_id"] != first_job["job_id"]
    result, file_apply, _raw = _invoke(
        capsys,
        "apply-high-confidence",
        "--job-id",
        second_job["job_id"],
        "--confirm-live-apply",
        "--write-files",
    )
    assert result == 0
    assert file_apply["file_write_enabled"] is True
    assert file_apply["file_written"] == 0
    assert not list(runtime_root.rglob("*.mp3"))


@pytest.mark.parametrize("mixed_result", [False, True], ids=("failed-job", "mixed-job"))
def test_resume_retries_eligible_provider_failures_with_aggregate_only_output(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mixed_result: bool,
):
    private_artist = "SYNTHETIC-PRIVATE-ARTIST-DO-NOT-PRINT"
    unresolved_title = "SYNTHETIC-SECOND-TITLE-DO-NOT-PRINT"
    database_path = runtime_root / "data" / "music_vault.sqlite3"
    database = MusicVaultDB(database_path)
    database.upsert_track(
        runtime_root / "media" / "recoverable.mp3",
        title=PRIVATE_TITLE,
        artist=private_artist,
        duration_seconds=180.0,
    )
    if mixed_result:
        database.upsert_track(
            runtime_root / "media" / "unresolved.mp3",
            title=unresolved_title,
            artist=private_artist,
            duration_seconds=181.0,
        )
    database.close()

    class _RecoveringProvider:
        available = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def search(self, title: str, artist: str | None = None, **_kwargs):
            self.calls.append((title, artist))
            if title == PRIVATE_TITLE:
                if not self.available:
                    raise MetadataProviderError("synthetic_provider_unavailable")
                return [
                    MetadataCandidate(
                        title=PRIVATE_TITLE,
                        artist=private_artist,
                        album="Synthetic Official Album",
                        release_date="2001-02-03",
                        recording_id="11111111-1111-4111-8111-111111111111",
                        release_id="22222222-2222-4222-8222-222222222222",
                        score=100,
                        duration_seconds=180.0,
                        album_artist=private_artist,
                        release_status="Official",
                    )
                ]
            return []

    provider = _RecoveringProvider()

    def service_factory(database, **_kwargs):
        return RemediationService(
            database,
            provider=provider,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(tool, "RemediationService", service_factory)

    result, analyzed, first_raw = _invoke(capsys, "analyze")
    assert result == 0
    assert analyzed["status"] == ("ready" if mixed_result else "failed")
    assert analyzed["failed"] == 1
    assert analyzed["no_match"] == (1 if mixed_result else 0)
    assert PRIVATE_TITLE not in first_raw
    assert private_artist not in first_raw
    assert unresolved_title not in first_raw

    with sqlite3.connect(database_path) as connection:
        effective_before = connection.execute(
            "SELECT title, artist FROM tracks ORDER BY id"
        ).fetchall()
        history_before = int(
            connection.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0]
        )

    provider.available = True
    result, resumed, resumed_raw = _invoke(
        capsys,
        "resume",
        "--job-id",
        analyzed["job_id"],
    )

    assert result == 0
    assert resumed["action"] == "resume"
    assert resumed["status"] == "ready"
    assert resumed["total"] == (2 if mixed_result else 1)
    assert resumed["analyzed"] == resumed["total"]
    assert resumed["failed"] == 0
    assert resumed["high_confidence"] == 1
    assert resumed["no_match"] == (1 if mixed_result else 0)
    assert resumed["provider_requests"] > analyzed["provider_requests"]
    assert PRIVATE_TITLE not in resumed_raw
    assert private_artist not in resumed_raw
    assert unresolved_title not in resumed_raw
    assert "candidate" not in resumed_raw.casefold()
    assert "path" not in resumed_raw.casefold()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT title, artist FROM tracks ORDER BY id"
        ).fetchall() == effective_before
        assert int(
            connection.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0]
        ) == history_before
        statuses = connection.execute(
            f"SELECT status FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? ORDER BY track_id",
            (analyzed["job_id"],),
        ).fetchall()
    assert [row[0] for row in statuses] == (
        ["high_confidence", "no_match"] if mixed_result else ["high_confidence"]
    )
    assert not list((runtime_root / "media").glob("*.mp3"))


@pytest.mark.parametrize(
    ("argv", "expected_error"),
    [
        (
            ("apply-high-confidence", "--job-id", VALID_JOB_ID),
            "explicit_apply_confirmation_required",
        ),
        (
            ("apply-high-confidence", "--job-id", VALID_JOB_ID, "--write-files"),
            "file_write_requires_live_apply_confirmation",
        ),
        (
            ("rollback", "--job-id", VALID_JOB_ID),
            "explicit_rollback_confirmation_required",
        ),
        (("rollback", "--confirm-rollback"), "job_id_required"),
        (("resume",), "job_id_required"),
        (("status", "--job-id", "../../private"), "job_id_invalid"),
        (("analyze", "--write-files"), "apply_flags_require_apply_action"),
        (
            ("status", "--confirm-rollback"),
            "rollback_confirmation_requires_rollback_action",
        ),
    ],
)
def test_authorization_and_job_id_failures_happen_before_any_database_open(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    expected_error: str,
):
    result, payload, raw = _invoke(capsys, *argv)

    assert result == 2
    assert payload["ok"] is False
    assert payload["error"] == expected_error
    assert PRIVATE_TITLE not in raw
    assert PRIVATE_PATH not in raw
    assert not (runtime_root / "data").exists()


def test_status_and_explicit_export_are_aggregate_only(
    runtime_root: Path,
    capsys: pytest.CaptureFixture[str],
):
    database_path = runtime_root / "data" / "music_vault.sqlite3"
    database = MusicVaultDB(database_path)
    database.conn.execute(
        "INSERT INTO tracks(path, title) VALUES(?, ?)",
        (PRIVATE_PATH, PRIVATE_TITLE),
    )
    report_path = runtime_root / "data" / "metadata_reports" / VALID_JOB_ID
    database.conn.execute(
        f"""
        INSERT INTO {REMEDIATION_JOBS_TABLE} (
            id, created_at, updated_at, status, mode, provider,
            library_revision, total_items, analyzed_items, review_items,
            private_report_path
        ) VALUES (?, 't0', 't0', 'ready', 'dry_run', 'musicbrainz',
                  'revision', 1, 1, 1, ?)
        """,
        (VALID_JOB_ID, str(report_path)),
    )
    database.conn.execute(
        f"""
        INSERT INTO {REMEDIATION_ITEMS_TABLE} (
            job_id, track_id, status, current_snapshot, candidate_snapshot,
            confidence_score, confidence_class, created_at, updated_at
        ) VALUES (?, 1, 'needs_review', ?, ?, 80, 'needs_review', 't0', 't0')
        """,
        (
            VALID_JOB_ID,
            json.dumps({"path": PRIVATE_PATH, "title": PRIVATE_TITLE}),
            json.dumps({"title": PRIVATE_TITLE}),
        ),
    )
    database.conn.commit()
    database.close()
    before_database = _sha256(database_path)

    result, payload, raw = _invoke(capsys, "status", "--job-id", VALID_JOB_ID)
    assert result == 0
    assert payload["job"]["needs_review"] == 1
    assert "report_path" not in payload
    assert PRIVATE_TITLE not in raw
    assert PRIVATE_PATH not in raw
    assert _sha256(database_path) == before_database

    result, exported, raw = _invoke(
        capsys,
        "export-private-report",
        "--job-id",
        VALID_JOB_ID,
    )
    assert result == 0
    assert exported["report_exists"] is True
    assert "report_path" not in exported
    assert PRIVATE_TITLE not in raw
    assert PRIVATE_PATH not in raw
    private_items = (report_path / "items.json").read_text(encoding="utf-8")
    assert PRIVATE_TITLE in private_items
    assert PRIVATE_PATH in private_items


def test_tool_imports_no_gui_sync_or_api_key_modules_and_wrapper_restores_location():
    project_root = Path(__file__).resolve().parents[1]
    code = (
        "import json,sys; import tools.dev.remediate_library_metadata; "
        "blocked=['music_vault.app','music_vault.core.youtube_sync']; "
        "print(json.dumps([name for name in blocked if name in sys.modules]))"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=project_root,
        env={**os.environ, "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS": "1"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []

    source = Path(tool.__file__).read_text(encoding="utf-8")
    assert "music_vault.app" not in source
    assert "youtube_sync" not in source
    assert "youtube_api_key" not in source
    wrapper = (project_root / "tools" / "dev" / "remediate_library_metadata.ps1").read_text(
        encoding="utf-8"
    )
    assert "Push-Location" in wrapper
    assert "Pop-Location" in wrapper
    assert ".venv\\Scripts\\python.exe" in wrapper
    assert "@args" in wrapper
