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
    "original_release_date",
    "version_type",
    "version_label",
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
    "original_release_date": "original_release_date",
    "version_type": "version_type",
    "version_label": "version_label",
    "artwork": "cover_path",
}

VERSION_TYPES = (
    "studio",
    "live",
    "remix",
    "edit",
    "acoustic",
    "cover",
    "instrumental",
    "demo",
    "radio_edit",
    "extended",
    "sped_up",
    "slowed",
    "nightcore",
    "mashup",
    "re_recording",
    "soundtrack",
    "youtube_exclusive",
    "unknown",
)

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


def normalize_version_type(value: object) -> str | None:
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    if text not in VERSION_TYPES:
        raise ValueError(f"Unsupported version type: {text}")
    return text


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


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return str(row[0] or "") if row is not None else ""


def _rebuild_checked_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    create_sql: str,
    columns: tuple[str, ...],
) -> None:
    """Expand a CHECK-constrained table without losing a single stored value."""

    current_sql = _table_sql(conn, table)
    if not current_sql:
        conn.execute(create_sql.format(table=table))
        return
    required_literals = ("'original_release_date'", "'version_type'", "'version_label'")
    if all(value in current_sql for value in required_literals):
        return

    replacement = f"{table}_schema_v6_new"
    conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
    conn.execute(create_sql.format(table=replacement))
    names = ", ".join(f'"{name}"' for name in columns)
    before_count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    conn.execute(
        f'INSERT INTO "{replacement}" ({names}) SELECT {names} FROM "{table}"'
    )
    after_count = int(
        conn.execute(f'SELECT COUNT(*) FROM "{replacement}"').fetchone()[0]
    )
    missing = int(
        conn.execute(
            f'SELECT COUNT(*) FROM (SELECT {names} FROM "{table}" '
            f'EXCEPT SELECT {names} FROM "{replacement}")'
        ).fetchone()[0]
    )
    added = int(
        conn.execute(
            f'SELECT COUNT(*) FROM (SELECT {names} FROM "{replacement}" '
            f'EXCEPT SELECT {names} FROM "{table}")'
        ).fetchone()[0]
    )
    if before_count != after_count or missing or added:
        raise RuntimeError(f"Could not verify the schema-v6 copy of {table}.")
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')


def create_metadata_schema(conn: sqlite3.Connection) -> None:
    columns = _columns(conn, "tracks")
    if "release_date" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN release_date TEXT")
    if "metadata_updated_at" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN metadata_updated_at TEXT")
    version_types = ", ".join(f"'{value}'" for value in VERSION_TYPES)
    for column, definition in (
        ("original_release_date", "TEXT"),
        ("version_type", f"TEXT CHECK (version_type IS NULL OR version_type IN ({version_types}))"),
        ("version_label", "TEXT"),
        ("discogs_release_id", "TEXT"),
        ("discogs_master_id", "TEXT"),
        ("discogs_track_position", "TEXT"),
        ("recording_group_key", "TEXT"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {definition}")

    editable = ", ".join(f"'{field}'" for field in EDITABLE_METADATA_FIELDS)
    observations = ", ".join(f"'{field}'" for field in OBSERVATION_FIELDS)
    version_value_check = (
        "field_name != 'version_type' OR value IS NULL OR "
        f"value IN ({version_types})"
    )
    field_sql = f"""
        CREATE TABLE {{table}} (
            track_id INTEGER NOT NULL,
            field_name TEXT NOT NULL CHECK (field_name IN ({editable})),
            value TEXT CHECK ({version_value_check}),
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
    observation_sql = f"""
        CREATE TABLE {{table}} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_key TEXT NOT NULL UNIQUE,
            track_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            field_name TEXT NOT NULL CHECK (field_name IN ({observations})),
            value TEXT CHECK ({version_value_check}),
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            observed_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    history_sql = f"""
        CREATE TABLE {{table}} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            change_group_id TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            field_name TEXT NOT NULL CHECK (field_name IN ({editable})),
            old_value TEXT CHECK (field_name != 'version_type' OR old_value IS NULL OR old_value IN ({version_types})),
            new_value TEXT CHECK (field_name != 'version_type' OR new_value IS NULL OR new_value IN ({version_types})),
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
    _rebuild_checked_table(
        conn,
        table="track_metadata_fields",
        create_sql=field_sql,
        columns=(
            "track_id", "field_name", "value", "provenance", "provider_reference",
            "confidence", "is_manual", "is_locked", "updated_at",
        ),
    )
    _rebuild_checked_table(
        conn,
        table="track_metadata_observations",
        create_sql=observation_sql,
        columns=(
            "id", "observation_key", "track_id", "provider", "field_name", "value",
            "provider_reference", "confidence", "observed_at",
        ),
    )
    _rebuild_checked_table(
        conn,
        table="track_metadata_history",
        create_sql=history_sql,
        columns=(
            "id", "change_group_id", "track_id", "field_name", "old_value", "new_value",
            "old_provenance", "new_provenance", "old_provider_reference",
            "new_provider_reference", "old_confidence", "new_confidence",
            "old_is_manual", "new_is_manual", "old_is_locked", "new_is_locked",
            "actor", "reason", "changed_at",
        ),
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
               original_release_date, version_type, version_label,
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
            "original_release_date": row["original_release_date"],
            "version_type": row["version_type"],
            "version_label": row["version_label"],
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
        for field_name in ("original_release_date", "version_type", "version_label"):
            value = values[field_name]
            _insert_field_state(
                conn,
                track_id=track_id,
                field_name=field_name,
                value=value,
                provenance="embedded" if value is not None else "unknown",
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
