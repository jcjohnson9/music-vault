from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable


EDITABLE_METADATA_FIELDS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "release_date",
    "artwork",
)

OBSERVATION_FIELDS = EDITABLE_METADATA_FIELDS + (
    "source_upload_date",
    "source_video_id",
)

MATERIALIZED_COLUMNS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "album_artist",
    "release_date": "release_date",
    "artwork": "cover_path",
}

_RELEASE_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_release_date(value: object) -> str | None:
    """Validate canonical release precision: YYYY, YYYY-MM, or YYYY-MM-DD."""

    text = str(value or "").strip()
    if not text:
        return None
    match = _RELEASE_DATE_RE.fullmatch(text)
    if match is None:
        raise ValueError("Release date must use YYYY, YYYY-MM, or YYYY-MM-DD.")
    year = int(match.group(1))
    month_text = match.group(2)
    day_text = match.group(3)
    if not 1 <= year <= 9999:
        raise ValueError("Release year is outside the supported range.")
    if month_text is None:
        return f"{year:04d}"
    month = int(month_text)
    if not 1 <= month <= 12:
        raise ValueError("Release month is invalid.")
    if day_text is None:
        return f"{year:04d}-{month:02d}"
    day = int(day_text)
    try:
        validated = date(year, month, day)
    except ValueError as exc:
        raise ValueError("Release day is invalid for that month.") from exc
    return validated.isoformat()


def release_year(value: object) -> str | None:
    normalized = normalize_release_date(value)
    return normalized[:4] if normalized else None


def observation_key(
    track_id: int,
    provider: str,
    field_name: str,
    value: object,
    provider_reference: object,
) -> str:
    payload = "\x1f".join(
        (
            str(int(track_id)),
            str(provider or "unknown").strip().casefold(),
            str(field_name).strip().casefold(),
            "" if value is None else str(value),
            "" if provider_reference is None else str(provider_reference),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def create_metadata_schema(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "tracks")
    if "release_date" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN release_date TEXT")
    if "metadata_updated_at" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN metadata_updated_at TEXT")

    editable = ", ".join(f"'{field}'" for field in EDITABLE_METADATA_FIELDS)
    observations = ", ".join(f"'{field}'" for field in OBSERVATION_FIELDS)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS track_metadata_fields (
            track_id INTEGER NOT NULL,
            field_name TEXT NOT NULL CHECK (field_name IN ({editable})),
            value TEXT,
            provenance TEXT NOT NULL,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            is_manual INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0, 1)),
            is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (track_id, field_name),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS track_metadata_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_key TEXT NOT NULL UNIQUE,
            track_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            field_name TEXT NOT NULL CHECK (field_name IN ({observations})),
            value TEXT,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            observed_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS track_metadata_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            change_group_id TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            field_name TEXT NOT NULL CHECK (field_name IN ({editable})),
            old_value TEXT,
            new_value TEXT,
            old_provenance TEXT,
            new_provenance TEXT,
            old_provider_reference TEXT,
            new_provider_reference TEXT,
            old_confidence REAL CHECK (old_confidence IS NULL OR old_confidence BETWEEN 0 AND 100),
            new_confidence REAL CHECK (new_confidence IS NULL OR new_confidence BETWEEN 0 AND 100),
            old_is_manual INTEGER NOT NULL CHECK (old_is_manual IN (0, 1)),
            new_is_manual INTEGER NOT NULL CHECK (new_is_manual IN (0, 1)),
            old_is_locked INTEGER NOT NULL CHECK (old_is_locked IN (0, 1)),
            new_is_locked INTEGER NOT NULL CHECK (new_is_locked IN (0, 1)),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_fields_track "
        "ON track_metadata_fields(track_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_fields_provenance_lock "
        "ON track_metadata_fields(provenance, is_locked)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_observations_track_field "
        "ON track_metadata_observations(track_id, field_name, observed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_history_track_group "
        "ON track_metadata_history(track_id, change_group_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_history_group "
        "ON track_metadata_history(change_group_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_history_track_changed "
        "ON track_metadata_history(track_id, changed_at DESC, id DESC)"
    )


def _insert_field_state(
    conn: sqlite3.Connection,
    *,
    track_id: int,
    field_name: str,
    value: object,
    provenance: str,
    provider_reference: object = None,
    confidence: float | None = None,
    is_locked: bool = False,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO track_metadata_fields (
            track_id, field_name, value, provenance, provider_reference,
            confidence, is_manual, is_locked, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            track_id,
            field_name,
            None if value is None else str(value),
            provenance,
            None if provider_reference is None else str(provider_reference),
            confidence,
            int(is_locked),
            updated_at,
        ),
    )


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    track_id: int,
    provider: str,
    field_name: str,
    value: object,
    provider_reference: object = None,
    confidence: float | None = None,
    observed_at: str,
) -> None:
    key = observation_key(track_id, provider, field_name, value, provider_reference)
    conn.execute(
        """
        INSERT INTO track_metadata_observations (
            observation_key, track_id, provider, field_name, value,
            provider_reference, confidence, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_key) DO UPDATE SET
            confidence=COALESCE(excluded.confidence, track_metadata_observations.confidence),
            observed_at=excluded.observed_at
        """,
        (
            key,
            track_id,
            provider,
            field_name,
            None if value is None else str(value),
            None if provider_reference is None else str(provider_reference),
            confidence,
            observed_at,
        ),
    )


def _credible_musicbrainz(row: sqlite3.Row) -> bool:
    return bool(
        str(row["musicbrainz_recording_id"] or "").strip()
        or str(row["musicbrainz_release_id"] or "").strip()
    )


def seed_existing_metadata(conn: sqlite3.Connection) -> None:
    """Seed v3 state additively without inventing canonical values or history."""

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, artist, album, album_artist, year, release_date,
               cover_path, source_kind, source_video_id, source_upload_date,
               musicbrainz_recording_id, musicbrainz_release_id,
               updated_at, metadata_updated_at
        FROM tracks ORDER BY id
        """
    ).fetchall()
    for row in rows:
        track_id = int(row["id"])
        observed_at = str(row["updated_at"] or utc_now())
        credible_mb = _credible_musicbrainz(row)
        recording_confirmed = bool(str(row["musicbrainz_recording_id"] or "").strip())
        release_confirmed = bool(str(row["musicbrainz_release_id"] or "").strip())
        source_kind = str(row["source_kind"] or "").strip().casefold()

        canonical_release = None
        if row["release_date"]:
            try:
                canonical_release = normalize_release_date(row["release_date"])
            except ValueError:
                canonical_release = None
        elif row["year"] and (source_kind != "youtube" or credible_mb):
            try:
                canonical_release = normalize_release_date(row["year"])
            except ValueError:
                canonical_release = None
        if canonical_release:
            conn.execute(
                "UPDATE tracks SET release_date=?, year=? WHERE id=?",
                (canonical_release, canonical_release[:4], track_id),
            )

        base_provider = "youtube" if source_kind == "youtube" else "embedded"
        values = {
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
            "album_artist": row["album_artist"],
            "release_date": canonical_release,
            "artwork": row["cover_path"],
        }
        artwork_confirmed = bool(
            release_confirmed
            and row["cover_path"]
            and Path(str(row["cover_path"])).stem.casefold()
            == str(row["musicbrainz_release_id"]).casefold()
        )
        for field_name in ("title", "artist", "album", "album_artist"):
            value = values[field_name]
            confirmed = (
                recording_confirmed
                if field_name in {"title", "artist"}
                else release_confirmed
                if field_name == "album"
                else False
            )
            if confirmed and value is not None:
                provenance = "musicbrainz_confirmed"
                reference = (
                    row["musicbrainz_recording_id"]
                    if field_name in {"title", "artist"}
                    else row["musicbrainz_release_id"]
                )
                locked = True
            else:
                provenance = base_provider if value is not None else "unknown"
                reference = row["source_video_id"] if source_kind == "youtube" else None
                locked = False
            _insert_field_state(
                conn,
                track_id=track_id,
                field_name=field_name,
                value=value,
                provenance=provenance,
                provider_reference=reference,
                is_locked=locked,
                updated_at=observed_at,
            )

        _insert_field_state(
            conn,
            track_id=track_id,
            field_name="release_date",
            value=canonical_release,
            provenance=(
                "musicbrainz_confirmed"
                if canonical_release is not None and release_confirmed
                else "embedded"
                if canonical_release is not None
                else "unknown"
            ),
            provider_reference=(
                row["musicbrainz_release_id"]
                if canonical_release is not None and release_confirmed
                else None
            ),
            is_locked=canonical_release is not None and release_confirmed,
            updated_at=observed_at,
        )
        _insert_field_state(
            conn,
            track_id=track_id,
            field_name="artwork",
            value=row["cover_path"],
            provenance=(
                "cover_art_archive"
                if artwork_confirmed
                else "youtube_thumbnail"
                if row["cover_path"] is not None and source_kind == "youtube"
                else "embedded"
                if row["cover_path"] is not None
                else "unknown"
            ),
            provider_reference=row["musicbrainz_release_id"] if artwork_confirmed else None,
            is_locked=artwork_confirmed,
            updated_at=observed_at,
        )

        for field_name, value in values.items():
            if value is None:
                continue
            confirmed = (
                recording_confirmed
                if field_name in {"title", "artist"}
                else release_confirmed
                if field_name in {"album", "release_date"}
                else artwork_confirmed
                if field_name == "artwork"
                else False
            )
            if confirmed:
                provider = "cover_art_archive" if field_name == "artwork" else "musicbrainz"
                reference = (
                    row["musicbrainz_recording_id"]
                    if field_name in {"title", "artist"}
                    else row["musicbrainz_release_id"]
                )
            else:
                provider = (
                    "youtube_thumbnail"
                    if field_name == "artwork" and source_kind == "youtube"
                    else base_provider
                )
                reference = row["source_video_id"] if source_kind == "youtube" else None
            _insert_observation(
                conn,
                track_id=track_id,
                provider=provider,
                field_name=field_name,
                value=value,
                provider_reference=reference,
                observed_at=observed_at,
            )

        if source_kind == "youtube":
            for field_name in ("source_video_id", "source_upload_date"):
                value = row[field_name]
                if value is not None:
                    _insert_observation(
                        conn,
                        track_id=track_id,
                        provider="youtube",
                        field_name=field_name,
                        value=value,
                        provider_reference=row["source_video_id"],
                        observed_at=observed_at,
                    )


def required_metadata_indexes() -> Iterable[str]:
    return (
        "idx_metadata_fields_track",
        "idx_metadata_fields_provenance_lock",
        "idx_metadata_observations_track_field",
        "idx_metadata_history_track_group",
        "idx_metadata_history_group",
        "idx_metadata_history_track_changed",
    )
