from __future__ import annotations

from collections.abc import Mapping
from typing import Any


BEST_ORIGINAL_PROFILE = "best_original"
MP3_320_COMPATIBILITY_PROFILE = "mp3_320_compatibility"
INHERIT_PROFILE = "inherit"

DOWNLOAD_QUALITY_PROFILES = frozenset(
    {BEST_ORIGINAL_PROFILE, MP3_320_COMPATIBILITY_PROFILE}
)
SOURCE_DOWNLOAD_QUALITY_PROFILES = frozenset(
    {INHERIT_PROFILE, *DOWNLOAD_QUALITY_PROFILES}
)

DEFAULT_DOWNLOAD_QUALITY_PROFILE = BEST_ORIGINAL_PROFILE
DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS = 320


def normalize_download_quality_profile(
    value: object,
    *,
    default: str = DEFAULT_DOWNLOAD_QUALITY_PROFILE,
) -> str:
    """Return a supported global future-download profile.

    Unknown and legacy numeric values intentionally resolve to Best Original.
    A numeric value described an encoder bitrate, not an acquisition profile,
    and therefore must not silently opt an existing installation into future
    lossy transcoding.
    """

    normalized = str(value or "").strip().casefold()
    if normalized in DOWNLOAD_QUALITY_PROFILES:
        return normalized
    normalized_default = str(default or "").strip().casefold()
    if normalized_default not in DOWNLOAD_QUALITY_PROFILES:
        normalized_default = DEFAULT_DOWNLOAD_QUALITY_PROFILE
    return normalized_default


def normalize_source_download_quality_profile(value: object) -> str:
    """Return a supported saved-source override, defaulting safely to inherit."""

    normalized = str(value or "").strip().casefold()
    return normalized if normalized in SOURCE_DOWNLOAD_QUALITY_PROFILES else INHERIT_PROFILE


def normalize_compatibility_mp3_bitrate_kbps(value: object) -> int:
    """Normalize the compatibility encoder setting to the locked 320 kbps value."""

    try:
        int(str(value).strip())
    except (TypeError, ValueError):
        pass
    return DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS


def migrate_audio_quality_config(
    config: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Add the Batch 11 configuration keys without mutating the caller's mapping.

    The former ``audio_quality`` value is considered once when the replacement
    compatibility bitrate is absent.  Existing installations still adopt Best
    Original as required; the old numeric setting never selects MP3 mode.
    Keeping the legacy key in the returned mapping is deliberate compatibility
    for older callers during the one-release transition.
    """

    migrated = dict(config or {})
    original = dict(migrated)

    migrated["download_quality_profile"] = normalize_download_quality_profile(
        migrated.get("download_quality_profile")
    )
    legacy_bitrate = migrated.get(
        "compatibility_mp3_bitrate_kbps",
        migrated.get("audio_quality", DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS),
    )
    migrated["compatibility_mp3_bitrate_kbps"] = (
        normalize_compatibility_mp3_bitrate_kbps(legacy_bitrate)
    )
    return migrated, migrated != original


__all__ = [
    "BEST_ORIGINAL_PROFILE",
    "DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS",
    "DEFAULT_DOWNLOAD_QUALITY_PROFILE",
    "DOWNLOAD_QUALITY_PROFILES",
    "INHERIT_PROFILE",
    "MP3_320_COMPATIBILITY_PROFILE",
    "SOURCE_DOWNLOAD_QUALITY_PROFILES",
    "migrate_audio_quality_config",
    "normalize_compatibility_mp3_bitrate_kbps",
    "normalize_download_quality_profile",
    "normalize_source_download_quality_profile",
]
