from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from music_vault.core import app_status
from music_vault.core import db as db_module
from music_vault.core import paths as runtime_paths
from music_vault.core import acceptance_network
from music_vault.core.app_status import write_app_status
from music_vault.core.db import MusicVaultDB
from tools.dev import batch10_3_acceptance as acceptance
from tools.dev import run_batch10_3_packaged_migration_smoke as packaged
from tools.dev import run_batch10_3_source_migration_proof as source_proof
from tools.dev import verify_batch10_3_live_migration as live_gate


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _schema7_database_runtime():
    """Run historical Batch 10.3 fixtures without additive schema-8 state."""

    original_version = db_module.CURRENT_SCHEMA_VERSION
    original_create = db_module.create_media_quality_schema
    original_seed = db_module.seed_existing_track_media_quality
    db_module.CURRENT_SCHEMA_VERSION = 7
    db_module.create_media_quality_schema = lambda _connection: None
    db_module.seed_existing_track_media_quality = lambda _connection, *_track_ids: None
    try:
        yield
    finally:
        db_module.CURRENT_SCHEMA_VERSION = original_version
        db_module.create_media_quality_schema = original_create
        db_module.seed_existing_track_media_quality = original_seed


@pytest.fixture(autouse=True)
def _reset_runtime_paths(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(acceptance.NO_SECRETS_ENVIRONMENT, "1")
    runtime_paths.clear_configured_data_dir()
    runtime_paths._resolved_project_root.cache_clear()
    yield
    runtime_paths.clear_configured_data_dir()
    runtime_paths._resolved_project_root.cache_clear()
    gc.collect()


def _schema6_runtime(root: Path) -> tuple[Path, Path, dict[str, object]]:
    data = root / "data"
    backups = data / "backups"
    database = data / "music_vault.sqlite3"
    data.mkdir(parents=True)
    source_proof._create_synthetic_schema6(database, backups, root)
    baseline = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=6,
    )
    return data, database, baseline


def _migrate_source_mode(root: Path, database: Path) -> None:
    previous = os.environ.get("MUSIC_VAULT_PROJECT_ROOT")
    os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(root)
    runtime_paths._resolved_project_root.cache_clear()
    try:
        with _schema7_database_runtime():
            db = MusicVaultDB(
                database,
                backup_dir=root / "data" / "backups",
                legacy_failure_file=root / "data" / "youtube_failed_ids.txt",
            )
            write_app_status(db, {"onboarding_completed": True})
            db.close()
    finally:
        if previous is None:
            os.environ.pop("MUSIC_VAULT_PROJECT_ROOT", None)
        else:
            os.environ["MUSIC_VAULT_PROJECT_ROOT"] = previous
        runtime_paths._resolved_project_root.cache_clear()


def _dry_run(
    root: Path, data: Path, database: Path, baseline: dict[str, object]
) -> dict[str, object]:
    with _schema7_database_runtime():
        return live_gate.clone_dry_run(
            project_root=root,
            data_dir=data,
            database=database,
            baseline=baseline,
            temporary_parent=root.parent,
        )


def _network_report(
    root: Path, *, attempts: int = 0, finalized: bool = True
) -> Path:
    path = root / "batch10_3-network-report.json"
    acceptance.atomic_write_json(
        path,
        {
            "schema_version": acceptance.NETWORK_REPORT_FORMAT_VERSION,
            "guard_installed": True,
            "outbound_blocked": True,
            "attempt_count": attempts,
            "provider_factory_invocation_count": 0,
            "provider_task_dispatch_count": 0,
            "finalized": finalized,
            "request_details_recorded": False,
            "credential_contents_read": False,
        },
    )
    return path


def _insert_exact_duplicate_credit_evidence(database: Path) -> None:
    """Model a legacy schema-6 credit collision without exposing identities."""
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "ALTER TABLE track_artist_credits RENAME TO legacy_artist_credits"
        )
        connection.executescript(
            """
            CREATE TABLE track_artist_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                artist_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                credit_order INTEGER NOT NULL,
                join_phrase TEXT NOT NULL DEFAULT '',
                provenance TEXT NOT NULL,
                provider_reference TEXT,
                confidence REAL,
                is_manual INTEGER NOT NULL DEFAULT 0,
                is_locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE RESTRICT
            );
            INSERT INTO track_artist_credits SELECT * FROM legacy_artist_credits;
            DROP TABLE legacy_artist_credits;
            """
        )
        duplicate_artist_id = int(
            connection.execute(
                "SELECT id FROM artists WHERE normalized_name='fixture ensemble' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        target_credit_id = int(
            connection.execute(
                "SELECT id FROM track_artist_credits WHERE artist_id!=? "
                "ORDER BY track_id,id LIMIT 1",
                (duplicate_artist_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO track_artist_credits(
                track_id,artist_id,role,credit_order,join_phrase,provenance,
                provider_reference,confidence,is_manual,is_locked,created_at,updated_at
            )
            SELECT track_id,?,role,credit_order,join_phrase,provenance,
                   provider_reference,confidence,is_manual,is_locked,created_at,updated_at
            FROM track_artist_credits WHERE id=?
            """,
            (duplicate_artist_id, target_credit_id),
        )


def test_app_status_writer_keeps_aggregate_state_but_nulls_item_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setattr(app_status, "project_root", lambda: tmp_path)
    monkeypatch.setattr(app_status, "data_dir", lambda: data)
    monkeypatch.setattr(app_status, "app_status_path", lambda: data / "app-status.json")
    monkeypatch.setattr(app_status, "config_path", lambda: data / "config.json")
    monkeypatch.setattr(app_status, "path_resolution_source", lambda: "synthetic")
    monkeypatch.setattr(app_status, "_api_ready", lambda: False)
    monkeypatch.setattr(app_status, "_ffmpeg_ready", lambda _config=None: False)
    db = MusicVaultDB(tmp_path / "status.sqlite3")
    private_values = {
        "PRIVATE_TRACK_ID",
        "PRIVATE_TITLE",
        "PRIVATE_ARTIST",
        "PRIVATE_ALBUM",
        "PRIVATE_PLAYLIST",
        "PRIVATE_PLAYLIST_ID",
        "PRIVATE_ERROR",
        "PRIVATE_PROVIDER_REFERENCE",
        "PRIVATE_PROJECT_ROOT",
        "PRIVATE_DATA_DIR",
        "PRIVATE_DATABASE",
        "PRIVATE_DOWNLOADS",
        "PRIVATE_CONFIG",
        "PRIVATE_STATUS_FILE",
    }
    try:
        path = write_app_status(
            db,
            {"download_folder": str(tmp_path / "downloads")},
            {
                "playback": {
                    "currently_playing": "PRIVATE_TRACK_ID",
                    "current_title": "PRIVATE_TITLE",
                    "current_artist": "PRIVATE_ARTIST",
                    "current_album": "PRIVATE_ALBUM",
                    "is_playing": True,
                    "queue_count": 3,
                    "provider_reference": "PRIVATE_PROVIDER_REFERENCE",
                },
                "sync": {
                    "last_sync_status": "failed",
                    "last_sync_playlist_title": "PRIVATE_PLAYLIST",
                    "last_sync_playlist_id": "PRIVATE_PLAYLIST_ID",
                    "last_sync_error": "PRIVATE_ERROR",
                },
                "paths": {
                    "project_root": "PRIVATE_PROJECT_ROOT",
                    "data_dir": "PRIVATE_DATA_DIR",
                    "database": "PRIVATE_DATABASE",
                    "downloads": "PRIVATE_DOWNLOADS",
                    "config": "PRIVATE_CONFIG",
                    "status_file": "PRIVATE_STATUS_FILE",
                },
            },
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert all(
            payload["playback"][field] is None
            for field in (
                "currently_playing",
                "current_title",
                "current_artist",
                "current_album",
            )
        )
        assert payload["playback"]["is_playing"] is True
        assert payload["playback"]["queue_count"] == 3
        assert "provider_reference" not in payload["playback"]
        assert payload["sync"]["last_sync_status"] == "failed"
        assert payload["sync"]["last_sync_playlist_title"] is None
        assert payload["sync"]["last_sync_playlist_id"] is None
        assert payload["sync"]["last_sync_error"] is None
        assert all(
            payload["paths"][field] is None
            for field in (
                "project_root",
                "data_dir",
                "database",
                "downloads",
                "config",
                "status_file",
            )
        )
        assert payload["paths"]["path_resolution_source"] == "synthetic"
        serialized = json.dumps(payload, sort_keys=True)
        assert not any(value in serialized for value in private_values)
    finally:
        db.close()


def test_source_migration_proof_isolated_idempotent_and_aggregate_only(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "outside-repository"
    parent.mkdir()
    with _schema7_database_runtime():
        result = source_proof.run_source_migration_proof(temporary_parent=parent)

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["counts"]["automatic_schema7_backup_count"] == 1
    assert result["counts"]["network_attempt_count"] == 0
    assert list(parent.iterdir()) == []
    encoded = json.dumps(result, sort_keys=True)
    assert "Fixture" not in encoded
    assert str(tmp_path) not in encoded
    assert result["raw_library_values_emitted"] is False


def test_baseline_backup_and_clone_dry_run_leave_schema6_source_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-live"
    data, database, baseline = _schema6_runtime(root)
    original_hash = acceptance.sha256_file(database)
    original_stat = database.stat().st_mtime_ns
    backup = data / "backups" / "explicit.sqlite3"

    evidence = live_gate.create_verified_backup(
        database=database,
        backup_dir=backup.parent,
        backup_path=backup,
        baseline=baseline,
    )
    with _schema7_database_runtime():
        dry_run = live_gate.clone_dry_run(
            project_root=root,
            data_dir=data,
            database=database,
            baseline=baseline,
            temporary_parent=tmp_path,
        )

    assert evidence["verified"] is True
    assert evidence["path_emitted"] is False
    assert dry_run["ok"] is True
    assert dry_run["source_database_unchanged"] is True
    assert dry_run["network_attempt_count"] == 0
    assert dry_run["safe_album_group_count"] > 0
    assert dry_run["safe_artist_merge_count"] == 1
    assert dry_run["review_scanned_count"] == 3
    assert acceptance.sha256_file(database) == original_hash
    assert database.stat().st_mtime_ns == original_stat
    encoded = json.dumps({"baseline": baseline, "dry_run": dry_run}, sort_keys=True)
    assert "Fixture" not in encoded
    assert str(tmp_path) not in encoded


def test_release_context_schema6_boundary_and_additive_guard_are_exact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-live"
    data, database, baseline = _schema6_runtime(root)
    before_guard = baseline["database"]["track_release_context_stable_guard"]

    with acceptance.readonly(database, immutable=False) as connection:
        before_columns = set(acceptance.columns(connection, "track_release_context"))
    assert before_columns.isdisjoint(acceptance.RELEASE_CONTEXT_ADDITIVE_COLUMNS)
    assert before_guard["count"] == 1

    _migrate_source_mode(root, database)
    migrated = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=7,
    )
    with acceptance.readonly(database, immutable=False) as connection:
        after_columns = set(acceptance.columns(connection, "track_release_context"))
    assert acceptance.RELEASE_CONTEXT_ADDITIVE_COLUMNS <= after_columns
    assert migrated["database"]["track_release_context_stable_guard"] == before_guard

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE track_release_context SET release_title='forbidden-change' "
            "WHERE track_id=(SELECT MIN(track_id) FROM track_release_context)"
        )
    changed = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=7,
    )
    assert changed["database"]["track_release_context_stable_guard"] != before_guard


def test_verified_backup_fails_closed_on_logical_tampering(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-live"
    data, database, baseline = _schema6_runtime(root)
    backup = data / "backups" / "explicit.sqlite3"
    acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO app_meta(key,value) VALUES('synthetic-tamper','1')"
        )

    with pytest.raises(acceptance.AcceptanceFailure):
        acceptance.verify_sqlite_backup(backup=backup, baseline=baseline)


def test_post_launch_gate_passes_then_detects_cover_path_mutation(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-live"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    data, database, baseline = _schema6_runtime(root)
    dry_run = _dry_run(root, data, database, baseline)
    backup = data / "backups" / "explicit.sqlite3"
    acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )
    _migrate_source_mode(root, database)
    network_report = _network_report(root)

    passed = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert passed["ok"] is True
    assert all(passed["checks"].values())
    assert passed["checks"]["track_release_context_stable_values_exact"] is True
    assert passed["counts"]["review_counts_before"] == {"review": 3}
    assert passed["counts"]["review_counts_after"] == {
        "applied_with_gaps": 2,
        "source_fallback": 1,
    }

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tracks SET cover_path='synthetic-forbidden-change' "
            "WHERE id=(SELECT MIN(id) FROM tracks)"
        )
    failed = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert failed["ok"] is False
    assert failed["checks"]["track_cover_paths_exact"] is False


def test_post_launch_gate_allows_only_exact_repair_marker_app_meta_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-app-meta"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    data, database, baseline = _schema6_runtime(root)
    dry_run = _dry_run(root, data, database, baseline)
    backup = data / "backups" / "explicit.sqlite3"
    acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )
    _migrate_source_mode(root, database)
    network_report = _network_report(root)

    def verify() -> dict[str, object]:
        return live_gate.verify_migration(
            baseline=baseline,
            dry_run=dry_run,
            project_root=root,
            data_dir=data,
            database=database,
            backup_path=backup,
            network_report=network_report,
        )

    assert verify()["ok"] is True
    with sqlite3.connect(database) as connection:
        baseline_row = tuple(
            connection.execute(
                "SELECT key,value FROM app_meta WHERE key<>? ORDER BY key LIMIT 1",
                (live_gate.METADATA_ACCEPTANCE_REPAIR_MARKER,),
            ).fetchone()
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_meta(key,value) VALUES('synthetic-unexpected','1')"
        )
    added = verify()
    assert added["ok"] is False
    assert added["checks"]["app_meta_baseline_rows_exact"] is False
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM app_meta WHERE key='synthetic-unexpected'")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE app_meta SET value='synthetic-tamper' WHERE key=?",
            (baseline_row[0],),
        )
    changed = verify()
    assert changed["ok"] is False
    assert changed["checks"]["app_meta_baseline_rows_exact"] is False
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE app_meta SET value=? WHERE key=?",
            (baseline_row[1], baseline_row[0]),
        )

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM app_meta WHERE key=?", (baseline_row[0],))
    removed = verify()
    assert removed["ok"] is False
    assert removed["checks"]["app_meta_baseline_rows_exact"] is False
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?)",
            baseline_row,
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE app_meta SET value='2' WHERE key=?",
            (live_gate.METADATA_ACCEPTANCE_REPAIR_MARKER,),
        )
    wrong_marker = verify()
    assert wrong_marker["ok"] is False
    assert wrong_marker["checks"]["batch10_5_repair_marker_exact"] is False


def test_live_gate_accepts_exact_credit_dedupe_with_semantic_subset_preserved(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-live"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    data, database, _initial = _schema6_runtime(root)
    _insert_exact_duplicate_credit_evidence(database)
    baseline = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=6,
    )
    before_credit_count = int(
        baseline["database"]["tables"]["track_artist_credits"]["count"]
    )
    dry_run = _dry_run(root, data, database, baseline)
    backup = data / "backups" / "explicit.sqlite3"
    acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )

    _migrate_source_mode(root, database)
    network_report = _network_report(root)
    result = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    with sqlite3.connect(database) as connection:
        after_credit_count = int(
            connection.execute("SELECT COUNT(*) FROM track_artist_credits").fetchone()[0]
        )

    assert after_credit_count == before_credit_count - 1
    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["checks"]["preservation_counts_exact"] is True
    assert result["checks"]["artist_credit_semantic_evidence_preserved"] is True


def test_credit_count_exception_cannot_hide_lost_semantic_evidence() -> None:
    assert live_gate._preservation_counts_match(
        {"tracks": 4, "track_artist_credits": 5, "track_metadata_history": 2},
        {"tracks": 4, "track_artist_credits": 4, "track_metadata_history": 3},
    )
    assert not live_gate._verify_credit_semantic_subset(
        ["credit-a", "credit-b"], ["credit-a"]
    )


def test_live_gate_binds_memberships_to_disposable_clone_result(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-live"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    data, database, baseline = _schema6_runtime(root)
    dry_run = _dry_run(root, data, database, baseline)
    backup = data / "backups" / "explicit.sqlite3"
    acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )
    _migrate_source_mode(root, database)
    network_report = _network_report(root)

    with sqlite3.connect(database) as connection:
        membership = connection.execute(
            "SELECT track_id,canonical_album_id FROM track_album_memberships ORDER BY track_id LIMIT 1"
        ).fetchone()
        replacement = connection.execute(
            "SELECT id FROM canonical_albums WHERE id!=? ORDER BY id LIMIT 1",
            (membership[1],),
        ).fetchone()
        assert replacement is not None
        connection.execute(
            "UPDATE track_album_memberships SET canonical_album_id=? WHERE track_id=?",
            (replacement[0], membership[0]),
        )

    result = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert result["ok"] is False
    assert result["checks"]["canonical_membership_count_expected"] is True
    assert result["checks"]["one_membership_per_track"] is True
    assert result["checks"]["dry_run_canonical_album_memberships_exact"] is False


def test_acceptance_network_guard_blocks_without_recording_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.gettempdir()) / (
        "MusicVault_Batch10_3_NetworkTest_" + uuid.uuid4().hex
    )
    report = root / "network-report.json"
    root.mkdir()
    monkeypatch.setenv(acceptance_network.NO_SECRETS_ENVIRONMENT, "1")
    monkeypatch.setenv(acceptance_network.NO_NETWORK_ENVIRONMENT, "1")
    monkeypatch.setenv(acceptance_network.NETWORK_REPORT_ENVIRONMENT, str(report))
    guard = acceptance_network.install_acceptance_network_guard()
    assert guard is not None
    try:
        guard.finalize()
        assert acceptance.verify_acceptance_network_report(report)["verified"] is True
        with pytest.raises(acceptance_network.AcceptanceNetworkBlocked):
            socket.create_connection(("synthetic.invalid", 443), timeout=0.01)
        guard.finalize()
        with pytest.raises(
            acceptance.AcceptanceFailure, match="provider_or_network_access_observed"
        ):
            acceptance.verify_acceptance_network_report(report)
        serialized = report.read_text(encoding="utf-8")
        assert "synthetic.invalid" not in serialized
        assert "443" not in serialized
    finally:
        guard.restore()
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize(
    ("field", "value", "failure"),
    (
        ("finalized", False, "policy"),
        ("guard_installed", False, "policy"),
        ("attempt_count", 1, "provider_or_network"),
        ("provider_factory_invocation_count", 1, "provider_or_network"),
        ("provider_task_dispatch_count", 1, "provider_or_network"),
        ("destination", "synthetic.invalid", "shape"),
    ),
)
def test_acceptance_network_report_tampering_fails_closed(
    tmp_path: Path, field: str, value: object, failure: str
) -> None:
    report = _network_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload[field] = value
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(acceptance.AcceptanceFailure, match=failure):
        acceptance.verify_acceptance_network_report(report)


def test_live_gate_detects_cover_portrait_and_manual_field_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-live"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    data, database, _initial = _schema6_runtime(root)
    with sqlite3.connect(database) as connection:
        cover_path = Path(
            str(
                connection.execute(
                    "SELECT cover_path FROM tracks "
                    "WHERE NULLIF(TRIM(cover_path),'') IS NOT NULL ORDER BY id LIMIT 1"
                ).fetchone()[0]
            )
        )
        connection.execute(
            "UPDATE track_metadata_fields SET provenance='manual',confidence=100,"
            "is_manual=1,is_locked=1 WHERE track_id=(SELECT MIN(track_id) "
            "FROM track_metadata_fields WHERE field_name='title') AND field_name='title'"
        )
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    original_cover = b"cover-one"
    replacement_cover = b"cover-two"
    cover_path.write_bytes(original_cover)
    cover_stat = cover_path.stat()
    baseline = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=6,
    )
    assert str(root) not in json.dumps(baseline, sort_keys=True)
    dry_run = _dry_run(root, data, database, baseline)
    backup = data / "backups" / "explicit.sqlite3"
    acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )
    _migrate_source_mode(root, database)
    network_report = _network_report(root)

    passed = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert passed["ok"] is True

    cover_path.write_bytes(replacement_cover)
    os.utime(
        cover_path,
        ns=(cover_stat.st_atime_ns, cover_stat.st_mtime_ns),
    )
    cover_failed = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert cover_failed["ok"] is False
    assert cover_failed["checks"]["track_cover_paths_exact"] is True
    assert cover_failed["checks"]["referenced_cover_files_unchanged"] is False

    cover_path.write_bytes(original_cover)
    os.utime(
        cover_path,
        ns=(cover_stat.st_atime_ns, cover_stat.st_mtime_ns),
    )
    portrait = data / "artist_images" / "files" / "downloaded.fixture"
    portrait.parent.mkdir(parents=True)
    portrait.write_bytes(b"private portrait payload")
    portrait_failed = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert portrait_failed["ok"] is False
    assert portrait_failed["checks"]["referenced_cover_files_unchanged"] is True
    assert portrait_failed["checks"]["artist_image_tree_unchanged"] is False

    shutil.rmtree(data / "artist_images")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE track_metadata_fields SET value=value || '-tampered' "
            "WHERE is_manual=1 OR is_locked=1"
        )
    metadata_failed = live_gate.verify_migration(
        baseline=baseline,
        dry_run=dry_run,
        project_root=root,
        data_dir=data,
        database=database,
        backup_path=backup,
        network_report=network_report,
    )
    assert metadata_failed["ok"] is False
    assert metadata_failed["checks"]["metadata_field_keys_not_removed"] is True
    assert metadata_failed["checks"]["non_artist_version_metadata_fields_exact"] is True
    assert metadata_failed["checks"]["manual_and_locked_metadata_fields_exact"] is False


def _packaged_runtime() -> Path:
    return Path(tempfile.gettempdir()) / (
        packaged.RUNTIME_PREFIX + "pytest_" + uuid.uuid4().hex
    )


def _packaged_review_manifest(runtime: Path) -> Path:
    output = runtime.with_name(runtime.name + packaged.UI_REVIEW_OUTPUT_SUFFIX)
    output.mkdir()
    screenshot = output / "1280x720_batch10_3_smoke.png"
    screenshot.write_bytes(b"synthetic packaged UI capture")
    behaviors = {name: True for name in packaged.UI_REVIEW_REQUIRED_TRUE_FIELDS}
    behaviors.update(
        {
            "packaged_process": True,
            "schema_version": 7,
            "network_attempt_count": 0,
            "canonical_album_count": 4,
            "artist_card_count": 5,
        }
    )
    manifest = output / "manifest.json"
    acceptance.atomic_write_json(
        manifest,
        {
            "status": "complete",
            "runtime": "isolated_temporary",
            "requested_capture_count": 1,
            "capture_count": 1,
            "runtime_checks": {"batch10_3_behaviors": behaviors},
            "captures": [
                {
                    "scene": "batch10_3_smoke",
                    "file": screenshot.name,
                    "sha256": acceptance.sha256_file(screenshot),
                    "browser_metrics": {
                        "kind": "artists",
                        "model_rows": 5,
                        "per_item_widget_count": 0,
                        "synthetic_provider_active": True,
                        "public_provider_call_count": 0,
                    },
                }
            ],
        },
    )
    return manifest


def test_packaged_prepare_and_source_mode_verify_are_isolated_and_fail_closed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "synthetic-project"
    executable = project / "dist" / "MusicVault" / "MusicVault.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable marker")
    runtime = _packaged_runtime()
    try:
        with _schema7_database_runtime():
            manifest = packaged.prepare(runtime, project)
        assert manifest["baseline"]["database"]["health"]["schema_version"] == 6
        assert (
            manifest["baseline"]["database"]["protected_tables"]["app_meta"]["count"]
            == 1
        )
        assert manifest["explicit_backup"]["verified"] is True
        assert manifest["execution_policy"]["no_secrets"] is True
        assert not (runtime / "data" / "youtube_api_key.txt").exists()
        assert not (runtime / "data" / "discogs_token.txt").exists()

        _migrate_source_mode(runtime, runtime / packaged.DATABASE_RELATIVE_PATH)
        network_report = _network_report(runtime)
        review_manifest = _packaged_review_manifest(runtime)
        result = packaged.verify(
            runtime,
            project,
            manifest,
            graceful_close_confirmed=True,
            network_report=network_report,
            review_manifest=review_manifest,
        )
        assert result["ok"] is True
        assert all(result["checks"].values())
        with pytest.raises(packaged.SmokeFailure, match="close_not_confirmed"):
            packaged.verify(
                runtime,
                project,
                manifest,
                graceful_close_confirmed=False,
                network_report=network_report,
                review_manifest=review_manifest,
            )
        attempted_report = _network_report(runtime, attempts=1)
        with pytest.raises(packaged.SmokeFailure, match="network_guard"):
            packaged.verify(
                runtime,
                project,
                manifest,
                graceful_close_confirmed=True,
                network_report=attempted_report,
                review_manifest=review_manifest,
            )
    finally:
        gc.collect()
        shutil.rmtree(runtime, ignore_errors=True)
        shutil.rmtree(
            runtime.with_name(runtime.name + packaged.UI_REVIEW_OUTPUT_SUFFIX),
            ignore_errors=True,
        )


def test_live_execution_guard_requires_ack_no_secret_mode_and_closed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_gate, "_music_vault_running", lambda: False)
    with pytest.raises(live_gate.GateFailure, match="acknowledgement"):
        live_gate._execution_guard("wrong")
    monkeypatch.delenv(acceptance.NO_SECRETS_ENVIRONMENT, raising=False)
    with pytest.raises(acceptance.AcceptanceFailure, match="no_secrets"):
        live_gate._execution_guard(live_gate.ACKNOWLEDGEMENT)
    monkeypatch.setenv(acceptance.NO_SECRETS_ENVIRONMENT, "1")
    monkeypatch.setattr(live_gate, "_music_vault_running", lambda: True)
    with pytest.raises(live_gate.GateFailure, match="process_running"):
        live_gate._execution_guard(live_gate.ACKNOWLEDGEMENT)


def test_powershell_wrappers_use_project_python_no_secret_mode_and_safe_cleanup() -> None:
    dev = PROJECT_ROOT / "tools" / "dev"
    source = (dev / "run_batch10_3_source_migration_proof.ps1").read_text(encoding="utf-8")
    live = (dev / "verify_batch10_3_live_migration.ps1").read_text(encoding="utf-8")
    frozen = (dev / "run_batch10_3_packaged_migration_smoke.ps1").read_text(encoding="utf-8")
    controlled = (dev / "run_batch10_3_controlled_live_startup.ps1").read_text(
        encoding="utf-8"
    )
    for wrapper in (source, live, frozen, controlled):
        assert ".venv\\Scripts\\python.exe" in wrapper
        assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper
    for wrapper in (frozen, controlled):
        assert "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK" in wrapper
        assert "MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT" in wrapper
        assert "Get-NetTCPConnection" in wrapper
        assert "CloseMainWindow" in wrapper
        assert "PostCloseOwned" in wrapper
        assert "Stop-Process" not in wrapper
    assert "Get-NetTCPConnection" in frozen
    assert "StartsWith($ResolvedTemp" in frozen
    assert "Remove-Item -LiteralPath $ResolvedRuntime -Recurse -Force" in frozen
    assert "batch10.3-live-schema-6-to-7" in live
    assert "launch-preflight" in controlled
    assert "--dry-run" in controlled
    assert "--network-report" in controlled


def test_live_gate_requires_every_durable_release_identity_index() -> None:
    assert {
        "idx_canonical_albums_provider_family",
        "idx_release_context_mb_release_group",
        "idx_release_context_provider_family",
    } <= acceptance.REQUIRED_V7_INDEXES
