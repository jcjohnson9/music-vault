from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


DEFAULT_VOLUME_PERCENT = 75
_CONFIG_SECRET_KEYS = {
    "api_key",
    "youtube_api_key",
    "youtube_api_key_value",
}


def normalize_volume_percent(
    value: object,
    default: int = DEFAULT_VOLUME_PERCENT,
) -> int:
    """Normalize a persisted volume value to an integer from 0 through 100."""

    def coerce(candidate: object) -> int | None:
        if isinstance(candidate, bool) or candidate is None:
            return None
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if not candidate:
                return None
        try:
            number = float(candidate)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        return int(round(number))

    normalized_default = coerce(default)
    if normalized_default is None:
        normalized_default = DEFAULT_VOLUME_PERCENT
    normalized_default = max(0, min(100, normalized_default))

    normalized = coerce(value)
    if normalized is None:
        return normalized_default
    return max(0, min(100, normalized))


def config_for_persistence(config: Mapping[str, Any]) -> dict[str, Any]:
    """Copy config without credential fields that belong in the key file."""
    return {
        str(key): value
        for key, value in config.items()
        if str(key).strip().lower() not in _CONFIG_SECRET_KEYS
    }


def build_track_row_map(track_ids: Iterable[object]) -> dict[int, int]:
    """Build a stable database-track-ID to current table-row lookup."""
    rows: dict[int, int] = {}
    for row, value in enumerate(track_ids):
        try:
            track_id = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        rows.setdefault(track_id, row)
    return rows


def locate_track_row(track_id: object, row_map: Mapping[int, int]) -> int | None:
    try:
        return row_map.get(int(track_id))
    except (TypeError, ValueError, OverflowError):
        return None
