from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import PurePath
from typing import Iterable

from .sync_schema import utc_now


TRACK_MEDIA_QUALITY_TABLE = "track_media_quality"

ACQUISITION_PROFILES = frozenset(
    {
        "best_original",
        "mp3_320_compatibility",
        "legacy_youtube_mp3",
        "local_import",
        "unknown",
    }
)
TRANSFORMATION_KINDS = frozenset(
    {
        "none",
        "source_preserved_remux",
        "lossy_transcode",
        "legacy_inferred_transcode",
        "local_original",
        "unknown",
    }
)
INSPECTION_STATES = frozenset(
    {"uninspected", "legacy_inferred", "inspected", "failed"}
)


def create_media_quality_schema(conn: sqlite3.Connection) -> None:
    """Create the additive schema-v8 quality inventory and its query indexes."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACK_MEDIA_QUALITY_TABLE} (
            track_id INTEGER PRIMARY KEY,
            acquisition_profile TEXT NOT NULL DEFAULT 'unknown'
                CHECK (acquisition_profile IN (
                    'best_original', 'mp3_320_compatibility',
                    'legacy_youtube_mp3', 'local_import', 'unknown'
                )),
            source_format_id TEXT,
            source_extension TEXT,
            source_container TEXT,
            source_codec TEXT,
            source_bitrate_kbps INTEGER
                CHECK (source_bitrate_kbps IS NULL OR source_bitrate_kbps > 0),
            source_sample_rate_hz INTEGER
                CHECK (source_sample_rate_hz IS NULL OR source_sample_rate_hz > 0),
            source_channels INTEGER
                CHECK (source_channels IS NULL OR source_channels > 0),
            source_filesize_bytes INTEGER
                CHECK (source_filesize_bytes IS NULL OR source_filesize_bytes >= 0),
            stored_extension TEXT,
            stored_container TEXT,
            stored_codec TEXT,
            stored_bitrate_kbps INTEGER
                CHECK (stored_bitrate_kbps IS NULL OR stored_bitrate_kbps > 0),
            stored_sample_rate_hz INTEGER
                CHECK (stored_sample_rate_hz IS NULL OR stored_sample_rate_hz > 0),
            stored_channels INTEGER
                CHECK (stored_channels IS NULL OR stored_channels > 0),
            stored_filesize_bytes INTEGER
                CHECK (stored_filesize_bytes IS NULL OR stored_filesize_bytes >= 0),
            transformation_kind TEXT NOT NULL DEFAULT 'unknown'
                CHECK (transformation_kind IN (
                    'none', 'source_preserved_remux', 'lossy_transcode',
                    'legacy_inferred_transcode', 'local_original', 'unknown'
                )),
            inspection_state TEXT NOT NULL DEFAULT 'uninspected'
                CHECK (inspection_state IN (
                    'uninspected', 'legacy_inferred', 'inspected', 'failed'
                )),
            provenance TEXT NOT NULL DEFAULT 'unknown'
                CHECK (length(trim(provenance)) > 0),
            inspected_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_track_media_quality_acquisition "
        "ON track_media_quality(acquisition_profile, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_track_media_quality_inspection "
        "ON track_media_quality(inspection_state, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_track_media_quality_stored_codec "
        "ON track_media_quality(stored_codec, track_id)",
    ):
        conn.execute(statement)


def _stored_extension(value: object) -> str | None:
    try:
        suffix = PurePath(str(value or "").strip()).suffix.casefold()
    except (TypeError, ValueError):
        return None
    return suffix or None


def _normalized_extension(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if "/" not in text and "\\" not in text:
        if text.startswith(".") and text.count(".") == 1:
            return text
        if "." not in text:
            return f".{text}"
    return _stored_extension(text)


def _legacy_classification(
    *,
    path: object,
    source_kind: object,
) -> tuple[str, str, str, str | None]:
    """Classify stored legacy facts without opening or probing personal media."""

    extension = _stored_extension(path)
    normalized_source = str(source_kind or "").strip().casefold()
    if normalized_source == "youtube" and extension == ".mp3":
        return (
            "legacy_youtube_mp3",
            "legacy_inferred_transcode",
            "legacy_inferred",
            extension,
        )
    if normalized_source in {"", "embedded", "local"}:
        return "local_import", "local_original", "legacy_inferred", extension
    return "unknown", "unknown", "uninspected", extension


def seed_existing_track_media_quality(
    conn: sqlite3.Connection,
    track_ids: Iterable[int] | None = None,
) -> int:
    """Create conservative inventory rows, preserving every existing row.

    Only database source classification and the stored filename extension are
    observed.  Source format, codec, bitrate, and all probed media facts remain
    NULL until an explicit read-only inspection or a future acquisition records
    them.
    """

    query = "SELECT id, path, source_kind FROM tracks"
    parameters: tuple[object, ...] = ()
    if track_ids is not None:
        normalized_ids = sorted({int(value) for value in track_ids})
        if not normalized_ids:
            return 0
        placeholders = ", ".join("?" for _ in normalized_ids)
        query += f" WHERE id IN ({placeholders})"
        parameters = tuple(normalized_ids)
    query += " ORDER BY id"

    timestamp = utc_now()
    created = 0
    for row in conn.execute(query, parameters).fetchall():
        acquisition, transformation, inspection, extension = _legacy_classification(
            path=row[1],
            source_kind=row[2],
        )
        cursor = conn.execute(
            f"""
            INSERT OR IGNORE INTO {TRACK_MEDIA_QUALITY_TABLE} (
                track_id, acquisition_profile, stored_extension,
                transformation_kind, inspection_state, provenance,
                inspected_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'schema_v8_legacy_classification', NULL, ?, ?)
            """,
            (
                int(row[0]),
                acquisition,
                extension,
                transformation,
                inspection,
                timestamp,
                timestamp,
            ),
        )
        created += max(0, int(cursor.rowcount))
    return created


def _profile(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in ACQUISITION_PROFILES:
        raise ValueError("Unsupported acquisition profile.")
    return normalized


def _transformation(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in TRANSFORMATION_KINDS:
        raise ValueError("Unsupported media transformation kind.")
    return normalized


def _inspection_state(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in INSPECTION_STATES:
        raise ValueError("Unsupported media inspection state.")
    return normalized


def _optional_text(value: object, *, casefold: bool = False) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized.casefold() if casefold else normalized


def _optional_positive_int(value: object, *, allow_zero: bool = False) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Media quality numeric facts must be integers.") from exc
    minimum = 0 if allow_zero else 1
    if normalized < minimum:
        raise ValueError("Media quality numeric facts must be positive.")
    return normalized


def _provenance_text(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip()
    elif isinstance(value, (Mapping, list, tuple)):
        normalized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    else:
        normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("Media quality provenance is required.")
    return normalized


def upsert_track_media_quality(
    conn: sqlite3.Connection,
    track_id: int,
    *,
    acquisition_profile: object,
    source_format_id: object = None,
    source_extension: object = None,
    source_container: object = None,
    source_codec: object = None,
    source_bitrate_kbps: object = None,
    source_sample_rate_hz: object = None,
    source_channels: object = None,
    source_filesize_bytes: object = None,
    stored_extension: object = None,
    stored_container: object = None,
    stored_codec: object = None,
    stored_bitrate_kbps: object = None,
    stored_sample_rate_hz: object = None,
    stored_channels: object = None,
    stored_filesize_bytes: object = None,
    transformation_kind: object,
    inspection_state: object,
    provenance: object,
    inspected_at: object = None,
) -> sqlite3.Row:
    """Persist one complete, truthful source/stored quality observation.

    The caller supplies the complete known fact set.  Passing ``None`` clears
    a previously observed optional value, preventing stale source claims from
    surviving a replacement acquisition.  ``created_at`` remains stable.
    """

    create_media_quality_schema(conn)
    timestamp = utc_now()
    values = {
        "track_id": int(track_id),
        "acquisition_profile": _profile(acquisition_profile),
        "source_format_id": _optional_text(source_format_id),
        "source_extension": _normalized_extension(source_extension),
        "source_container": _optional_text(source_container, casefold=True),
        "source_codec": _optional_text(source_codec, casefold=True),
        "source_bitrate_kbps": _optional_positive_int(source_bitrate_kbps),
        "source_sample_rate_hz": _optional_positive_int(source_sample_rate_hz),
        "source_channels": _optional_positive_int(source_channels),
        "source_filesize_bytes": _optional_positive_int(
            source_filesize_bytes, allow_zero=True
        ),
        "stored_extension": _normalized_extension(stored_extension),
        "stored_container": _optional_text(stored_container, casefold=True),
        "stored_codec": _optional_text(stored_codec, casefold=True),
        "stored_bitrate_kbps": _optional_positive_int(stored_bitrate_kbps),
        "stored_sample_rate_hz": _optional_positive_int(stored_sample_rate_hz),
        "stored_channels": _optional_positive_int(stored_channels),
        "stored_filesize_bytes": _optional_positive_int(
            stored_filesize_bytes, allow_zero=True
        ),
        "transformation_kind": _transformation(transformation_kind),
        "inspection_state": _inspection_state(inspection_state),
        "provenance": _provenance_text(provenance),
        "inspected_at": _optional_text(inspected_at),
        "timestamp": timestamp,
    }
    conn.execute(
        f"""
        INSERT INTO {TRACK_MEDIA_QUALITY_TABLE} (
            track_id, acquisition_profile, source_format_id, source_extension,
            source_container, source_codec, source_bitrate_kbps,
            source_sample_rate_hz, source_channels, source_filesize_bytes,
            stored_extension, stored_container, stored_codec,
            stored_bitrate_kbps, stored_sample_rate_hz, stored_channels,
            stored_filesize_bytes, transformation_kind, inspection_state,
            provenance, inspected_at, created_at, updated_at
        ) VALUES (
            :track_id, :acquisition_profile, :source_format_id, :source_extension,
            :source_container, :source_codec, :source_bitrate_kbps,
            :source_sample_rate_hz, :source_channels, :source_filesize_bytes,
            :stored_extension, :stored_container, :stored_codec,
            :stored_bitrate_kbps, :stored_sample_rate_hz, :stored_channels,
            :stored_filesize_bytes, :transformation_kind, :inspection_state,
            :provenance, :inspected_at, :timestamp, :timestamp
        )
        ON CONFLICT(track_id) DO UPDATE SET
            acquisition_profile=excluded.acquisition_profile,
            source_format_id=excluded.source_format_id,
            source_extension=excluded.source_extension,
            source_container=excluded.source_container,
            source_codec=excluded.source_codec,
            source_bitrate_kbps=excluded.source_bitrate_kbps,
            source_sample_rate_hz=excluded.source_sample_rate_hz,
            source_channels=excluded.source_channels,
            source_filesize_bytes=excluded.source_filesize_bytes,
            stored_extension=excluded.stored_extension,
            stored_container=excluded.stored_container,
            stored_codec=excluded.stored_codec,
            stored_bitrate_kbps=excluded.stored_bitrate_kbps,
            stored_sample_rate_hz=excluded.stored_sample_rate_hz,
            stored_channels=excluded.stored_channels,
            stored_filesize_bytes=excluded.stored_filesize_bytes,
            transformation_kind=excluded.transformation_kind,
            inspection_state=excluded.inspection_state,
            provenance=excluded.provenance,
            inspected_at=excluded.inspected_at,
            updated_at=excluded.updated_at
        """,
        values,
    )
    row = conn.execute(
        f"SELECT * FROM {TRACK_MEDIA_QUALITY_TABLE} WHERE track_id=?",
        (int(track_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError("The track media quality row could not be persisted.")
    return row


def get_track_media_quality(
    conn: sqlite3.Connection,
    track_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {TRACK_MEDIA_QUALITY_TABLE} WHERE track_id=?",
        (int(track_id),),
    ).fetchone()


def track_media_quality_summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Return stable aggregate profile counts without exposing track metadata."""

    counts = {profile: 0 for profile in sorted(ACQUISITION_PROFILES)}
    for row in conn.execute(
        f"SELECT acquisition_profile, COUNT(*) FROM {TRACK_MEDIA_QUALITY_TABLE} "
        "GROUP BY acquisition_profile"
    ):
        counts[str(row[0])] = int(row[1])
    return counts


def required_media_quality_indexes() -> tuple[str, ...]:
    return (
        "idx_track_media_quality_acquisition",
        "idx_track_media_quality_inspection",
        "idx_track_media_quality_stored_codec",
    )


__all__ = [
    "ACQUISITION_PROFILES",
    "INSPECTION_STATES",
    "TRACK_MEDIA_QUALITY_TABLE",
    "TRANSFORMATION_KINDS",
    "create_media_quality_schema",
    "get_track_media_quality",
    "required_media_quality_indexes",
    "seed_existing_track_media_quality",
    "track_media_quality_summary",
    "upsert_track_media_quality",
]
