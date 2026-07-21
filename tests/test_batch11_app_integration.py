from __future__ import annotations

import json
import os
from types import SimpleNamespace

from music_vault.app import MusicVaultWindow
from music_vault.core.audio_quality_config import (
    BEST_ORIGINAL_PROFILE,
    DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
)


def test_existing_config_quality_migration_is_persisted_exactly_once(tmp_path) -> None:
    config_file = tmp_path / "music_vault_config.json"
    harness = SimpleNamespace()
    harness.default_config = lambda: MusicVaultWindow.default_config(harness)
    harness.config_file_path = lambda: config_file
    legacy = MusicVaultWindow.default_config(harness)
    legacy.pop("download_quality_profile")
    legacy.pop("compatibility_mp3_bitrate_kbps")
    legacy["audio_quality"] = "256"
    config_file.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = MusicVaultWindow.load_config(harness)
    persisted = json.loads(config_file.read_text(encoding="utf-8"))
    assert migrated["download_quality_profile"] == BEST_ORIGINAL_PROFILE
    assert persisted["download_quality_profile"] == BEST_ORIGINAL_PROFILE
    assert persisted["compatibility_mp3_bitrate_kbps"] == (
        DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS
    )
    assert persisted["audio_quality"] == "256"

    stable_timestamp = 1_700_000_000_000_000_000
    os.utime(config_file, ns=(stable_timestamp, stable_timestamp))
    second = MusicVaultWindow.load_config(harness)
    assert second["download_quality_profile"] == BEST_ORIGINAL_PROFILE
    assert config_file.stat().st_mtime_ns == stable_timestamp
