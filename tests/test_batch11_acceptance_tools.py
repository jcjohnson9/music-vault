from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from music_vault.core.youtube_sync import YouTubeSyncConfig
from tools.dev import batch11_acceptance as acceptance
from tools.dev import run_batch11_quality_e2e as e2e


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _acceptance_root(tmp_path: Path, suffix: str = "Test") -> Path:
    return tmp_path / f"{acceptance.TEMP_PREFIX}{suffix}"


def test_temporary_acceptance_root_is_bounded_and_prefixed(tmp_path: Path) -> None:
    root = _acceptance_root(tmp_path)
    assert acceptance.safe_temporary_root(root, must_exist=False) == root.resolve()
    root.mkdir()
    assert acceptance.safe_temporary_root(root, must_exist=True) == root.resolve()

    with pytest.raises(acceptance.AcceptanceFailure, match="unsafe_temporary"):
        acceptance.safe_temporary_root(PROJECT_ROOT)
    with pytest.raises(acceptance.AcceptanceFailure, match="unsafe_temporary"):
        acceptance.safe_temporary_root(tmp_path / "unowned-batch11-root")


def test_config_guard_allows_only_the_two_quality_migration_keys(tmp_path: Path) -> None:
    config = tmp_path / "music_vault_config.json"
    config.write_text(
        json.dumps({"volume_percent": 23, "audio_quality": "320"}),
        encoding="utf-8",
    )
    before = acceptance.config_guard(config)
    config.write_text(
        json.dumps(
            {
                "volume_percent": 23,
                "audio_quality": "320",
                "download_quality_profile": "best_original",
                "compatibility_mp3_bitrate_kbps": 320,
            }
        ),
        encoding="utf-8",
    )
    migrated = acceptance.config_guard(config)
    assert migrated["stable_digest"] == before["stable_digest"]
    assert migrated["full_digest"] != before["full_digest"]
    assert migrated["profile_present"] is True
    assert migrated["compatibility_bitrate_present"] is True

    config.write_text(
        json.dumps(
            {
                "volume_percent": 99,
                "audio_quality": "320",
                "download_quality_profile": "best_original",
                "compatibility_mp3_bitrate_kbps": 320,
            }
        ),
        encoding="utf-8",
    )
    assert acceptance.config_guard(config)["stable_digest"] != before["stable_digest"]


def test_media_guard_is_exact_but_emits_no_path_or_media_bytes(tmp_path: Path) -> None:
    media = tmp_path / "private-name.opus"
    media.write_bytes(b"synthetic media bytes")
    database = tmp_path / "library.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE tracks(id INTEGER PRIMARY KEY,path TEXT NOT NULL)")
        connection.execute("INSERT INTO tracks(path) VALUES (?)", (str(media),))
        connection.commit()
        guard = acceptance.media_guard(connection)
    finally:
        connection.close()

    rendered = json.dumps(guard, sort_keys=True)
    assert "private-name" not in rendered
    assert "synthetic media bytes" not in rendered
    assert guard["missing_media_count"] == 0
    assert guard["unique_media_count"] == 1

    media.write_bytes(b"synthetic media bytes changed")
    connection = sqlite3.connect(database)
    try:
        changed = acceptance.media_guard(connection)
    finally:
        connection.close()
    assert changed["path_digest"] == guard["path_digest"]
    assert changed["media_digest"] != guard["media_digest"]


def test_conservative_quality_verifier_rejects_invented_source_facts(tmp_path: Path) -> None:
    database = tmp_path / "quality.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE tracks(id INTEGER PRIMARY KEY,path TEXT,source_kind TEXT)"
        )
        connection.execute(
            "CREATE TABLE track_media_quality("
            "track_id INTEGER PRIMARY KEY,acquisition_profile TEXT,"
            "source_format_id TEXT,source_extension TEXT,source_container TEXT,"
            "source_codec TEXT,source_bitrate_kbps INTEGER,"
            "source_sample_rate_hz INTEGER,source_channels INTEGER,"
            "source_filesize_bytes INTEGER,stored_container TEXT,"
            "stored_codec TEXT,stored_bitrate_kbps INTEGER,"
            "stored_sample_rate_hz INTEGER,stored_channels INTEGER,"
            "stored_filesize_bytes INTEGER,inspected_at TEXT,"
            "transformation_kind TEXT,inspection_state TEXT)"
        )
        connection.execute(
            "INSERT INTO tracks VALUES (1,'synthetic.mp3','youtube')"
        )
        connection.execute(
            "INSERT INTO track_media_quality VALUES "
            "(1,'legacy_youtube_mp3',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,"
            "NULL,NULL,NULL,NULL,NULL,NULL,NULL,'legacy_inferred_transcode',"
            "'legacy_inferred')"
        )
        assert acceptance._verify_quality_rows(connection)["quality_row_count"] == 1
        connection.execute(
            "UPDATE track_media_quality SET source_codec='opus' WHERE track_id=1"
        )
        with pytest.raises(acceptance.AcceptanceFailure, match="conservative"):
            acceptance._verify_quality_rows(connection)
    finally:
        connection.close()


def test_network_report_requires_zero_provider_and_socket_activity(tmp_path: Path) -> None:
    report = tmp_path / "network.json"
    payload = {
        "guard_installed": True,
        "outbound_blocked": True,
        "attempt_count": 0,
        "provider_factory_invocation_count": 0,
        "provider_task_dispatch_count": 0,
        "finalized": True,
        "request_details_recorded": False,
        "credential_contents_read": False,
    }
    report.write_text(json.dumps(payload), encoding="utf-8")
    assert acceptance.verify_network_report(report)["attempt_count"] == 0
    payload["provider_task_dispatch_count"] = 1
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(acceptance.AcceptanceFailure, match="network_evidence"):
        acceptance.verify_network_report(report)


def test_live_prepare_creates_verified_schema7_backup_without_raw_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "synthetic-project"
    data = project / "data"
    data.mkdir(parents=True)
    media = data / "synthetic.mp3"
    media.write_bytes(b"synthetic")
    database = data / "music_vault.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE tracks(id INTEGER PRIMARY KEY,path TEXT NOT NULL)")
        connection.execute("INSERT INTO tracks(path) VALUES (?)", (str(media),))
        connection.execute(f"PRAGMA user_version={acceptance.PRE_SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()
    (data / "music_vault_config.json").write_text(
        json.dumps({"volume_percent": 23, "download_folder": str(data)}),
        encoding="utf-8",
    )
    evidence = _acceptance_root(tmp_path, "LivePrepare")
    evidence.mkdir()
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")

    manifest = acceptance.prepare_live_manifest(
        project_root=project,
        evidence_root=evidence,
    )
    backup = data / "backups" / manifest["explicit_backup"]["filename"]
    assert backup.is_file()
    assert manifest["database_before"]["schema_version"] == 7
    assert manifest["credential_contents_read"] is False
    rendered = json.dumps(manifest, sort_keys=True)
    assert str(media) not in rendered
    assert str(project) not in rendered


def test_combined_summary_marks_live_stage_as_required_before_completion() -> None:
    summary = acceptance.combine_summaries(
        {
            "status": "passed",
            "stage": "isolated_packaged_quality_scenario",
            "schema_version": acceptance.POST_SCHEMA_VERSION,
        },
        None,
    )
    assert summary["status"] == "pending_live_migration"
    assert summary["stage_b"] == {
        "status": "pending_live_migration",
        "requires_explicit_live_flag": True,
    }
    assert summary["network_or_secret_access"] is False


def test_combined_summary_requires_both_exact_stage_contracts() -> None:
    stage_a = {
        "status": "passed",
        "stage": "isolated_packaged_quality_scenario",
        "schema_version": acceptance.POST_SCHEMA_VERSION,
    }
    stage_b = {
        "status": "passed",
        "stage": "controlled_live_schema_7_to_8",
        "schema_before": acceptance.PRE_SCHEMA_VERSION,
        "schema_after": acceptance.POST_SCHEMA_VERSION,
    }
    assert acceptance.combine_summaries(stage_a, stage_b)["status"] == "passed"
    with pytest.raises(acceptance.AcceptanceFailure, match="stage_a"):
        acceptance.combine_summaries({"status": "passed"}, stage_b)
    with pytest.raises(acceptance.AcceptanceFailure, match="stage_b"):
        acceptance.combine_summaries(stage_a, {"status": "passed"})


def test_stage_a_format_fixtures_use_dynamic_non_numeric_format_ids(tmp_path: Path) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"synthetic")
    formats = e2e._source_formats("opus", source)
    assert len(formats) == 1
    assert formats[0]["format_id"].startswith("synthetic-ranked-")
    assert not str(formats[0]["format_id"]).isdigit()
    assert formats[0]["acodec"] == "opus"
    assert formats[0]["vcodec"] == "none"


def test_party_review_media_outlasts_the_bounded_control_exercise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert e2e.PARTY_REVIEW_DURATION_SECONDS >= 15.0
    assert e2e.PARTY_REVIEW_DURATION_SECONDS > e2e.SYNTHETIC_DURATION_SECONDS
    expected_pcm_bytes = int(e2e.PARTY_REVIEW_DURATION_SECONDS * 48_000 * 1 * 2)
    assert expected_pcm_bytes <= 5 * 1024 * 1024

    generated: list[dict[str, object]] = []

    def fake_generate(_ffmpeg: Path, destination: Path, **kwargs: object) -> None:
        destination.write_bytes(b"synthetic-wave")
        generated.append({"destination": destination, **kwargs})

    class FakeDB:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def upsert_track(self, path: Path, **kwargs: object) -> int:
            self.rows.append({"path": path, **kwargs})
            return len(self.rows)

    monkeypatch.setattr(e2e, "_generate_sine", fake_generate)
    runtime = tmp_path / "runtime"
    (runtime / "data").mkdir(parents=True)
    database = FakeDB()

    result = e2e._create_party_fixture(runtime, database, Path("ffmpeg"))

    assert result == {"track_count": 3, "fixture_valid": True}
    assert len(generated) == 3
    assert {
        item["duration_seconds"] for item in generated
    } == {e2e.PARTY_REVIEW_DURATION_SECONDS}
    assert {item["channels"] for item in generated} == {1}
    assert {
        row["duration_seconds"] for row in database.rows
    } == {e2e.PARTY_REVIEW_DURATION_SECONDS}


def test_fake_syncer_uses_production_worker_under_no_secret_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
    output = tmp_path / "downloads"
    archive = tmp_path / "runtime" / "archive.txt"
    config = YouTubeSyncConfig(
        playlist_url=(
            "https://www.youtube.com/playlist?list=" + e2e.SOURCE_IDS["a"]
        ),
        output_dir=output,
        archive_file=archive,
    )
    api = e2e.FakeYouTubeDataAPI(
        {e2e.SOURCE_IDS["a"]: (e2e.VIDEO_IDS["opus"],)}
    )

    syncer = e2e.FakeSyncer(config, api=api)

    assert e2e.FakeSyncer.sync is e2e.AuthorizedYouTubePlaylistSyncer.sync
    assert syncer.config is config
    assert syncer.api is api
    assert syncer._ffmpeg_discovery is None
    assert output.is_dir()
    assert archive.parent.is_dir()
    playlist_id, _title, entries = syncer._extract_playlist_entries_via_api()
    assert playlist_id == e2e.SOURCE_IDS["a"]
    assert [entry["id"] for entry in entries] == [e2e.VIDEO_IDS["opus"]]


def test_stage_a_secret_filename_audit_is_narrow_and_fail_closed(tmp_path: Path) -> None:
    assert e2e._is_secret_named_path(tmp_path / "synthetic_api_key.txt") is True
    assert e2e._is_secret_named_path(tmp_path / "provider_token.txt") is True
    assert e2e._is_secret_named_path(tmp_path / "music_vault_config.json") is False
    assert e2e._is_secret_named_path(42) is False


def test_powershell_gate_is_offline_packaged_and_live_opt_in_only() -> None:
    wrapper = (PROJECT_ROOT / "tools" / "dev" / "run_batch11_quality_e2e.ps1").read_text(
        encoding="utf-8"
    )
    helper = (PROJECT_ROOT / "tools" / "dev" / "run_batch11_quality_e2e.py").read_text(
        encoding="utf-8"
    )
    assert "[switch]$RunLiveMigration" in wrapper
    assert 'ValidateSet("batch11-live-schema7-to-8")' in wrapper
    assert "$RunLiveMigration -and" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK" in wrapper
    prepare_environment = wrapper.split(
        "# The source-side synthetic preparation", 1
    )[1].split("& $Python -B $Tool prepare-stage-a", 1)[0]
    assert '$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"' in prepare_environment
    assert '$env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"' in prepare_environment
    assert (
        '$env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = '
        '$StageAPreparationNetworkReport'
    ) in prepare_environment
    assert "Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK" not in prepare_environment
    assert "Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT" not in prepare_environment
    assert "MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT" in wrapper
    assert "MUSIC_VAULT_UI_REVIEW" in wrapper
    assert "dist\\MusicVault\\MusicVault.exe" in wrapper
    assert "Get-NetTCPConnection" in wrapper
    assert "Test-MusicVaultSourceProcess" in wrapper
    assert "$OwnedProcessIsLive" in wrapper
    assert "CloseMainWindow" in wrapper
    assert "-WindowStyle Hidden" in wrapper
    assert "Stop-Process" not in wrapper
    assert "taskkill" not in wrapper.casefold()
    assert "requests.get" not in helper
    assert "YoutubeDL(" not in helper
    assert "FakeYoutubeDLBoundary" in helper
    assert "class FakeSyncer(AuthorizedYouTubePlaylistSyncer)" in helper
    assert e2e.PREPARATION_NETWORK_REPORT_NAME in wrapper
    fake_syncer_source = helper.split("class FakeSyncer", 1)[1].split(
        "def _wait_for", 1
    )[0]
    assert "super().__init__" not in fake_syncer_source
    assert "secret_file_open_attempt_count" in helper
    assert "youtube_api_key.txt" not in helper
    assert "discogs_token.txt" not in helper
