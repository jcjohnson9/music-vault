from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from music_vault.core import acceptance_network
from music_vault.core import db as db_module
from music_vault.core import paths
from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_images import (
    ArtistIdentity,
    ArtistImageCache,
    ArtistImageStatus,
    SyntheticArtistImageProvider,
    create_artist_image_provider,
)
from music_vault.core.runtime_policy import RuntimePolicy
from music_vault.app import MusicVaultWindow
from tools.dev import batch10_3_acceptance as acceptance
from tools.dev import batch10_4_acceptance as batch104
from tools.dev import run_batch10_3_source_migration_proof as source_proof
from tools.dev import run_batch10_4_packaged_quiescence_smoke as packaged_quiescence


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_runtime_paths(monkeypatch: pytest.MonkeyPatch):
    previous = os.environ.get("MUSIC_VAULT_PROJECT_ROOT")
    yield
    if previous is None:
        monkeypatch.delenv("MUSIC_VAULT_PROJECT_ROOT", raising=False)
    else:
        monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", previous)
    paths._resolved_project_root.cache_clear()


@contextmanager
def _schema7_database_runtime():
    """Reproduce the historical schema-7 runtime without Batch 11 tables."""
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


def _migrate_schema7(root: Path, database: Path) -> None:
    previous = os.environ.get("MUSIC_VAULT_PROJECT_ROOT")
    os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(root)
    paths._resolved_project_root.cache_clear()
    try:
        with _schema7_database_runtime():
            db = MusicVaultDB(database, backup_dir=root / "data" / "backups")
            db.close()
    finally:
        if previous is None:
            os.environ.pop("MUSIC_VAULT_PROJECT_ROOT", None)
        else:
            os.environ["MUSIC_VAULT_PROJECT_ROOT"] = previous
        paths._resolved_project_root.cache_clear()


def _schema7_runtime(root: Path) -> tuple[Path, Path]:
    data = root / "data"
    database = data / "music_vault.sqlite3"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic Batch 10.4 marker\n", encoding="utf-8")
    data.mkdir(exist_ok=True)
    source_proof._create_synthetic_schema6(database, data / "backups", root)
    _migrate_schema7(root, database)
    return data, database


def _cache(root: Path, *, synthetic: bool = False) -> ArtistImageCache:
    cache = ArtistImageCache(root)
    identity = ArtistIdentity.from_display_name("Synthetic Acceptance Artist")
    cache.store(SyntheticArtistImageProvider().resolve(identity))
    if not synthetic:
        payload = json.loads(cache.index_path.read_text(encoding="utf-8"))
        next(iter(payload["entries"].values()))["image_provider"] = "Wikimedia Commons"
        cache.index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return cache


def _safe_status(path: Path, *, reason: str = "acceptance_no_network") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "app": "Music Vault",
                "app_version": "1.1.0",
                "health": {"api_ready": False, "ffmpeg_ready": True, "ok": False},
                "discogs_ready": False,
                "provider_work_deferred": True,
                "provider_work_defer_reason": reason,
                "paths": {
                    "project_root": None,
                    "data_dir": None,
                    "database": None,
                    "downloads": None,
                    "config": None,
                    "status_file": None,
                    "path_resolution_source": "environment",
                },
                "playback": {
                    "currently_playing": None,
                    "current_title": None,
                    "current_artist": None,
                    "current_album": None,
                    "is_playing": False,
                    "queue_count": 0,
                },
                "sync": {
                    "last_sync_playlist_title": None,
                    "last_sync_playlist_id": None,
                    "last_sync_error": None,
                    "last_sync_failures": [],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _network_report(path: Path, *, attempts: int = 0) -> None:
    acceptance.atomic_write_json(
        path,
        {
            "schema_version": acceptance.NETWORK_REPORT_FORMAT_VERSION,
            "guard_installed": True,
            "outbound_blocked": True,
            "attempt_count": attempts,
            "provider_factory_invocation_count": 0,
            "provider_task_dispatch_count": 0,
            "finalized": True,
            "request_details_recorded": False,
            "credential_contents_read": False,
        },
    )


def test_artist_cache_audit_is_strict_aggregate_only(tmp_path: Path) -> None:
    cache = _cache(tmp_path / "artist_images", synthetic=True)
    expected_files = [item for item in cache.root.rglob("*") if item.is_file()]
    expected_bytes = sum(item.stat().st_size for item in expected_files)

    result = batch104.audit_artist_cache(
        cache.root,
        expected_file_count=len(expected_files),
        expected_total_bytes=expected_bytes,
        allow_synthetic=True,
    )

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["counts"]["tree_file_count"] == 2
    assert result["counts"]["physical_image_count"] == 1
    assert result["counts"]["provider_counts"]["synthetic"] == 1
    assert result["raw_identity_values_emitted"] is False
    assert result["urls_emitted"] is False
    serialized = json.dumps(result, sort_keys=True)
    assert "Synthetic Acceptance Artist" not in serialized


def test_artist_cache_audit_detects_traversal_secret_fields_and_partial_payload(
    tmp_path: Path,
) -> None:
    cache = _cache(tmp_path / "artist_images", synthetic=True)
    payload = json.loads(cache.index_path.read_text(encoding="utf-8"))
    record = next(iter(payload["entries"].values()))
    record["cache_file"] = "../outside.jpg"
    record["api_token"] = "synthetic-forbidden-value"
    cache.index_path.write_text(json.dumps(payload), encoding="utf-8")
    (cache.root / ".partial-download.tmp").write_bytes(b"MZ")

    result = batch104.audit_artist_cache(cache.root, allow_synthetic=True)

    assert result["ok"] is False
    assert result["issues"]["path_violation"] > 0
    assert result["issues"]["secret_field"] > 0
    assert result["issues"]["temporary_file"] > 0
    assert result["issues"]["unexpected_payload"] > 0
    assert "synthetic-forbidden-value" not in json.dumps(result)


def test_artist_cache_audit_rejects_duplicate_json_keys_without_identity_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artist_images"
    root.mkdir()
    (root / "index.json").write_text(
        '{"schema_version":1,"entries":{},"entries":{}}', encoding="utf-8"
    )

    result = batch104.audit_artist_cache(root)

    assert result["ok"] is False
    assert result["issues"]["index_invalid"] == 1
    assert result["raw_identity_values_emitted"] is False


def test_schema7_backup_uses_sqlite_backup_api_and_verifies_logical_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    data, database = _schema7_runtime(root)
    cache = _cache(data / "artist_images")
    baseline = batch104.capture_quiescence_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_cache_file_count=2,
        expected_cache_total_bytes=sum(
            item.stat().st_size for item in cache.root.rglob("*") if item.is_file()
        ),
    )
    track_count = baseline["database_state"]["database"]["tables"]["tracks"][
        "count"
    ]
    backup = data / "backups" / "batch10_4.sqlite3"

    result = batch104.create_schema7_backup(
        database=database,
        backup=backup,
        baseline=baseline,
        expected_counts={"tracks": track_count},
    )

    assert result["verified"] is True
    assert result["schema_version"] == 7
    assert result["table_count_checks"] == {"tracks": True}
    assert backup.is_file()
    with sqlite3.connect(backup) as connection:
        connection.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('tamper','1')")
    with pytest.raises(acceptance.AcceptanceFailure, match="backup_logical_mismatch"):
        acceptance.verify_sqlite_backup(
            backup=backup,
            baseline=baseline["database_state"],
            expected_schema=7,
        )


def test_live_quiescence_verifier_allows_only_safe_status_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    data, database = _schema7_runtime(root)
    cache = _cache(data / "artist_images")
    (root / "dist" / "MusicVault").mkdir(parents=True)
    status_path = data / "music_vault_status.json"
    _safe_status(status_path, reason="migration_startup")
    cache_bytes = sum(
        item.stat().st_size for item in cache.root.rglob("*") if item.is_file()
    )
    baseline = batch104.capture_quiescence_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_cache_file_count=2,
        expected_cache_total_bytes=cache_bytes,
    )
    manifest = {
        "manifest_format_version": batch104.MANIFEST_FORMAT_VERSION,
        "baseline": baseline,
        "expected_cache_file_count": 2,
        "expected_cache_total_bytes": cache_bytes,
    }
    _safe_status(status_path, reason="acceptance_no_network")
    network_report = tmp_path / "network.json"
    _network_report(network_report)
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")

    result = batch104.verify_live_quiescence(
        project_root=root,
        manifest=manifest,
        network_report=network_report,
        graceful_close_confirmed=True,
    )

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["app_status"]["provider_work_defer_reason"] == "acceptance_no_network"
    assert result["credential_contents_read"] is False


def test_live_quiescence_verifier_detects_database_or_provider_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    data, database = _schema7_runtime(root)
    cache = _cache(data / "artist_images")
    (root / "dist" / "MusicVault").mkdir(parents=True)
    status_path = data / "music_vault_status.json"
    _safe_status(status_path)
    cache_bytes = sum(
        item.stat().st_size for item in cache.root.rglob("*") if item.is_file()
    )
    baseline = batch104.capture_quiescence_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_cache_file_count=2,
        expected_cache_total_bytes=cache_bytes,
    )
    manifest = {
        "manifest_format_version": batch104.MANIFEST_FORMAT_VERSION,
        "baseline": baseline,
        "expected_cache_file_count": 2,
        "expected_cache_total_bytes": cache_bytes,
    }
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('changed','1')")
    _safe_status(status_path, reason="acceptance_no_secrets")
    network_report = tmp_path / "network.json"
    _network_report(network_report, attempts=1)
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")

    with pytest.raises(
        acceptance.AcceptanceFailure, match="provider_or_network_access_observed"
    ):
        batch104.verify_live_quiescence(
            project_root=root,
            manifest=manifest,
            network_report=network_report,
            graceful_close_confirmed=True,
        )


def test_app_status_validator_fails_closed_on_readiness_identity_or_unsafe_reason(
    tmp_path: Path,
) -> None:
    status = tmp_path / "status.json"
    _safe_status(status)
    passed = batch104.validate_safe_app_status(status)
    assert passed["verified"] is True

    payload = json.loads(status.read_text(encoding="utf-8"))
    payload["health"]["api_ready"] = True
    payload["playback"]["current_title"] = "forbidden identity"
    payload["provider_work_defer_reason"] = "artist/provider/query"
    status.write_text(json.dumps(payload), encoding="utf-8")
    failed = batch104.validate_safe_app_status(status)
    assert failed["verified"] is False
    assert failed["checks"]["api_ready_false"] is False
    assert failed["checks"]["playback_identity_suppressed"] is False
    assert failed["checks"]["defer_reason_safe"] is False
    assert "forbidden identity" not in json.dumps(failed)


def test_prepare_live_acceptance_requires_temp_scope_and_creates_fresh_backup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    data, database = _schema7_runtime(root)
    cache = _cache(data / "artist_images")
    cache_bytes = sum(
        item.stat().st_size for item in cache.root.rglob("*") if item.is_file()
    )
    with sqlite3.connect(database) as connection:
        track_count = int(connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    evidence = Path(tempfile.gettempdir()) / (
        batch104.EVIDENCE_PREFIX + "pytest_" + uuid.uuid4().hex
    )
    try:
        manifest = batch104.prepare_live_acceptance(
            project_root=root,
            evidence_dir=evidence,
            acknowledgement=batch104.LIVE_ACKNOWLEDGEMENT,
            expected_counts={"tracks": track_count},
            expected_cache_file_count=2,
            expected_cache_total_bytes=cache_bytes,
        )
        backup = data / "backups" / manifest["backup_name"]
        assert manifest["backup"]["verified"] is True
        assert manifest["backup"]["schema_version"] == 7
        assert backup.is_file()
        assert manifest["baseline"]["database_file"]["database"]["sha256"] == acceptance.sha256_file(
            database
        )
    finally:
        shutil.rmtree(evidence, ignore_errors=True)

    with pytest.raises(batch104.Batch104Failure, match="evidence_scope_invalid"):
        batch104.prepare_live_acceptance(
            project_root=root,
            evidence_dir=root / "data" / "MusicVault_Batch10_4_forbidden",
            acknowledgement=batch104.LIVE_ACKNOWLEDGEMENT,
        )


def test_controlled_wrapper_is_no_secret_no_network_graceful_and_non_destructive() -> None:
    source = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_4_controlled_live_startup.ps1"
    ).read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in source
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in source
    assert "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK" in source
    assert "MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT" in source
    assert "Get-NetTCPConnection" in source
    assert "CloseMainWindow" in source
    assert "PostCloseOwned" in source
    assert "Stop-Process" not in source
    assert "Remove-Item -LiteralPath" not in source
    assert "Music Vault v1.1.0 Development" in source
    assert "tracks=304" in source
    assert "canonical_albums=167" in source
    assert '"playlist_track_origins=7"' in source
    assert '"playlist_origins=7"' not in source
    assert "--expected-cache-file-count" in source
    assert '"226"' in source

    cache_audit = (
        PROJECT_ROOT / "tools" / "dev" / "audit_batch10_4_artist_cache.ps1"
    ).read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in cache_audit
    assert "audit-cache" in cache_audit
    assert "--expected-file-count 226" in cache_audit
    assert "--expected-total-bytes 30791281" in cache_audit
    assert "Get-Content" not in cache_audit
    assert "Remove-Item" not in cache_audit


def test_batch10_3_backup_api_remains_backward_compatible(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data = root / "data"
    database = data / "music_vault.sqlite3"
    data.mkdir(parents=True)
    source_proof._create_synthetic_schema6(database, data / "backups", root)
    (root / "music_vault").mkdir()
    (root / "run.py").write_text("# marker\n", encoding="utf-8")
    baseline = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=6,
    )
    result = acceptance.create_verified_sqlite_backup(
        database=database,
        backup=data / "backups" / "legacy.sqlite3",
        baseline=baseline,
    )
    assert result["schema_version"] == 6


def test_batch10_4_network_report_prefix_and_zero_provider_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_root = Path(tempfile.gettempdir()) / (
        "MusicVault_Batch10_4_NetworkTest_" + uuid.uuid4().hex
    )
    report = report_root / "network.json"
    report_root.mkdir()
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT", str(report))
    monkeypatch.setenv("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "synthetic")
    guard = acceptance_network.install_acceptance_network_guard()
    assert guard is not None
    try:
        guard.finalize()
        evidence = acceptance.verify_acceptance_network_report(report)
        assert evidence["attempt_count"] == 0
        assert evidence["provider_factory_invocation_count"] == 0
        assert evidence["provider_task_dispatch_count"] == 0

        provider = create_artist_image_provider(
            runtime_policy=RuntimePolicy(
                acceptance_no_secrets=True,
                acceptance_no_network=True,
            )
        )
        assert provider.resolve(
            ArtistIdentity.from_display_name("Aggregate Only")
        ).status is ArtistImageStatus.DISABLED
        guard.finalize()
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["attempt_count"] == 0
        assert payload["provider_factory_invocation_count"] == 1
        assert payload["provider_task_dispatch_count"] == 0
        serialized = json.dumps(payload, sort_keys=True)
        assert "Aggregate Only" not in serialized
        assert "synthetic" not in serialized
    finally:
        guard.restore()
        shutil.rmtree(report_root, ignore_errors=True)


def test_acceptance_provider_counters_fail_closed_without_recording_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_root = Path(tempfile.gettempdir()) / (
        "MusicVault_Batch10_4_CounterTest_" + uuid.uuid4().hex
    )
    report = report_root / "network.json"
    report_root.mkdir()
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT", str(report))
    guard = acceptance_network.install_acceptance_network_guard()
    assert guard is not None
    try:
        acceptance_network.record_provider_factory_invocation()
        acceptance_network.record_provider_task_dispatch()
        guard.finalize()
        with pytest.raises(
            acceptance.AcceptanceFailure, match="provider_or_network_access_observed"
        ):
            acceptance.verify_acceptance_network_report(report)
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["provider_factory_invocation_count"] == 1
        assert payload["provider_task_dispatch_count"] == 1
        assert set(payload) == {
            "schema_version",
            "guard_installed",
            "outbound_blocked",
            "attempt_count",
            "provider_factory_invocation_count",
            "provider_task_dispatch_count",
            "finalized",
            "request_details_recorded",
            "credential_contents_read",
        }
    finally:
        guard.restore()
        shutil.rmtree(report_root, ignore_errors=True)


def test_blocked_metadata_factory_entry_is_counted_before_policy_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_root = Path(tempfile.gettempdir()) / (
        "MusicVault_Batch10_4_MetadataFactoryTest_" + uuid.uuid4().hex
    )
    report = report_root / "network.json"
    report_root.mkdir()
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT", str(report))
    guard = acceptance_network.install_acceptance_network_guard()
    assert guard is not None
    policy = RuntimePolicy(
        acceptance_no_secrets=True,
        acceptance_no_network=True,
    )
    holder = type(
        "BlockedFactoryHost",
        (),
        {
            "_current_runtime_policy": lambda self: policy,
            "_provider_work_allowed": lambda self: False,
        },
    )()
    try:
        with pytest.raises(RuntimeError, match="deferred"):
            MusicVaultWindow._create_discogs_metadata_provider(
                holder, "synthetic-unread-token"
            )
        with pytest.raises(RuntimeError, match="deferred"):
            MusicVaultWindow._create_musicbrainz_metadata_provider(holder)
        guard.finalize()
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["provider_factory_invocation_count"] == 2
        assert payload["provider_task_dispatch_count"] == 0
        assert payload["attempt_count"] == 0
        assert "synthetic-unread-token" not in json.dumps(payload)
    finally:
        guard.restore()
        shutil.rmtree(report_root, ignore_errors=True)


def test_acceptance_report_rejects_uncontrolled_temp_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = Path(tempfile.gettempdir()) / ("uncontrolled_" + uuid.uuid4().hex) / "n.json"
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT", str(report))
    with pytest.raises(RuntimeError, match="controlled prefix"):
        acceptance_network.install_acceptance_network_guard()


def test_packaged_quiescence_prepare_is_schema6_synthetic_and_aggregate_only(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    executable = project / "dist" / "MusicVault" / "MusicVault.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable marker")
    runtime = Path(tempfile.gettempdir()) / (
        packaged_quiescence.RUNTIME_PREFIX + "pytest_" + uuid.uuid4().hex
    )
    review_output = runtime.with_name(
        runtime.name + packaged_quiescence.SECOND_REVIEW_OUTPUT_SUFFIX
    )
    try:
        with _schema7_database_runtime():
            manifest = packaged_quiescence.prepare(runtime, project)
        assert manifest["manifest_format_version"] == 1
        assert manifest["baseline"]["database"]["health"]["schema_version"] == 6
        assert manifest["explicit_backup"]["verified"] is True
        assert manifest["initial_artist_cache"]["ok"] is True
        assert manifest["initial_artist_cache"]["counts"]["index_entry_count"] == 0
        assert not (runtime / "data" / "youtube_api_key.txt").exists()
        assert not (runtime / "data" / "discogs_token.txt").exists()
        assert manifest["raw_library_values_emitted"] is False
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
        shutil.rmtree(review_output, ignore_errors=True)


def test_packaged_quiescence_wrapper_owns_two_launches_and_cleans_only_on_success() -> None:
    source = (
        PROJECT_ROOT
        / "tools"
        / "dev"
        / "run_batch10_4_packaged_quiescence_smoke.ps1"
    ).read_text(encoding="utf-8")
    assert "verify-first" in source and "verify-second" in source
    assert 'MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"' in source
    assert 'MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"' in source
    assert 'MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = "synthetic"' in source
    assert "Get-NetTCPConnection" in source
    assert "CloseMainWindow" in source and "PostCloseOwned" in source
    assert "-WindowStyle Minimized" in source
    assert "-WindowStyle Hidden" not in source
    assert "Stop-Process" not in source
    assert "$Succeeded -and $FirstClosed -and $SecondClosed" in source
    assert "Remove-SafeBatch104TempTree" in source
