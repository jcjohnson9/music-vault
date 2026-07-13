from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core.desktop_shortcut import create_or_update_desktop_shortcut
from music_vault.core.ffmpeg import FFmpegDiscoveryResult, discover_ffmpeg
from music_vault.core.youtube_sync import AuthorizedYouTubePlaylistSyncer, YouTubeSyncConfig
from music_vault.app import MusicVaultWindow


def _pair(root: Path, *, nested: bool = False) -> Path:
    directory = root / "synthetic-version" / "bin" if nested else root
    directory.mkdir(parents=True)
    (directory / "ffmpeg.exe").write_bytes(b"fake")
    (directory / "ffprobe.exe").write_bytes(b"fake")
    return directory


class _SuccessfulProbe:
    def __init__(self):
        self.calls = []

    def __call__(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="version", stderr="")


def test_configured_pair_resolves_and_probes_with_bounded_safe_commands(tmp_path):
    bin_dir = _pair(tmp_path / "ffmpeg")
    runner = _SuccessfulProbe()
    result = discover_ffmpeg(bin_dir, runner=runner, timeout=99)
    assert result.ready and result.source == "configured"
    assert result.bin_dir == bin_dir.resolve()
    assert result.yt_dlp_location == str(bin_dir.resolve())
    assert len(runner.calls) == 2
    for arguments, kwargs in runner.calls:
        assert arguments[1:] == ["-version"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 5.0


def test_missing_ffprobe_is_a_clear_failure(tmp_path):
    location = tmp_path / "ffmpeg"
    location.mkdir()
    (location / "ffmpeg.exe").write_bytes(b"fake")
    result = discover_ffmpeg(location, probe=False)
    assert not result.ready
    assert result.error_code == "incomplete_pair"
    assert "both ffmpeg.exe and ffprobe.exe" in result.error


def test_probe_timeout_is_bounded_and_sanitized(tmp_path):
    bin_dir = _pair(tmp_path / "ffmpeg")

    def timeout_runner(*_args, **kwargs):
        raise subprocess.TimeoutExpired("private command", kwargs["timeout"])

    result = discover_ffmpeg(bin_dir, runner=timeout_runner, timeout=0.2)
    assert not result.ready
    assert result.error_code == "ffmpeg_probe_timeout"
    assert "private command" not in result.error


def test_path_portable_and_legacy_sources_resolve_complete_pairs(tmp_path):
    path_pair = _pair(tmp_path / "path")
    result = discover_ffmpeg(probe=False, path_value=str(path_pair), legacy_root=tmp_path / "none")
    assert result.ready and result.source == "path"

    portable_pair = _pair(tmp_path / "portable tools", nested=True)
    result = discover_ffmpeg(
        portable_tools_location=portable_pair.parents[1],
        probe=False,
        path_value="",
        legacy_root=tmp_path / "none",
    )
    assert result.ready and result.source == "portable_tools"

    legacy_pair = _pair(tmp_path / "legacy", nested=True)
    result = discover_ffmpeg(
        probe=False,
        path_value="",
        legacy_root=legacy_pair.parents[1],
    )
    assert result.ready and result.source == "legacy_tools"


def test_absent_optional_portable_tools_falls_through_to_path(tmp_path):
    path_pair = _pair(tmp_path / "path")
    result = discover_ffmpeg(
        portable_tools_location=tmp_path / "portable" / "tools",
        probe=False,
        path_value=str(path_pair),
        legacy_root=tmp_path / "none",
    )
    assert result.ready and result.source == "path"


def test_sync_passes_configured_discovery_location_to_ytdlp_once(tmp_path, monkeypatch):
    selected = tmp_path / "ffmpeg"
    bin_dir = selected / "bin"
    calls = []

    def fake_discovery(configured):
        calls.append(configured)
        return FFmpegDiscoveryResult(
            True,
            "configured",
            bin_dir,
            bin_dir / "ffmpeg.exe",
            bin_dir / "ffprobe.exe",
        )

    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        fake_discovery,
    )
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=synthetic",
        tmp_path / "downloads",
        tmp_path / "archive.txt",
        ffmpeg_location=selected,
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    assert syncer._ffmpeg_location() == str(bin_dir)
    assert syncer._ffmpeg_location() == str(bin_dir)
    assert calls == [selected]


def test_invalid_configured_ffmpeg_fails_before_api_or_ytdlp(
    tmp_path, monkeypatch
):
    selected = tmp_path / "invalid ffmpeg"
    discovery_calls = []
    api_calls = []
    ytdlp_calls = []

    def fake_discovery(configured):
        discovery_calls.append(configured)
        return FFmpegDiscoveryResult(
            False,
            "configured",
            error="The selected location is incomplete.",
            error_code="incomplete_pair",
        )

    def fail_if_api_runs(_self):
        api_calls.append(True)
        raise AssertionError("API enumeration must not run")

    class UnexpectedYoutubeDL:
        def __init__(self, *_args, **_kwargs):
            ytdlp_calls.append(True)
            raise AssertionError("yt-dlp must not run")

    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg", fake_discovery
    )
    monkeypatch.setattr(
        AuthorizedYouTubePlaylistSyncer,
        "_extract_playlist_entries_via_api",
        fail_if_api_runs,
    )
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.yt_dlp.YoutubeDL", UnexpectedYoutubeDL
    )
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=synthetic",
        tmp_path / "downloads",
        tmp_path / "archive.txt",
        ffmpeg_location=selected,
    )

    result = AuthorizedYouTubePlaylistSyncer(config).sync()

    assert result.status == "failed"
    assert result.failures
    assert "Configured FFmpeg is not ready" in result.failures[0].reason
    assert discovery_calls == [selected]
    assert api_calls == []
    assert ytdlp_calls == []


def test_app_ffmpeg_readiness_is_cached_until_explicit_invalidation(monkeypatch):
    calls = []
    bin_dir = Path("synthetic-ffmpeg").resolve()

    def fake_discovery(**kwargs):
        calls.append(kwargs)
        return FFmpegDiscoveryResult(
            True,
            "path",
            bin_dir,
            bin_dir / "ffmpeg.exe",
            bin_dir / "ffprobe.exe",
        )

    window = SimpleNamespace(config={}, _last_ffmpeg_discovery=None)
    monkeypatch.setattr("music_vault.app.discover_ffmpeg", fake_discovery)
    monkeypatch.setattr("music_vault.app.portable_root", lambda: None)

    assert (
        MusicVaultWindow.discover_ffmpeg_readiness(window).yt_dlp_location
        == str(bin_dir)
    )
    assert (
        MusicVaultWindow.discover_ffmpeg_readiness(window).yt_dlp_location
        == str(bin_dir)
    )
    assert len(calls) == 1

    MusicVaultWindow.invalidate_ffmpeg_discovery(window)
    assert (
        MusicVaultWindow.discover_ffmpeg_readiness(window).yt_dlp_location
        == str(bin_dir)
    )
    assert len(calls) == 2


def test_app_preview_probe_does_not_replace_cached_active_setting(monkeypatch):
    active_dir = Path("active-ffmpeg").resolve()
    preview_dir = Path("preview-ffmpeg").resolve()
    calls = []

    def fake_discovery(**kwargs):
        calls.append(kwargs.get("configured_location"))
        bin_dir = preview_dir if kwargs.get("configured_location") else active_dir
        return FFmpegDiscoveryResult(
            True,
            "configured" if kwargs.get("configured_location") else "path",
            bin_dir,
            bin_dir / "ffmpeg.exe",
            bin_dir / "ffprobe.exe",
        )

    window = SimpleNamespace(config={}, _last_ffmpeg_discovery=None)
    monkeypatch.setattr("music_vault.app.discover_ffmpeg", fake_discovery)
    monkeypatch.setattr("music_vault.app.portable_root", lambda: None)

    active = MusicVaultWindow.discover_ffmpeg_readiness(window)
    preview = MusicVaultWindow.discover_ffmpeg_readiness(window, "preview")
    cached = MusicVaultWindow.discover_ffmpeg_readiness(window)

    assert active.bin_dir == active_dir
    assert preview.bin_dir == preview_dir
    assert cached is active
    assert calls == [None, "preview"]


def test_shortcut_helper_uses_target_working_directory_icon_and_no_shell(tmp_path):
    portable = tmp_path / "Portable App"
    portable.mkdir()
    executable = portable / "MusicVault.exe"
    executable.write_bytes(b"exe")
    icon = portable / "music_vault.ico"
    icon.write_bytes(b"ico")
    desktop = tmp_path / "Redirected Desktop"
    calls = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        payload = {
            "status": "created",
            "shortcut_path": str(desktop / "Music Vault.lnk"),
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    result = create_or_update_desktop_shortcut(
        executable,
        portable,
        desktop_dir=desktop,
        icon_path=icon,
        runner=runner,
        powershell_executable="synthetic-powershell.exe",
    )
    assert result.succeeded and result.status == "created"
    assert result.target_path == executable.resolve()
    assert result.working_directory == portable.resolve()
    assert result.icon_path == icon.resolve()
    arguments, kwargs = calls[0]
    assert kwargs["env"]["MUSIC_VAULT_SHORTCUT_EXECUTABLE"] == str(executable.resolve())
    assert kwargs["env"]["MUSIC_VAULT_SHORTCUT_WORKING_DIRECTORY"] == str(portable.resolve())
    assert kwargs["env"]["MUSIC_VAULT_SHORTCUT_DESKTOP"] == str(desktop.resolve())
    assert kwargs["env"]["MUSIC_VAULT_SHORTCUT_ICON"] == str(icon.resolve())
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 15


def test_unrelated_shortcut_conflict_is_not_reported_as_success(tmp_path):
    portable = tmp_path / "portable"
    portable.mkdir()
    executable = portable / "MusicVault.exe"
    executable.write_bytes(b"exe")
    desktop = tmp_path / "Desktop"

    def runner(_arguments, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "conflict",
                    "shortcut_path": str(desktop / "Music Vault.lnk"),
                }
            ),
            stderr="",
        )

    result = create_or_update_desktop_shortcut(
        executable,
        portable,
        desktop_dir=desktop,
        runner=runner,
        powershell_executable="synthetic-powershell.exe",
    )
    assert result.status == "conflict"
    assert not result.succeeded
    assert "another application location" in result.error


@pytest.mark.skipif(os.name != "nt", reason="Windows shortcut integration")
def test_shortcut_integration_uses_redirected_desktop_and_refuses_retarget(tmp_path):
    portable = tmp_path / "Portable"
    portable.mkdir()
    first_executable = portable / "MusicVault.exe"
    second_executable = portable / "OtherMusicVault.exe"
    first_executable.write_bytes(b"first")
    second_executable.write_bytes(b"second")
    desktop = tmp_path / "Synthetic Desktop"

    first = create_or_update_desktop_shortcut(
        first_executable,
        portable,
        desktop_dir=desktop,
    )
    assert first.status == "created"
    assert (desktop / "Music Vault.lnk").is_file()

    conflict = create_or_update_desktop_shortcut(
        second_executable,
        portable,
        desktop_dir=desktop,
    )
    assert conflict.status == "conflict"

    unchanged = create_or_update_desktop_shortcut(
        first_executable,
        portable,
        desktop_dir=desktop,
    )
    assert unchanged.status == "unchanged"
