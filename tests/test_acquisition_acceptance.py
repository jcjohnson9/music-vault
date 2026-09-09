"""The acquisition acceptance gate must never become a personal-data entrypoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.dev import verify_acquisition as gate


@pytest.fixture
def acceptance_root(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / (gate.TEMP_PREFIX + "a" * 32)


def test_only_new_direct_temporary_root_is_accepted(acceptance_root):
    assert gate.safe_new_root(acceptance_root) == acceptance_root.resolve()
    acceptance_root.mkdir()
    with pytest.raises(gate.AcceptanceFailure, match="unsafe_acceptance_root"):
        gate.safe_new_root(acceptance_root)


@pytest.mark.parametrize("name", [
    "data", "MusicVault_Acquisition_", "MusicVault_Acquisition_" + "g" * 32,
    "MusicVault_Acquisition_" + "a" * 31, "MusicVault_Acquisition_" + "a" * 33,
])
def test_personal_or_unvalidated_root_name_is_refused(tmp_path, acceptance_root, name):
    with pytest.raises(gate.AcceptanceFailure, match="unsafe_acceptance_root"):
        gate.safe_new_root(tmp_path / name)


def test_nested_temp_root_is_refused(tmp_path, acceptance_root):
    with pytest.raises(gate.AcceptanceFailure, match="unsafe_acceptance_root"):
        gate.safe_new_root(tmp_path / "nested" / acceptance_root.name)


@pytest.mark.parametrize("filename", ["youtube_api_key.txt", "DISCOGS_TOKEN.TXT"])
def test_guard_blocks_credential_opens_without_reading_content(acceptance_root, filename):
    guard = gate.RuntimeGuard(acceptance_root, network=True)
    with pytest.raises(gate.AcceptanceFailure, match="credential_open_blocked"):
        guard("open", (str(acceptance_root / filename), "rb", 0))
    assert guard.summary()["credential_open_attempts"] == 1


@pytest.mark.parametrize("event,args", [
    ("open", ("outside.txt", "wb", 0)),
    ("open", ("outside.txt", None, os.O_CREAT | os.O_RDWR)),
    ("os.mkdir", ("outside", 0o777, -1)),
    ("os.remove", ("outside.txt", -1)),
    ("os.rename", ("outside.txt", "other.txt", -1, -1)),
])
def test_guard_blocks_outside_mutation(acceptance_root, event, args):
    guard = gate.RuntimeGuard(acceptance_root, network=False)
    with pytest.raises(gate.AcceptanceFailure, match="outside_runtime_write_blocked"):
        guard(event, args)
    assert guard.outside_write_attempts == 1


def test_guard_blocks_outside_database(acceptance_root):
    guard = gate.RuntimeGuard(acceptance_root, network=False)
    with pytest.raises(gate.AcceptanceFailure, match="outside_runtime_database_blocked"):
        guard("sqlite3.connect", (str(acceptance_root.parent / "personal.sqlite3"),))
    assert guard.outside_database_attempts == 1


def test_guard_allows_owned_writes_and_subprocess_null_device(acceptance_root):
    guard = gate.RuntimeGuard(acceptance_root, network=False)
    guard("open", (str(acceptance_root / "signal.flac"), "wb", 0))
    guard("open", (os.devnull, None, os.O_RDWR))
    guard("sqlite3.connect", (str(acceptance_root / "data/music_vault.sqlite3"),))
    assert not any(guard.summary().values())


@pytest.mark.parametrize("event,args", [
    ("socket.getaddrinfo", ("www.youtube.com", 443, 0, 0, 0)),
    ("socket.connect", (object(), ("127.0.0.1", 443))),
    ("socket.sendto", (object(), ("127.0.0.1", 443))),
])
def test_offline_guard_blocks_network_before_transport(acceptance_root, event, args):
    guard = gate.RuntimeGuard(acceptance_root, network=False)
    with pytest.raises(gate.AcceptanceFailure, match="offline_network_blocked"):
        guard(event, args)
    assert guard.blocked_network_attempts == 1


@pytest.mark.parametrize("host", [
    "api.discogs.com", "musicbrainz.org", "lrclib.net", "example.com",
    "www.youtube.com.example.com", "evilyoutube.com", "127.0.0.1",
])
def test_network_mode_rejects_metadata_provider_and_non_fixture_dns(acceptance_root, host):
    guard = gate.RuntimeGuard(acceptance_root, network=True)
    with pytest.raises(gate.AcceptanceFailure, match="non_fixture_network_blocked"):
        guard("socket.getaddrinfo", (host, 443, 0, 0, 0))


@pytest.mark.parametrize("host", [
    "www.youtube.com", "rr1.example.googlevideo.com", "i.ytimg.com",
    "youtubei.googleapis.com", "WWW.YouTube.COM.",
])
def test_network_mode_permits_anonymous_extractor_hosts(acceptance_root, host):
    guard = gate.RuntimeGuard(acceptance_root, network=True)
    guard("socket.getaddrinfo", (host, 443, 0, 0, 0))
    assert guard.blocked_network_attempts == 0


def test_isolation_controls_paths_environment_and_restores_caller(acceptance_root, monkeypatch):
    from music_vault.core import paths

    acceptance_root.mkdir()
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", "ignored-previous-value")
    monkeypatch.setenv("YOUTUBE_API_KEY", "synthetic-sentinel")
    monkeypatch.setenv("DISCOGS_TOKEN", "synthetic-sentinel")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic.invalid")
    previous_environment = dict(os.environ)
    previous_directory = Path.cwd()
    with gate.isolated_runtime(acceptance_root, network=False) as guard:
        assert paths.project_root() == acceptance_root
        assert paths.database_path().is_relative_to(acceptance_root)
        assert paths.app_status_path().is_relative_to(acceptance_root)
        assert paths.metadata_job_backups_dir().is_relative_to(acceptance_root)
        assert Path.cwd() == acceptance_root
        assert os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] == "1"
        assert os.environ["MUSIC_VAULT_ACCEPTANCE_NO_NETWORK"] == "1"
        assert "YOUTUBE_API_KEY" not in os.environ
        assert "DISCOGS_TOKEN" not in os.environ
        assert "HTTPS_PROXY" not in os.environ
        assert guard.active
    assert not guard.active
    assert dict(os.environ) == previous_environment
    assert Path.cwd() == previous_directory


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Unit runner seams only; the real offline pipeline is checked below."""
    from music_vault.core import acquisition_runtime, ffmpeg

    readiness = acquisition_runtime.AcquisitionReadiness(True, "fixture", "fixture")
    monkeypatch.setattr(acquisition_runtime, "acquisition_readiness", lambda: readiness)
    pair = SimpleNamespace(ready=True, bin_dir=None, yt_dlp_location=None)
    monkeypatch.setattr(ffmpeg, "discover_ffmpeg", lambda **kwargs: pair)

    def fixture(root, _ffmpeg):
        (root / "temporary-fixture.flac").write_bytes(b"synthetic")
        return SimpleNamespace(path=root / "temporary-fixture.flac")

    monkeypatch.setattr(gate, "_offline_item", fixture)
    monkeypatch.setattr(gate, "verify_import_boundary", lambda *args: {"unit_fixture": True})
    return pair


def test_success_leaves_only_aggregate_evidence(acceptance_root, stub_pipeline):
    report = gate.run_acceptance(acceptance_root, mode="offline")
    assert report["passed"] is True
    assert report["runtime_payload_removed"] is True
    assert not any(report["guard"].values())
    assert list(acceptance_root.iterdir()) == [acceptance_root / gate.EVIDENCE_NAME]
    stored = json.loads((acceptance_root / gate.EVIDENCE_NAME).read_text(encoding="utf-8"))
    assert stored == report
    assert str(acceptance_root) not in json.dumps(stored)


def test_failure_is_sanitized_and_payload_still_removed(acceptance_root, stub_pipeline, monkeypatch):
    def fail(*args):
        raise ValueError("private title https://example.invalid/?credential=synthetic")

    monkeypatch.setattr(gate, "verify_import_boundary", fail)
    report = gate.run_acceptance(acceptance_root, mode="offline")
    assert report["passed"] is False
    assert report["failed_phase"] == "verification_import"
    assert report["failure_code"] == "verification_import_failed"
    assert report["runtime_payload_removed"] is True
    assert "private title" not in json.dumps(report)
    assert "credential=synthetic" not in json.dumps(report)


def test_fixed_public_fixture_and_safe_production_failure_survive_reporting(
    acceptance_root, stub_pipeline, monkeypatch,
):
    from music_vault.core import youtube_sync
    from music_vault.core.acquisition_diagnostics import (
        AcquisitionDiagnostic, AcquisitionError, AcquisitionReason, AcquisitionStage,
    )

    calls = []

    def fail(video_id, destination, **kwargs):
        calls.append((video_id, destination, kwargs))
        raise AcquisitionError(AcquisitionDiagnostic(
            AcquisitionStage.TRANSFER, AcquisitionReason.HTTP_FORBIDDEN, 403,
        ))

    monkeypatch.setattr(youtube_sync, "acquire_public_audio", fail)
    report = gate.run_acceptance(acceptance_root, mode="network")
    assert calls[0][0] == "YE7VzlLtp-4"
    assert calls[0][1] == acceptance_root / "data/youtube_downloads"
    assert calls[0][2]["max_download_bytes"] == gate.MAX_DOWNLOAD_BYTES
    assert calls[0][2]["timeout_seconds"] == gate.MAX_ACQUISITION_SECONDS
    assert report["passed"] is False
    assert report["acquisition_diagnostic"]["stage"] == "media_transfer"
    assert report["acquisition_diagnostic"]["http_status"] == 403
    assert report["runtime_payload_removed"] is True


def test_cleanup_failure_never_reports_a_pass(acceptance_root, stub_pipeline, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(gate.shutil, "rmtree", fail)
    report = gate.run_acceptance(acceptance_root, mode="offline")
    assert report["passed"] is False
    assert report["runtime_payload_removed"] is False
    assert report["cleanup_failure"] == "owned_runtime_cleanup_failed"


def test_cli_requires_explicit_mode_and_root():
    with pytest.raises(SystemExit) as failure:
        gate.main([])
    assert failure.value.code == 2


def test_cli_rejects_arbitrary_urls_and_live_database_options():
    with pytest.raises(SystemExit) as failure:
        gate.main(["--url", "https://example.invalid", "--database", "personal.sqlite3"])
    assert failure.value.code == 2


def test_actual_offline_fixture_import_integrity_and_cleanup(acceptance_root, monkeypatch):
    from music_vault.core import acquisition_runtime, ffmpeg

    # CI need not install optional FFmpeg CLI tools for unit tests. The
    # official packaged acceptance gate is mandatory and cannot skip them.
    if not ffmpeg.discover_ffmpeg().ready:
        pytest.skip("No installed FFmpeg/ffprobe pair for real offline acceptance")
    if not acquisition_runtime.acquisition_readiness().ready:
        pytest.skip("No complete acquisition stack for real offline acceptance")
    report = gate.run_acceptance(acceptance_root, mode="offline")
    assert report["passed"] is True, report.get("failure_code")
    pipeline = report["pipeline"]
    assert pipeline["track_count"] == pipeline["second_import_track_count"] == 1
    assert pipeline["canonical_identity_count"] == pipeline["quality_row_count"] == 1
    assert pipeline["sqlite_integrity_ok"] and pipeline["foreign_keys_ok"]
    assert pipeline["truncated_media_rejected"] and pipeline["archive_unchanged"]
    assert pipeline["media_and_tags_unchanged_by_import"]
    assert pipeline["stored_codec"] == "flac"
    assert pipeline["duration_seconds"] == 1.0
    assert report["runtime_payload_removed"]


def test_frozen_dispatch_stays_before_gui_import(monkeypatch):
    import run

    calls = []
    monkeypatch.setattr(run.sys, "argv", [
        "MusicVault.exe", "--verify-acquisition", "--mode", "offline",
        "--runtime-root", "synthetic-root",
    ])
    monkeypatch.setattr(gate, "main", lambda args: calls.append(args) or 17)
    assert run.main() == 17
    assert calls == [["--mode", "offline", "--runtime-root", "synthetic-root"]]
