from __future__ import annotations

"""Aggregate-only gate for the explicitly authorized schema-5 to schema-6 run.

The baseline mode uses SQLite immutable read-only access and never constructs
``MusicVaultDB``.  This tool does not launch the application, access provider
credentials, run a provider lookup, or print library values/paths.  A caller
must explicitly choose a mode, acknowledge the live library, set the no-secret
acceptance environment, and close MusicVault.exe.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core.sync_schema import required_sync_indexes  # noqa: E402
from music_vault.metadata.artist_credits import normalize_artist_name  # noqa: E402
from music_vault.metadata.intelligence_schema import (  # noqa: E402
    required_intelligence_indexes,
)
from tools.dev import verify_batch10_live_migration as batch10_gate  # noqa: E402


EXPECTED_PRE_SCHEMA = 5
EXPECTED_POST_SCHEMA = 6
BASELINE_FORMAT_VERSION = 2
ACKNOWLEDGEMENT = "batch10.1-live-schema-5-to-6"

PRESERVED_TABLES = (
    "tracks",
    "playlists",
    "playlist_tracks",
    "playlist_track_origins",
    "sync_sources",
    "sync_source_items",
    "source_track_identities",
    "source_identity_conflicts",
    "sync_source_runs",
    "sync_failures",
    "track_metadata_fields",
    "track_metadata_observations",
    "track_metadata_history",
    "metadata_remediation_jobs",
    "metadata_remediation_items",
    "metadata_provider_cache",
)
RUNTIME_GUARD_FILES = {
    "config": "music_vault_config.json",
    "download_archive": "youtube_download_archive.txt",
    "failed_ids": "youtube_failed_ids.txt",
}
SECRET_FILE_NAMES = {
    "youtube_api_key": "youtube_api_key.txt",
    "discogs_token": "discogs_token.txt",
}
V6_TABLES = {
    "artists",
    "track_artist_credits",
    "track_release_context",
    "metadata_intelligence_jobs",
    "metadata_intelligence_items",
}
V6_REQUIRED_INDEXES = frozenset(
    {*required_sync_indexes(), *required_intelligence_indexes()}
)
V6_FIELD_EXTENSIONS = {
    "original_release_date": "original_release_date",
    "version_type": "version_type",
    "version_label": "version_label",
}
FIELD_STATE_COLUMNS = (
    "track_id",
    "field_name",
    "value",
    "provenance",
    "provider_reference",
    "confidence",
    "is_manual",
    "is_locked",
    "updated_at",
)
SAFE_STATUS_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "app",
        "app_version",
        "release_channel",
        "updated_at",
        "health",
        "library",
        "playback",
        "sync",
        "party_mode_active",
        "party_mode_preset",
        "audio_reactivity_available",
        "party_mode_lyrics_enabled",
        "lyrics_available",
        "lyrics_synchronized",
        "metadata_intelligence_enabled",
        "metadata_intelligence_job_status",
        "metadata_intelligence_total",
        "metadata_intelligence_analyzed",
        "metadata_intelligence_applied",
        "metadata_intelligence_review_count",
        "discogs_ready",
        "paths",
    }
)
FORBIDDEN_STATUS_KEY_PARTS = (
    "token",
    "authorization",
    "provider_query",
    "query_title",
    "uploader",
    "image_url",
    "discogs_release_id",
    "discogs_master_id",
    "recording_id",
    "source_item",
    "playlist_title",
)


class GateFailure(RuntimeError):
    pass


def _hash_text(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _aggregate_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _readonly(database: Path) -> sqlite3.Connection:
    return batch10_gate._readonly_connection(database)


def _normal_readonly(database: Path) -> sqlite3.Connection:
    return batch10_gate._normal_readonly_connection(database)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return batch10_gate._tables(connection)


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return sorted(batch10_gate._columns(connection, table))


def _quoted(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _row_digest(row: Sequence[object]) -> str:
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            payload = b"N"
        elif isinstance(value, bytes):
            payload = b"B" + hashlib.sha256(value).digest()
        else:
            payload = b"T" + str(value).encode("utf-8", errors="surrogatepass")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _table_guard(
    connection: sqlite3.Connection,
    table: str,
    *,
    columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    tables = _tables(connection)
    if table not in tables:
        return {"exists": False, "count": 0, "columns": [], "digest": _aggregate_digest(())}
    selected = list(columns) if columns is not None else _columns(connection, table)
    available = set(_columns(connection, table))
    if not selected or not set(selected) <= available:
        raise GateFailure("preserved_table_shape_changed")
    sql = "SELECT " + ",".join(_quoted(name) for name in selected) + " FROM " + _quoted(table)
    records = [_row_digest(tuple(row)) for row in connection.execute(sql)]
    return {
        "exists": True,
        "count": len(records),
        "columns": selected,
        "digest": _aggregate_digest(records),
    }


def _preserved_table_guards(
    connection: sqlite3.Connection,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    before = baseline.get("preserved_tables", {}) if isinstance(baseline, Mapping) else {}
    guards: dict[str, dict[str, Any]] = {}
    for table in PRESERVED_TABLES:
        columns = None
        prior = before.get(table) if isinstance(before, Mapping) else None
        if isinstance(prior, Mapping) and isinstance(prior.get("columns"), list):
            columns = [str(value) for value in prior["columns"]]
        guards[table] = _table_guard(connection, table, columns=columns)
    return guards


def _provider_guard(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(connection)
    track_columns = set(_columns(connection, "tracks")) if "tracks" in tables else set()

    def count(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0])

    observations = 0
    if "track_metadata_observations" in tables:
        observations = count(
            "SELECT COUNT(*) FROM track_metadata_observations "
            "WHERE lower(provider) IN ('discogs','musicbrainz')"
        )
    return {
        "provider_observation_count": observations,
        "provider_cache_count": (
            count("SELECT COUNT(*) FROM metadata_provider_cache")
            if "metadata_provider_cache" in tables
            else 0
        ),
        "musicbrainz_recording_count": (
            count("SELECT COUNT(*) FROM tracks WHERE NULLIF(TRIM(musicbrainz_recording_id),'') IS NOT NULL")
            if "musicbrainz_recording_id" in track_columns
            else 0
        ),
        "musicbrainz_release_count": (
            count("SELECT COUNT(*) FROM tracks WHERE NULLIF(TRIM(musicbrainz_release_id),'') IS NOT NULL")
            if "musicbrainz_release_id" in track_columns
            else 0
        ),
        "discogs_release_count": (
            count("SELECT COUNT(*) FROM tracks WHERE NULLIF(TRIM(discogs_release_id),'') IS NOT NULL")
            if "discogs_release_id" in track_columns
            else 0
        ),
        "discogs_master_count": (
            count("SELECT COUNT(*) FROM tracks WHERE NULLIF(TRIM(discogs_master_id),'') IS NOT NULL")
            if "discogs_master_id" in track_columns
            else 0
        ),
        "intelligence_job_count": (
            count("SELECT COUNT(*) FROM metadata_intelligence_jobs")
            if "metadata_intelligence_jobs" in tables
            else 0
        ),
        "intelligence_item_count": (
            count("SELECT COUNT(*) FROM metadata_intelligence_items")
            if "metadata_intelligence_items" in tables
            else 0
        ),
        "release_context_count": (
            count("SELECT COUNT(*) FROM track_release_context")
            if "track_release_context" in tables
            else 0
        ),
    }


def _artist_baseline(connection: sqlite3.Connection) -> dict[str, Any]:
    if "tracks" not in _tables(connection):
        return {
            "nonempty_count": 0,
            "ampersand_count": 0,
            "display_digest": _aggregate_digest(()),
        }
    rows = connection.execute(
        "SELECT id,artist FROM tracks WHERE NULLIF(TRIM(artist),'') IS NOT NULL"
    ).fetchall()
    return {
        "nonempty_count": len(rows),
        "ampersand_count": sum("&" in str(row[1]) for row in rows),
        "display_digest": _aggregate_digest(
            f"{int(row[0])}:{_hash_text(row[1])}" for row in rows
        ),
    }


def _secret_metadata(path: Path) -> dict[str, Any]:
    # Deliberately do not open or hash credential files.
    return batch10_gate._file_guard(path, include_digest=False)


def capture_baseline(
    *,
    project_root: Path,
    data_dir: Path,
    database: Path,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    core = batch10_gate.capture_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
    )
    connection = _readonly(database)
    try:
        preserved = _preserved_table_guards(connection, baseline)
        artists = _artist_baseline(connection)
        providers = _provider_guard(connection)
    finally:
        connection.close()
    runtime_guards = {
        key: batch10_gate._file_guard(data_dir / name, include_digest=True)
        for key, name in RUNTIME_GUARD_FILES.items()
    }
    credentials = {
        key: _secret_metadata(data_dir / name)
        for key, name in SECRET_FILE_NAMES.items()
    }
    return {
        "baseline_format_version": BASELINE_FORMAT_VERSION,
        "database": core["database"],
        "sidecars": core["sidecars"],
        "media": core["media"],
        "backups": core["backups"],
        "packaged_data_folder_exists": core["packaged_data_folder_exists"],
        "runtime_guards": runtime_guards,
        "credential_metadata": credentials,
        "preserved_tables": preserved,
        "artist_baseline": artists,
        "provider_guard": providers,
    }


def _backup_path(backup_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    candidate = backup_dir / f"music_vault_batch10_1_live_rollback_{stamp}.sqlite3"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / f"music_vault_batch10_1_live_rollback_{stamp}_{counter}.sqlite3"
        counter += 1
    return candidate


def _verify_backup(backup: Path, baseline: Mapping[str, Any]) -> bool:
    try:
        if not backup.is_file() or backup.stat().st_size <= 0:
            return False
        connection = _normal_readonly(backup)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table_counts = batch10_gate._all_table_counts(connection)
            preserved = _preserved_table_guards(connection, baseline)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, GateFailure, ValueError, TypeError):
        return False
    return bool(
        integrity.casefold() == "ok"
        and schema == EXPECTED_PRE_SCHEMA
        and table_counts == baseline["database"]["table_counts"]
        and preserved == baseline["preserved_tables"]
    )


def create_verified_backup(
    *,
    database: Path,
    backup_dir: Path,
    baseline: Mapping[str, Any],
    backup_path: Path | None = None,
) -> dict[str, Any]:
    if int(baseline["database"]["schema_version"]) != EXPECTED_PRE_SCHEMA:
        raise GateFailure("baseline_schema_is_not_5")
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination_path = backup_path or _backup_path(backup_dir)
    if destination_path.exists():
        raise GateFailure("rollback_backup_already_exists")
    source = _readonly(database)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    if not _verify_backup(destination_path, baseline):
        raise GateFailure("rollback_backup_verification_failed")
    return {
        "ok": True,
        "created": True,
        "verified": True,
        "schema_version": EXPECTED_PRE_SCHEMA,
        "size": int(destination_path.stat().st_size),
        "opaque_file_token": batch10_gate._backup_file_token(destination_path),
    }


def _status_is_safe(path: Path) -> tuple[bool, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, False
    if not isinstance(payload, dict) or not set(payload) <= SAFE_STATUS_TOP_LEVEL:
        return False, False

    def unsafe(value: object) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                folded = str(key).casefold()
                if any(part in folded for part in FORBIDDEN_STATUS_KEY_PARTS):
                    # Legacy neutral placeholders are safe only when empty.
                    if nested not in (None, "", [], {}):
                        return True
                if unsafe(nested):
                    return True
        elif isinstance(value, list):
            return any(unsafe(item) for item in value)
        elif isinstance(value, str):
            folded = value.casefold()
            if "discogs token=" in folded or "authorization:" in folded:
                return True
        return False

    aggregate = all(
        int(payload.get(key) or 0) == 0
        for key in (
            "metadata_intelligence_total",
            "metadata_intelligence_analyzed",
            "metadata_intelligence_applied",
            "metadata_intelligence_review_count",
        )
    )
    return True, bool(aggregate and not unsafe(payload))


def _new_backups(backup_dir: Path, baseline: Mapping[str, Any]) -> list[Path]:
    old = {str(value) for value in baseline["backups"].get("file_tokens", [])}
    return [
        path
        for path in sorted(backup_dir.glob("*.sqlite3"))
        if path.is_file() and batch10_gate._backup_file_token(path) not in old
    ]


def _field_state_rows(
    connection: sqlite3.Connection,
) -> dict[tuple[int, str], tuple[object, ...]]:
    if "track_metadata_fields" not in _tables(connection):
        return {}
    available = set(_columns(connection, "track_metadata_fields"))
    if not set(FIELD_STATE_COLUMNS) <= available:
        raise GateFailure("field_state_shape_changed")
    rows = connection.execute(
        "SELECT "
        + ",".join(_quoted(name) for name in FIELD_STATE_COLUMNS)
        + " FROM track_metadata_fields"
    ).fetchall()
    result: dict[tuple[int, str], tuple[object, ...]] = {}
    for row in rows:
        values = tuple(row)
        key = (int(values[0]), str(values[1]))
        if key in result:
            raise GateFailure("duplicate_track_field_state")
        result[key] = values
    return result


def _track_extension_state(
    connection: sqlite3.Connection,
) -> tuple[dict[int, dict[str, object]], set[str]]:
    available = set(_columns(connection, "tracks"))
    selected = [
        name
        for name in (
            "id",
            "updated_at",
            "metadata_updated_at",
            "source_upload_date",
            *V6_FIELD_EXTENSIONS.values(),
        )
        if name in available
    ]
    if "id" not in selected:
        raise GateFailure("tracks_shape_changed")
    rows = connection.execute(
        "SELECT " + ",".join(_quoted(name) for name in selected) + " FROM tracks"
    ).fetchall()
    return (
        {
            int(row[0]): {name: row[index] for index, name in enumerate(selected)}
            for row in rows
        },
        available,
    )


def _field_state_checks(
    connection: sqlite3.Connection,
    reference: sqlite3.Connection,
) -> dict[str, Any]:
    """Prove v6 added only its three inert field-state rows per track."""

    current_rows = _field_state_rows(connection)
    baseline_rows = _field_state_rows(reference)
    current_tracks, current_columns = _track_extension_state(connection)
    baseline_tracks, baseline_columns = _track_extension_state(reference)

    baseline_keys = set(baseline_rows)
    current_keys = set(current_rows)
    extension_names = set(V6_FIELD_EXTENSIONS)
    baseline_extension_count = sum(
        field_name in extension_names for _track_id, field_name in baseline_keys
    )
    preserved_count = sum(
        current_rows.get(key) == row for key, row in baseline_rows.items()
    )
    additions = current_keys - baseline_keys
    expected_additions = {
        (track_id, field_name)
        for track_id in baseline_tracks
        for field_name in V6_FIELD_EXTENSIONS
    }

    duplicate_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT track_id,field_name,COUNT(*) AS copies
                FROM track_metadata_fields
                GROUP BY track_id,field_name
                HAVING copies != 1
            )
            """
        ).fetchone()[0]
    )
    safe_default_count = 0
    safe_authority_count = 0
    preserved_materialized_count = 0
    no_fabricated_date_count = 0
    for track_id, field_name in sorted(expected_additions):
        row = current_rows.get((track_id, field_name))
        current_track = current_tracks.get(track_id, {})
        baseline_track = baseline_tracks.get(track_id, {})
        column = V6_FIELD_EXTENSIONS[field_name]
        current_value = current_track.get(column)
        baseline_value = baseline_track.get(column) if column in baseline_columns else None

        if current_value == baseline_value:
            preserved_materialized_count += 1
        if field_name != "original_release_date" or (
            current_value == baseline_value
            and (row is None or row[2] == current_value)
        ):
            no_fabricated_date_count += 1
        if row is None:
            continue

        value = row[2]
        provenance = str(row[3])
        expected_provenance = (
            "unknown" if value is None or not str(value).strip() else "embedded"
        )
        timestamp = row[8]
        metadata_timestamp = current_track.get("metadata_updated_at")
        expected_timestamp = (
            metadata_timestamp
            if metadata_timestamp is not None
            else current_track.get("updated_at")
        )
        timestamp_safe = (
            timestamp == expected_timestamp
            if expected_timestamp is not None
            else bool(str(timestamp or "").strip())
        )
        if value == current_value and provenance == expected_provenance and timestamp_safe:
            safe_default_count += 1
        if row[4] is None and row[5] is None and int(row[6]) == 0 and int(row[7]) == 0:
            safe_authority_count += 1

    expected_count = len(expected_additions)
    per_field_additions = {
        field_name: sum(name == field_name for _track_id, name in additions)
        for field_name in V6_FIELD_EXTENSIONS
    }
    return {
        "baseline_count": len(baseline_rows),
        "baseline_preserved_count": preserved_count,
        "baseline_extension_count": baseline_extension_count,
        "addition_count": len(additions),
        "expected_addition_count": expected_count,
        "expected_additions_match": additions == expected_additions,
        "safe_default_count": safe_default_count,
        "safe_authority_count": safe_authority_count,
        "preserved_materialized_count": preserved_materialized_count,
        "no_fabricated_date_count": no_fabricated_date_count,
        "duplicate_count": duplicate_count,
        "per_field_additions": per_field_additions,
        "current_track_count": len(current_tracks),
        "baseline_track_count": len(baseline_tracks),
        "current_has_extension_columns": set(V6_FIELD_EXTENSIONS.values()) <= current_columns,
    }


def _credit_checks(
    connection: sqlite3.Connection,
    reference: sqlite3.Connection,
) -> dict[str, Any]:
    baseline_rows = reference.execute(
        "SELECT id,artist FROM tracks WHERE NULLIF(TRIM(artist),'') IS NOT NULL"
    ).fetchall()
    baseline_artists = {int(row[0]): str(row[1]) for row in baseline_rows}
    nonempty = len(baseline_artists)
    if not V6_TABLES <= _tables(connection):
        return {
            "credit_count": 0,
            "artist_count": 0,
            "normalized_identity_mismatch_count": nonempty,
            "invalid_credit_shape_count": nonempty,
            "ampersand_split_count": sum("&" in value for value in baseline_artists.values()),
            "unreferenced_artist_count": 0,
            "unexpected_credit_track_count": 0,
            "duplicate_normalized_entity_count": 0,
            "stored_normalization_mismatch_count": 0,
            "expected_unique_normalized_artist_count": len(
                {normalize_artist_name(value) for value in baseline_artists.values()}
            ),
        }
    count = lambda sql: int(connection.execute(sql).fetchone()[0])
    credit_rows = connection.execute(
        """
        SELECT c.track_id,c.artist_id,c.role,c.credit_order,c.join_phrase,
               c.provenance,a.display_name,a.normalized_name
        FROM track_artist_credits c
        JOIN artists a ON a.id=c.artist_id
        ORDER BY c.track_id,c.credit_order,c.id
        """
    ).fetchall()
    credits_by_track: dict[int, list[Sequence[object]]] = {}
    normalized_to_ids: dict[str, set[int]] = {}
    stored_normalization_mismatch_count = 0
    for row in credit_rows:
        credits_by_track.setdefault(int(row[0]), []).append(row)
        try:
            display_normalized = normalize_artist_name(row[6])
        except ValueError:
            display_normalized = ""
        stored_normalized = str(row[7])
        normalized_to_ids.setdefault(stored_normalized, set()).add(int(row[1]))
        if display_normalized != stored_normalized:
            stored_normalization_mismatch_count += 1

    normalized_identity_mismatch_count = 0
    invalid_credit_shape_count = 0
    ampersand_split_count = 0
    for track_id, display in baseline_artists.items():
        rows = credits_by_track.get(track_id, [])
        if len(rows) != 1:
            normalized_identity_mismatch_count += 1
            invalid_credit_shape_count += 1
            continue
        row = rows[0]
        try:
            baseline_normalized = normalize_artist_name(display)
            entity_normalized = normalize_artist_name(row[6])
        except ValueError:
            normalized_identity_mismatch_count += 1
            continue
        if baseline_normalized != entity_normalized or baseline_normalized != str(row[7]):
            normalized_identity_mismatch_count += 1
        if (
            str(row[2]) != "primary"
            or int(row[3]) != 0
            or str(row[4]) != ""
            or not str(row[5] or "").strip()
        ):
            invalid_credit_shape_count += 1
        if "&" in baseline_normalized and "&" not in entity_normalized:
            ampersand_split_count += 1

    baseline_track_ids = set(baseline_artists)
    return {
        "credit_count": len(credit_rows),
        "artist_count": count("SELECT COUNT(*) FROM artists"),
        "normalized_identity_mismatch_count": normalized_identity_mismatch_count,
        "invalid_credit_shape_count": invalid_credit_shape_count,
        "ampersand_split_count": ampersand_split_count,
        "unreferenced_artist_count": count(
            """
            SELECT COUNT(*) FROM artists a
            WHERE NOT EXISTS (SELECT 1 FROM track_artist_credits c WHERE c.artist_id=a.id)
            """
        ),
        "unexpected_credit_track_count": sum(
            track_id not in baseline_track_ids for track_id in credits_by_track
        ),
        "duplicate_normalized_entity_count": sum(
            len(artist_ids) > 1 for artist_ids in normalized_to_ids.values()
        ),
        "stored_normalization_mismatch_count": stored_normalization_mismatch_count,
        "expected_unique_normalized_artist_count": len(
            {normalize_artist_name(value) for value in baseline_artists.values()}
        ),
    }


def verify_migration(
    *,
    baseline: Mapping[str, Any],
    project_root: Path,
    data_dir: Path,
    database: Path,
    backup_path: Path,
) -> dict[str, Any]:
    if baseline.get("baseline_format_version") != BASELINE_FORMAT_VERSION:
        raise GateFailure("baseline_format_unsupported")
    if int(baseline["database"]["schema_version"]) != EXPECTED_PRE_SCHEMA:
        raise GateFailure("baseline_schema_is_not_5")
    current = capture_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
        baseline=baseline,
    )
    connection = _readonly(database)
    reference = _normal_readonly(backup_path)
    try:
        tables = _tables(connection)
        index_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        missing_indexes = V6_REQUIRED_INDEXES - index_names
        field_state = _field_state_checks(connection, reference)
        credit = _credit_checks(connection, reference)
        foreign_keys_enabled = int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        reference.close()
        connection.close()

    preserved = {
        table: current["preserved_tables"][table] == baseline["preserved_tables"][table]
        for table in PRESERVED_TABLES
    }
    # The v6 migration intentionally appends exactly three inert field-state
    # rows per track.  Preserve every baseline row byte-for-byte, but do not
    # misclassify those audited additions as a rewrite.
    preserved["track_metadata_fields"] = bool(
        field_state["baseline_extension_count"] == 0
        and field_state["baseline_preserved_count"] == field_state["baseline_count"]
        and field_state["expected_additions_match"]
        and field_state["safe_default_count"] == field_state["expected_addition_count"]
        and field_state["safe_authority_count"] == field_state["expected_addition_count"]
        and field_state["preserved_materialized_count"]
        == field_state["expected_addition_count"]
        and field_state["no_fabricated_date_count"]
        == field_state["expected_addition_count"]
        and field_state["duplicate_count"] == 0
        and field_state["current_track_count"] == field_state["baseline_track_count"]
        and field_state["current_has_extension_columns"]
    )
    runtime_unchanged = all(
        current["runtime_guards"][key] == baseline["runtime_guards"][key]
        for key in RUNTIME_GUARD_FILES
    )
    credentials_unchanged = current["credential_metadata"] == baseline["credential_metadata"]
    media_unchanged = current["media"] == baseline["media"]
    artist_strings_unchanged = (
        current["artist_baseline"] == baseline["artist_baseline"]
    )
    provider_before = baseline["provider_guard"]
    provider_after = current["provider_guard"]
    preexisting_provider_values_preserved = all(
        provider_after[key] == provider_before[key]
        for key in (
            "provider_observation_count",
            "provider_cache_count",
            "musicbrainz_recording_count",
            "musicbrainz_release_count",
        )
    )
    no_new_provider_activity = bool(
        provider_after["discogs_release_count"] == provider_before["discogs_release_count"] == 0
        and provider_after["discogs_master_count"] == provider_before["discogs_master_count"] == 0
        and provider_after["intelligence_job_count"] == 0
        and provider_after["intelligence_item_count"] == 0
        and provider_after["release_context_count"] == 0
    )

    explicit_verified = _verify_backup(backup_path, baseline)
    new_backups = _new_backups(data_dir / "backups", baseline)
    explicit_token = batch10_gate._backup_file_token(backup_path)
    explicit_created = any(
        batch10_gate._backup_file_token(path) == explicit_token for path in new_backups
    )
    automatic = [
        path
        for path in new_backups
        if re.fullmatch(
            r"music_vault_pre_schema_v6_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d+)?\.sqlite3",
            path.name,
        )
    ]
    verified_automatic = sum(_verify_backup(path, baseline) for path in automatic)
    status_compatible, status_private = _status_is_safe(
        data_dir / "music_vault_status.json"
    )
    sidecars_absent = all(
        not value["exists"] for value in current["sidecars"].values()
    )

    checks = {
        "schema_is_6": int(current["database"]["schema_version"]) == EXPECTED_POST_SCHEMA,
        "all_preexisting_table_rows_and_values_preserved": all(preserved.values()),
        "tracks_preserved": preserved["tracks"],
        "playlists_preserved": preserved["playlists"],
        "playlist_memberships_preserved": preserved["playlist_tracks"],
        "source_definitions_preserved": preserved["sync_sources"],
        "source_items_preserved": preserved["sync_source_items"],
        "playlist_origins_preserved": preserved["playlist_track_origins"],
        "metadata_history_and_remediation_preserved": all(
            preserved[name]
            for name in (
                "track_metadata_fields",
                "track_metadata_observations",
                "track_metadata_history",
                "metadata_remediation_jobs",
                "metadata_remediation_items",
            )
        ),
        "all_baseline_field_state_rows_preserved_byte_identically": (
            field_state["baseline_preserved_count"] == field_state["baseline_count"]
        ),
        "exactly_three_safe_v6_field_rows_added_per_track": (
            field_state["baseline_extension_count"] == 0
            and field_state["expected_additions_match"]
            and field_state["safe_default_count"] == field_state["expected_addition_count"]
        ),
        "v6_field_rows_have_no_fabricated_provider_manual_or_lock": (
            field_state["safe_authority_count"] == field_state["expected_addition_count"]
        ),
        "v6_field_rows_do_not_fabricate_canonical_dates_or_versions": (
            field_state["preserved_materialized_count"]
            == field_state["expected_addition_count"]
            and field_state["no_fabricated_date_count"]
            == field_state["expected_addition_count"]
            and field_state["safe_default_count"]
            == field_state["expected_addition_count"]
        ),
        "no_duplicate_track_field_state": field_state["duplicate_count"] == 0,
        "artist_display_strings_preserved": artist_strings_unchanged,
        "one_conservative_credit_per_nonempty_artist": (
            credit["credit_count"] == int(baseline["artist_baseline"]["nonempty_count"])
            and credit["normalized_identity_mismatch_count"] == 0
            and credit["invalid_credit_shape_count"] == 0
            and credit["unexpected_credit_track_count"] == 0
        ),
        "artist_credit_normalized_identity_matches_track_display": (
            credit["normalized_identity_mismatch_count"] == 0
            and credit["stored_normalization_mismatch_count"] == 0
        ),
        "normalized_artist_entity_reuse_is_deterministic": (
            credit["duplicate_normalized_entity_count"] == 0
            and credit["artist_count"] == credit["expected_unique_normalized_artist_count"]
        ),
        "ampersand_names_not_split": credit["ampersand_split_count"] == 0,
        "no_label_or_unrelated_artist_fabricated": (
            credit["unreferenced_artist_count"] == 0
            and credit["normalized_identity_mismatch_count"] == 0
            and credit["unexpected_credit_track_count"] == 0
        ),
        "preexisting_provider_values_preserved": preexisting_provider_values_preserved,
        "no_provider_lookup_or_intelligence_job_ran": no_new_provider_activity,
        "required_v6_tables_present": V6_TABLES <= tables,
        "required_indexes_present": not missing_indexes,
        "foreign_keys_enabled": foreign_keys_enabled,
        "foreign_key_check_clean": foreign_keys == 0,
        "integrity_ok": integrity.casefold() == "ok",
        "extra_rollback_backup_created": explicit_created,
        "extra_rollback_backup_verified": explicit_verified,
        "automatic_schema_backup_created_and_verified": verified_automatic >= 1,
        "runtime_config_archive_failure_files_unchanged": runtime_unchanged,
        "credential_file_metadata_unchanged_without_reading_contents": credentials_unchanged,
        "media_content_and_timestamps_unchanged": media_unchanged,
        "app_status_compatible": status_compatible,
        "app_status_private_and_no_job_activity": status_private,
        "sqlite_sidecars_absent": sidecars_absent,
        "packaged_data_folder_absent": not current["packaged_data_folder_exists"],
    }
    return {
        "baseline_format_version": BASELINE_FORMAT_VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "tracks": int(current["database"]["track_count"]),
            "playlists": int(current["database"]["playlist_count"]),
            "playlist_memberships": int(current["database"]["membership_count"]),
            "source_definitions": int(current["database"]["sources"]["saved_count"]),
            "source_items": int(current["database"]["sources"]["item_count"]),
            "playlist_origins": int(current["database"]["origin_count"]),
            "nonempty_artist_strings": int(baseline["artist_baseline"]["nonempty_count"]),
            "seeded_artist_credits": int(credit["credit_count"]),
            "seeded_artist_entities": int(credit["artist_count"]),
            "preserved_baseline_field_state_rows": int(
                field_state["baseline_preserved_count"]
            ),
            "baseline_field_state_rows": int(field_state["baseline_count"]),
            "expected_new_v6_field_state_rows": int(
                field_state["expected_addition_count"]
            ),
            "actual_new_v6_field_state_rows": int(field_state["addition_count"]),
            "new_original_release_date_field_rows": int(
                field_state["per_field_additions"]["original_release_date"]
            ),
            "new_version_type_field_rows": int(
                field_state["per_field_additions"]["version_type"]
            ),
            "new_version_label_field_rows": int(
                field_state["per_field_additions"]["version_label"]
            ),
            "new_backup_file_count": len(new_backups),
            "automatic_backup_candidate_count": len(automatic),
            "verified_automatic_backup_count": verified_automatic,
            "provider_observation_count": int(provider_after["provider_observation_count"]),
            "intelligence_job_count": int(provider_after["intelligence_job_count"]),
            "intelligence_item_count": int(provider_after["intelligence_item_count"]),
            "missing_required_index_count": len(missing_indexes),
        },
        "database_file": {
            "before": {
                "sha256": baseline["database"]["sha256"],
                "size": int(baseline["database"]["size"]),
                "mtime_ns": int(baseline["database"]["mtime_ns"]),
            },
            "after": {
                "sha256": current["database"]["sha256"],
                "size": int(current["database"]["size"]),
                "mtime_ns": int(current["database"]["mtime_ns"]),
            },
        },
    }


def _music_vault_running() -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq MusicVault.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return "musicvault.exe" in completed.stdout.casefold()


def _execution_guard(acknowledgement: str) -> None:
    if acknowledgement != ACKNOWLEDGEMENT:
        raise GateFailure("live_library_acknowledgement_required")
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() != "1":
        raise GateFailure("acceptance_no_secrets_guard_required")
    if _music_vault_running():
        raise GateFailure("music_vault_process_must_be_closed")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateFailure("baseline_unavailable") from exc
    if not isinstance(value, dict):
        raise GateFailure("baseline_invalid")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "create-backup", "verify"))
    parser.add_argument("--acknowledge-live-library", required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    _execution_guard(args.acknowledge_live_library)
    root = args.project_root.expanduser().resolve()
    data = (args.data_dir or root / "data").expanduser().resolve()
    database = (args.database or data / "music_vault.sqlite3").expanduser().resolve()
    if not database.is_file():
        raise GateFailure("database_unavailable")

    if args.mode == "baseline":
        result = capture_baseline(project_root=root, data_dir=data, database=database)
        if int(result["database"]["schema_version"]) != EXPECTED_PRE_SCHEMA:
            raise GateFailure("live_database_schema_is_not_5")
    else:
        if args.baseline is None:
            raise GateFailure("baseline_required")
        baseline = _load_json(args.baseline.expanduser().resolve())
        if args.mode == "create-backup":
            requested = args.backup.expanduser().resolve() if args.backup else None
            result = create_verified_backup(
                database=database,
                backup_dir=data / "backups",
                baseline=baseline,
                backup_path=requested,
            )
        else:
            if args.backup is None:
                raise GateFailure("rollback_backup_required")
            result = verify_migration(
                baseline=baseline,
                project_root=root,
                data_dir=data,
                database=database,
                backup_path=args.backup.expanduser().resolve(),
            )
    if args.output is not None:
        _write_json(args.output.expanduser().resolve(), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
    except (GateFailure, OSError, sqlite3.Error, TypeError, ValueError):
        print(json.dumps({"ok": False, "error_code": "batch10_1_live_gate_failed"}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
