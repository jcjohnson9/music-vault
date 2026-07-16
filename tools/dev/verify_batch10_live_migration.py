from __future__ import annotations

"""Aggregate-only safety gate for the controlled Batch 10 live migration.

The baseline path deliberately uses SQLite's immutable, read-only URI mode and
never constructs ``MusicVaultDB``.  This keeps merely measuring the live
library from triggering application migrations.  The command emits hashes and
counts only: no track, playlist, source, or filesystem names are included.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, urlparse


EXPECTED_SCHEMA_VERSION = 6
BASELINE_FORMAT_VERSION = 1
STATUS_SCHEMA_VERSION = 1

CORE_COUNT_TABLES = (
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
)
METADATA_COUNT_TABLES = (
    "track_metadata_fields",
    "track_metadata_observations",
    "track_metadata_history",
    "metadata_remediation_jobs",
    "metadata_remediation_items",
    "metadata_provider_cache",
)
SCHEMA_V6_FIELD_STATES = (
    "original_release_date",
    "version_type",
    "version_label",
)
REQUIRED_BATCH10_INDEXES = frozenset(
    {
        "idx_sync_sources_active_order",
        "idx_sync_sources_enabled_order",
        "uq_sync_sources_active_destination",
        "idx_sync_source_items_source",
        "idx_sync_source_items_source_position",
        "idx_sync_source_items_source_video",
        "idx_sync_source_items_video",
        "idx_sync_source_items_track",
        "idx_sync_source_items_present",
        "idx_sync_source_items_removed",
        "idx_source_track_identities_track",
        "idx_source_identity_conflicts_open",
        "idx_source_identity_conflicts_canonical",
        "uq_playlist_origins_manual",
        "uq_playlist_origins_source",
        "idx_playlist_origins_playlist_order",
        "idx_playlist_origins_source",
        "idx_playlist_origins_track",
        "idx_sync_source_runs_source_recent",
        "idx_sync_source_runs_batch",
        "idx_sync_source_runs_status",
        "idx_sync_failures_source_status",
        "idx_sync_failures_source_item",
    }
)
GUARD_FILE_NAMES = {
    "config": "music_vault_config.json",
    "status": "music_vault_status.json",
    "download_archive": "youtube_download_archive.txt",
    "failed_ids": "youtube_failed_ids.txt",
}
STATUS_TOP_LEVEL_FIELDS = frozenset(
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
        "paths",
    }
)
STATUS_SECTION_FIELDS = {
    "health": frozenset({"ok", "api_ready", "ffmpeg_ready"}),
    "library": frozenset(
        {
            "track_count",
            "playlist_count",
            "album_count",
            "artist_count",
            "missing_track_count",
        }
    ),
    # These are the established playback identity/state fields.  In
    # particular, current_title/current_artist/current_album are intentionally
    # allowed even though source/item title fields are forbidden below.
    "playback": frozenset(
        {
            "currently_playing",
            "current_title",
            "current_artist",
            "current_album",
            "is_playing",
            "shuffle_enabled",
            "autoplay_enabled",
            "repeat_mode",
            "queue_count",
        }
    ),
    "paths": frozenset(
        {
            "project_root",
            "data_dir",
            "database",
            "downloads",
            "config",
            "status_file",
            "path_resolution_source",
        }
    ),
}
AGGREGATE_SYNC_STATUS_FIELDS = frozenset(
    {
        "last_sync_at",
        "last_sync_status",
        "last_sync_new_items",
        "last_sync_imported_count",
        "last_sync_visible_item_count",
        "last_sync_downloaded_count",
        "last_sync_existing_count",
        "last_sync_failed_count",
        "sync_source_count",
        "enabled_sync_source_count",
        "active_sync_batch",
        "active_sync_source_index",
        "last_sync_batch_status",
        "last_sync_batch_source_count",
        "last_sync_batch_complete_count",
        "last_sync_batch_issue_count",
        "last_sync_batch_failed_count",
        "last_sync_batch_downloaded_count",
        "last_sync_batch_imported_count",
        "last_sync_batch_item_failure_count",
    }
)
# Kept in schema version 1 only for backwards compatibility.  Batch 10 must
# leave every identity/detail value empty rather than exporting it.
EMPTY_LEGACY_SYNC_STATUS_FIELDS = frozenset(
    {
        "last_sync_playlist_title",
        "last_sync_playlist_id",
        "last_sync_error",
        "last_sync_failures",
    }
)
ALLOWED_SYNC_STATUS_FIELDS = (
    AGGREGATE_SYNC_STATUS_FIELDS | EMPTY_LEGACY_SYNC_STATUS_FIELDS
)
LEGACY_PLAYLIST_CONFIG_KEYS = (
    "youtube_playlist_url",
    "youtube_sync_playlist_url",
    "playlist_url",
)
YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,150}$")


class GateFailure(RuntimeError):
    """A deliberately detail-free acceptance failure."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _aggregate_digest(records: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(record.encode("ascii", errors="strict"))
        digest.update(b"\n")
    return digest.hexdigest()


def _readonly_connection(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise GateFailure("database_unavailable")
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except sqlite3.Error as exc:
        raise GateFailure("database_read_failed") from exc


def _normal_readonly_connection(database: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except sqlite3.Error as exc:
        raise GateFailure("database_read_failed") from exc


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in _tables(connection):
        return set()
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quoted_identifier(table)})"
        )
    }


def _count(connection: sqlite3.Connection, table: str, where: str = "") -> int:
    if table not in _tables(connection):
        return 0
    suffix = f" WHERE {where}" if where else ""
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quoted_identifier(table)}{suffix}"
    ).fetchone()
    return int(row[0]) if row else 0


def _all_table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: _count(connection, table)
        for table in sorted(_tables(connection))
    }


def _playlist_order_digest(connection: sqlite3.Connection) -> str:
    if "playlist_tracks" not in _tables(connection):
        return _aggregate_digest(())
    records = (
        f"{int(row[0])}:{int(row[1])}:{int(row[2])}"
        for row in connection.execute(
            "SELECT playlist_id, track_id, position FROM playlist_tracks "
            "ORDER BY playlist_id, position, track_id"
        )
    )
    return _aggregate_digest(records)


def _database_summary(connection: sqlite3.Connection, database: Path) -> dict[str, Any]:
    stat = database.stat()
    tables = _tables(connection)
    counts = {
        table: _count(connection, table)
        for table in (*CORE_COUNT_TABLES, *METADATA_COUNT_TABLES)
    }
    metadata = {
        table: counts[table]
        for table in METADATA_COUNT_TABLES
    }
    fields_columns = _columns(connection, "track_metadata_fields")
    metadata.update(
        {
            "schema_v6_field_state_count": (
                _count(
                    connection,
                    "track_metadata_fields",
                    "field_name IN ('original_release_date','version_type','version_label')",
                )
                if "field_name" in fields_columns
                else 0
            ),
            "manual_field_count": (
                _count(connection, "track_metadata_fields", "is_manual=1")
                if "is_manual" in fields_columns
                else 0
            ),
            "locked_field_count": (
                _count(connection, "track_metadata_fields", "is_locked=1")
                if "is_locked" in fields_columns
                else 0
            ),
        }
    )
    remediation_columns = _columns(connection, "metadata_remediation_items")
    metadata.update(
        {
            "applied_remediation_count": (
                _count(connection, "metadata_remediation_items", "status='applied'")
                if "status" in remediation_columns
                else 0
            ),
            "verified_media_write_count": (
                _count(
                    connection,
                    "metadata_remediation_items",
                    "file_write_status='verified'",
                )
                if "file_write_status" in remediation_columns
                else 0
            ),
            "rolled_back_remediation_count": (
                _count(connection, "metadata_remediation_items", "status='rolled_back'")
                if "status" in remediation_columns
                else 0
            ),
        }
    )
    identity_expectations = _identity_expectations(connection)
    source_counts = {
        "saved_count": counts["sync_sources"],
        "active_count": _count(connection, "sync_sources", "archived_at IS NULL"),
        "enabled_count": _count(
            connection,
            "sync_sources",
            "archived_at IS NULL AND enabled=1",
        ),
        "item_count": counts["sync_source_items"],
        "identity_count": counts["source_track_identities"],
        "identity_conflict_count": counts["source_identity_conflicts"],
        "open_identity_conflict_count": _count(
            connection,
            "source_identity_conflicts",
            "resolved_at IS NULL",
        ),
        "run_count": counts["sync_source_runs"],
        "library_destination_count": _count(
            connection,
            "sync_sources",
            "destination_kind='library'",
        ),
        "managed_destination_count": _count(
            connection,
            "sync_sources",
            "destination_kind='playlist'",
        ),
        "manual_origin_count": _count(
            connection,
            "playlist_track_origins",
            "origin_kind='manual'",
        ),
        "managed_origin_count": _count(
            connection,
            "playlist_track_origins",
            "origin_kind='sync_source'",
        ),
        **identity_expectations,
    }
    connection.execute("PRAGMA foreign_keys=ON")
    foreign_keys_enabled = int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    foreign_key_issue_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
    integrity_ok = bool(integrity_row and str(integrity_row[0]).casefold() == "ok")
    return {
        "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "sha256": _sha256_file(database),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "table_counts": _all_table_counts(connection),
        "track_count": counts["tracks"],
        "playlist_count": counts["playlists"],
        "membership_count": counts["playlist_tracks"],
        "origin_count": counts["playlist_track_origins"],
        "playlist_order_digest": _playlist_order_digest(connection),
        "metadata": metadata,
        "sources": source_counts,
        "foreign_keys_enabled": foreign_keys_enabled,
        "foreign_key_issue_count": foreign_key_issue_count,
        "integrity_ok": integrity_ok,
    }


def _identity_expectations(connection: sqlite3.Connection) -> dict[str, Any]:
    """Compute deterministic identity expectations without exposing identities."""

    tables = _tables(connection)
    track_columns = _columns(connection, "tracks")
    required = {"id", "path", "source_kind", "source_video_id"}
    if "tracks" not in tables or not required <= track_columns:
        empty = _aggregate_digest(())
        return {
            "youtube_identity_claim_count": 0,
            "unique_youtube_identity_count": 0,
            "duplicate_youtube_identity_claim_count": 0,
            "expected_identity_count": _count(connection, "source_track_identities"),
            "expected_identity_conflict_count": _count(
                connection, "source_identity_conflicts"
            ),
            "expected_identity_mapping_digest": empty,
            "actual_identity_mapping_digest": empty,
        }

    claims: dict[str, list[tuple[int, bool]]] = {}
    for row in connection.execute(
        """
        SELECT id, path, trim(source_video_id)
        FROM tracks
        WHERE lower(trim(COALESCE(source_kind, '')))='youtube'
          AND length(trim(COALESCE(source_video_id, ''))) > 0
        ORDER BY source_video_id, id
        """
    ):
        identity = str(row[2])
        try:
            exists = Path(str(row[1])).is_file()
        except (OSError, TypeError, ValueError):
            exists = False
        claims.setdefault(identity, []).append((int(row[0]), exists))

    existing_mappings: dict[tuple[str, str], int] = {}
    if "source_track_identities" in tables:
        for row in connection.execute(
            "SELECT source_kind, external_track_id, track_id "
            "FROM source_track_identities"
        ):
            existing_mappings[(str(row[0]), str(row[1]))] = int(row[2])

    expected_mappings = dict(existing_mappings)
    expected_conflicts: set[tuple[str, str, int]] = set()
    if "source_identity_conflicts" in tables:
        expected_conflicts.update(
            (str(row[0]), str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT source_kind, external_track_id, conflicting_track_id "
                "FROM source_identity_conflicts"
            )
        )
    for identity, grouped_claims in claims.items():
        claim_ids = {track_id for track_id, _exists in grouped_claims}
        existing_track = existing_mappings.get(("youtube", identity))
        if existing_track in claim_ids:
            canonical = int(existing_track)
        else:
            canonical = min(
                grouped_claims,
                key=lambda claim: (0 if claim[1] else 1, claim[0]),
            )[0]
        expected_mappings[("youtube", identity)] = canonical
        for track_id, _exists in grouped_claims:
            if track_id != canonical:
                expected_conflicts.add(("youtube", identity, track_id))

    def mapping_digest(mappings: dict[tuple[str, str], int]) -> str:
        return _aggregate_digest(
            f"{hashlib.sha256(f'{kind}:{identity}'.encode('utf-8')).hexdigest()}:{track_id}"
            for (kind, identity), track_id in mappings.items()
        )

    actual_digest = mapping_digest(existing_mappings)
    claim_count = sum(len(group) for group in claims.values())
    return {
        "youtube_identity_claim_count": claim_count,
        "unique_youtube_identity_count": len(claims),
        "duplicate_youtube_identity_claim_count": claim_count - len(claims),
        "expected_identity_count": len(expected_mappings),
        "expected_identity_conflict_count": len(expected_conflicts),
        "expected_identity_mapping_digest": mapping_digest(expected_mappings),
        "actual_identity_mapping_digest": actual_digest,
    }


def _file_guard(path: Path, *, include_digest: bool) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "size": 0, "mtime_ns": None, "sha256": None}
    stat = path.stat()
    return {
        "exists": True,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        # The API-key caller always uses include_digest=False.  Do not replace
        # this with a convenience hash of every guard file.
        "sha256": _sha256_file(path) if include_digest else None,
    }


def _valid_youtube_playlist_value(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "://" not in text:
        return PLAYLIST_ID_RE.fullmatch(text) is not None
    try:
        parsed = urlparse(text)
        if parsed.scheme.casefold() != "https":
            return False
        if parsed.username or parsed.password:
            return False
        if (parsed.hostname or "").casefold() not in YOUTUBE_HOSTS:
            return False
        if parsed.port not in (None, 443):
            return False
        identity = (parse_qs(parsed.query).get("list") or [""])[0].strip()
    except (TypeError, ValueError):
        return False
    return PLAYLIST_ID_RE.fullmatch(identity) is not None


def _has_valid_legacy_source_config(config_path: Path) -> bool:
    """Read only enough config structure to return one non-identifying bit."""

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    # Match application precedence: only the first genuinely persisted legacy
    # value is considered, and an invalid first value must not cause a later
    # key to be silently registered instead.
    for key in LEGACY_PLAYLIST_CONFIG_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _valid_youtube_playlist_value(value)
    return False


def _media_guard(connection: sqlite3.Connection) -> dict[str, Any]:
    if "tracks" not in _tables(connection) or "path" not in _columns(connection, "tracks"):
        return {
            "count": 0,
            "missing_count": 0,
            "total_size": 0,
            "latest_mtime_ns": None,
            "stat_digest": _aggregate_digest(()),
            "content_digest": _aggregate_digest(()),
        }

    paths = {Path(str(row[0])) for row in connection.execute("SELECT path FROM tracks")}
    stat_records: list[str] = []
    content_records: list[str] = []
    total_size = 0
    latest_mtime: int | None = None
    present = 0
    missing = 0
    for path in paths:
        try:
            if not path.is_file():
                missing += 1
                continue
            stat = path.stat()
            content_hash = _sha256_file(path)
        except OSError:
            missing += 1
            continue
        present += 1
        total_size += int(stat.st_size)
        latest_mtime = max(latest_mtime or stat.st_mtime_ns, stat.st_mtime_ns)
        stat_records.append(f"{stat.st_size}:{stat.st_mtime_ns}:{content_hash}")
        content_records.append(content_hash)
    return {
        "count": present,
        "missing_count": missing,
        "total_size": total_size,
        "latest_mtime_ns": latest_mtime,
        "stat_digest": _aggregate_digest(stat_records),
        "content_digest": _aggregate_digest(content_records),
    }


def _sidecar_guards(database: Path) -> dict[str, dict[str, Any]]:
    return {
        "wal": _file_guard(Path(f"{database}-wal"), include_digest=True),
        "shm": _file_guard(Path(f"{database}-shm"), include_digest=True),
        "journal": _file_guard(Path(f"{database}-journal"), include_digest=True),
    }


def _backup_file_token(path: Path) -> str:
    """Return a stable opaque token without exposing a backup file name."""

    return hashlib.sha256(path.name.casefold().encode("utf-8")).hexdigest()


def _backup_guard(backup_dir: Path) -> dict[str, Any]:
    files = sorted(path for path in backup_dir.glob("*.sqlite3") if path.is_file())
    records: list[str] = []
    total_size = 0
    latest_mtime: int | None = None
    for path in files:
        stat = path.stat()
        digest = _sha256_file(path)
        records.append(f"{stat.st_size}:{stat.st_mtime_ns}:{digest}")
        total_size += int(stat.st_size)
        latest_mtime = max(latest_mtime or stat.st_mtime_ns, stat.st_mtime_ns)
    return {
        "count": len(files),
        "total_size": total_size,
        "latest_mtime_ns": latest_mtime,
        "digest": _aggregate_digest(records),
        # Tokens let verification distinguish files that appeared after the
        # immutable baseline without putting any backup name or path in gate
        # output.  File contents are still validated separately below.
        "file_tokens": sorted(_backup_file_token(path) for path in files),
    }


def capture_baseline(
    *,
    project_root: Path,
    data_dir: Path,
    database: Path,
) -> dict[str, Any]:
    """Capture an aggregate-only immutable snapshot without migrating the DB."""

    connection = _readonly_connection(database)
    try:
        database_summary = _database_summary(connection, database)
        media = _media_guard(connection)
    finally:
        connection.close()

    guards = {
        key: _file_guard(data_dir / filename, include_digest=True)
        for key, filename in GUARD_FILE_NAMES.items()
    }
    # Never open or hash this file.  Only filesystem metadata is recorded.
    api_key = _file_guard(data_dir / "youtube_api_key.txt", include_digest=False)
    return {
        "baseline_format_version": BASELINE_FORMAT_VERSION,
        "database": database_summary,
        "sidecars": _sidecar_guards(database),
        "guard_files": guards,
        "api_key": api_key,
        "media": media,
        "backups": _backup_guard(data_dir / "backups"),
        "valid_legacy_source_configured": _has_valid_legacy_source_config(
            data_dir / GUARD_FILE_NAMES["config"]
        ),
        "packaged_data_folder_exists": (project_root / "dist" / "MusicVault" / "data").exists(),
    }


def _default_backup_path(backup_dir: Path) -> Path:
    candidate = backup_dir / f"music_vault_batch10_live_rollback_{_utc_stamp()}.sqlite3"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / (
            f"music_vault_batch10_live_rollback_{_utc_stamp()}_{counter}.sqlite3"
        )
        counter += 1
    return candidate


def create_verified_backup(
    *,
    database: Path,
    backup_dir: Path,
    backup_path: Path | None = None,
) -> dict[str, Any]:
    """Create and verify a rollback database using SQLite's backup API."""

    backup_dir.mkdir(parents=True, exist_ok=True)
    destination_path = (backup_path or _default_backup_path(backup_dir)).resolve()
    try:
        destination_path.relative_to(backup_dir.resolve())
    except ValueError as exc:
        raise GateFailure("backup_target_outside_backup_directory") from exc
    if destination_path.exists():
        raise GateFailure("backup_target_exists")
    if any(value["exists"] for value in _sidecar_guards(database).values()):
        raise GateFailure("database_sidecar_present")

    source = _readonly_connection(database)
    expected_counts = _all_table_counts(source)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(destination_path)
        source.backup(destination)
    except Exception:
        if destination is not None:
            destination.close()
            destination = None
        try:
            destination_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise GateFailure("backup_creation_failed")
    finally:
        if destination is not None:
            destination.close()
        source.close()

    verified = _normal_readonly_connection(destination_path)
    try:
        integrity = verified.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = bool(integrity and str(integrity[0]).casefold() == "ok")
        counts_match = _all_table_counts(verified) == expected_counts
        schema_version = int(verified.execute("PRAGMA user_version").fetchone()[0])
    finally:
        verified.close()
    if not integrity_ok or not counts_match:
        raise GateFailure("backup_verification_failed")
    stat = destination_path.stat()
    return {
        "created": True,
        "verified": True,
        "integrity_ok": True,
        "table_counts_match": True,
        "schema_version": schema_version,
        "size": int(stat.st_size),
        "sha256": _sha256_file(destination_path),
    }


def _status_is_compatible_and_private(status_path: Path) -> tuple[bool, bool]:
    if not status_path.is_file():
        return False, False
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, False
    compatible = (
        isinstance(payload, dict)
        and payload.get("schema_version") == STATUS_SCHEMA_VERSION
        and payload.get("app") == "Music Vault"
        and isinstance(payload.get("library"), dict)
        and isinstance(payload.get("playback"), dict)
        and isinstance(payload.get("sync"), dict)
    )
    if not compatible:
        return False, False

    private_detail_found = not set(payload).issubset(STATUS_TOP_LEVEL_FIELDS)
    for section, allowed_fields in STATUS_SECTION_FIELDS.items():
        values = payload.get(section)
        if values is None:
            continue
        if not isinstance(values, dict) or not set(values).issubset(allowed_fields):
            private_detail_found = True

    sync = payload["sync"]
    if not set(sync).issubset(ALLOWED_SYNC_STATUS_FIELDS):
        private_detail_found = True
    for key, value in sync.items():
        if key in EMPTY_LEGACY_SYNC_STATUS_FIELDS:
            if value not in (None, "", [], {}):
                private_detail_found = True
        elif isinstance(value, (list, dict)):
            # Aggregate Batch 10 fields are scalar.  Per-source histories,
            # item snapshots, labels, titles, folders, storage keys, and error
            # objects do not belong in this compact external document.
            private_detail_found = True
    serialized_sync = json.dumps(sync, sort_keys=True, ensure_ascii=True).casefold()
    if "youtube.com/" in serialized_sync or "youtu.be/" in serialized_sync:
        private_detail_found = True
    return True, not private_detail_found


def _structural_checks(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(connection)
    index_names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    missing_indexes = REQUIRED_BATCH10_INDEXES - index_names

    orphan_memberships = 0
    duplicate_positions = 0
    origin_orphans = 0
    uncovered_memberships = 0
    duplicate_manual_origins = 0
    duplicate_managed_origins = 0
    manual_origin_count = 0
    managed_origin_count = 0
    if {"tracks", "playlists", "playlist_tracks"} <= tables:
        orphan_memberships = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM playlist_tracks pt
                LEFT JOIN playlists p ON p.id=pt.playlist_id
                LEFT JOIN tracks t ON t.id=pt.track_id
                WHERE p.id IS NULL OR t.id IS NULL
                """
            ).fetchone()[0]
        )
        duplicate_positions = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT playlist_id, position FROM playlist_tracks
                    GROUP BY playlist_id, position HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
    if "playlist_track_origins" in tables:
        origin_orphans = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM playlist_track_origins o
                LEFT JOIN playlist_tracks pt
                  ON pt.playlist_id=o.playlist_id AND pt.track_id=o.track_id
                LEFT JOIN playlists p ON p.id=o.playlist_id
                LEFT JOIN tracks t ON t.id=o.track_id
                LEFT JOIN sync_sources s ON s.id=o.sync_source_id
                WHERE pt.track_id IS NULL OR p.id IS NULL OR t.id IS NULL
                   OR (o.origin_kind='sync_source' AND s.id IS NULL)
                """
            ).fetchone()[0]
        )
        uncovered_memberships = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM playlist_tracks pt
                WHERE NOT EXISTS (
                    SELECT 1 FROM playlist_track_origins o
                    WHERE o.playlist_id=pt.playlist_id AND o.track_id=pt.track_id
                )
                """
            ).fetchone()[0]
        )
        duplicate_manual_origins = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT playlist_id, track_id FROM playlist_track_origins
                    WHERE origin_kind='manual'
                    GROUP BY playlist_id, track_id HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        duplicate_managed_origins = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT playlist_id, track_id, sync_source_id
                    FROM playlist_track_origins WHERE origin_kind='sync_source'
                    GROUP BY playlist_id, track_id, sync_source_id HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        manual_origin_count = _count(
            connection, "playlist_track_origins", "origin_kind='manual'"
        )
        managed_origin_count = _count(
            connection, "playlist_track_origins", "origin_kind='sync_source'"
        )
    membership_count = _count(connection, "playlist_tracks")
    exact_manual_seed = (
        manual_origin_count == membership_count
        and managed_origin_count == 0
        and uncovered_memberships == 0
    )
    return {
        "required_indexes_present": not missing_indexes,
        "missing_required_index_count": len(missing_indexes),
        "orphan_membership_count": orphan_memberships,
        "duplicate_playlist_position_count": duplicate_positions,
        "origin_orphan_count": origin_orphans,
        "uncovered_membership_count": uncovered_memberships,
        "duplicate_manual_origin_count": duplicate_manual_origins,
        "duplicate_managed_origin_count": duplicate_managed_origins,
        "exact_manual_seed": exact_manual_seed,
    }


def _verify_backup_file(backup_path: Path, baseline: dict[str, Any]) -> bool:
    """Verify a backup is an exact, pre-migration copy of the baseline DB."""

    try:
        if not backup_path.is_file() or backup_path.stat().st_size <= 0:
            return False
        connection = _normal_readonly_connection(backup_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                return False
            backup_counts = _all_table_counts(connection)
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            connection.close()
    except (GateFailure, OSError, sqlite3.Error, TypeError, ValueError):
        return False
    baseline_database = baseline["database"]
    return (
        backup_counts == baseline_database["table_counts"]
        and schema_version == int(baseline_database["schema_version"])
        and schema_version < EXPECTED_SCHEMA_VERSION
    )


def _new_backup_files(backup_dir: Path, baseline: dict[str, Any]) -> list[Path]:
    """Find post-baseline backups using opaque name tokens only."""

    baseline_tokens = baseline.get("backups", {}).get("file_tokens")
    if not isinstance(baseline_tokens, list):
        # An older/incomplete baseline cannot safely prove which files are new.
        return []
    known = {str(token) for token in baseline_tokens}
    return [
        path
        for path in sorted(backup_dir.glob("*.sqlite3"))
        if path.is_file() and _backup_file_token(path) not in known
    ]


def _is_automatic_migration_backup(path: Path) -> bool:
    return (
        re.fullmatch(
            rf"music_vault_pre_schema_v{EXPECTED_SCHEMA_VERSION}_"
            r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d+)?\.sqlite3",
            path.name,
        )
        is not None
    )


def verify_migration(
    *,
    baseline: dict[str, Any],
    project_root: Path,
    data_dir: Path,
    database: Path,
    backup_path: Path | None,
) -> dict[str, Any]:
    if baseline.get("baseline_format_version") != BASELINE_FORMAT_VERSION:
        raise GateFailure("baseline_format_unsupported")
    current = capture_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
    )
    before_db = baseline["database"]
    after_db = current["database"]
    preserved_fields = (
        "track_count",
        "playlist_count",
        "membership_count",
        "playlist_order_digest",
    )
    preservation = {
        field: after_db.get(field) == before_db.get(field)
        for field in preserved_fields
    }
    before_metadata = before_db["metadata"]
    after_metadata = after_db["metadata"]
    preserved_metadata_keys = set(before_metadata) - {
        "track_metadata_fields",
        "schema_v6_field_state_count",
    }
    preexisting_metadata_preserved = all(
        after_metadata.get(key) == before_metadata.get(key)
        for key in preserved_metadata_keys
    )
    before_v6_fields = int(before_metadata.get("schema_v6_field_state_count", 0))
    after_v6_fields = int(after_metadata.get("schema_v6_field_state_count", 0))
    before_legacy_fields = int(before_metadata["track_metadata_fields"]) - before_v6_fields
    after_legacy_fields = int(after_metadata["track_metadata_fields"]) - after_v6_fields
    schema_v6_field_states_complete = (
        before_legacy_fields == after_legacy_fields
        and after_v6_fields
        == int(after_db["track_count"]) * len(SCHEMA_V6_FIELD_STATES)
    )
    runtime_guards_unchanged = all(
        current["guard_files"][key] == baseline["guard_files"][key]
        for key in ("config", "download_archive", "failed_ids")
    )
    api_key_unchanged = current["api_key"] == baseline["api_key"]
    media_unchanged = current["media"] == baseline["media"]
    sidecars_clean = all(not value["exists"] for value in current["sidecars"].values())
    status_compatible, status_aggregate_private = _status_is_compatible_and_private(
        data_dir / GUARD_FILE_NAMES["status"]
    )

    connection = _readonly_connection(database)
    try:
        structural = _structural_checks(connection)
    finally:
        connection.close()

    explicit_backup_verified = (
        _verify_backup_file(backup_path, baseline)
        if backup_path is not None
        else False
    )
    backup_dir = data_dir / "backups"
    new_backup_files = _new_backup_files(backup_dir, baseline)
    explicit_backup_resolved = backup_path.resolve() if backup_path is not None else None
    acceptance_backup_created = bool(
        explicit_backup_resolved is not None
        and any(path.resolve() == explicit_backup_resolved for path in new_backup_files)
    )
    automatic_backup_candidates = [
        path
        for path in new_backup_files
        if path.resolve() != explicit_backup_resolved
        and _is_automatic_migration_backup(path)
    ]
    verified_automatic_backup_count = sum(
        _verify_backup_file(path, baseline) for path in automatic_backup_candidates
    )
    automatic_migration_backup_created = verified_automatic_backup_count >= 1
    backup_count_increase = (
        int(current["backups"]["count"]) - int(baseline["backups"]["count"])
    )
    legacy_expected = bool(baseline.get("valid_legacy_source_configured", False))
    expected_source_count = int(before_db["sources"]["saved_count"]) + int(
        legacy_expected
    )
    source_count_matches = int(after_db["sources"]["saved_count"]) == expected_source_count
    legacy_library_source_matches = (
        int(after_db["sources"]["library_destination_count"])
        == int(before_db["sources"]["library_destination_count"]) + int(legacy_expected)
        and int(after_db["sources"]["managed_destination_count"])
        == int(before_db["sources"]["managed_destination_count"])
    )
    identity_mapping_matches = (
        int(after_db["sources"]["identity_count"])
        == int(before_db["sources"]["expected_identity_count"])
        and after_db["sources"]["actual_identity_mapping_digest"]
        == before_db["sources"]["expected_identity_mapping_digest"]
    )
    identity_conflicts_match = (
        int(after_db["sources"]["identity_conflict_count"])
        == int(before_db["sources"]["expected_identity_conflict_count"])
    )
    checks = {
        "schema_is_6": after_db["schema_version"] == EXPECTED_SCHEMA_VERSION,
        "tracks_preserved": preservation["track_count"],
        "playlists_preserved": preservation["playlist_count"],
        "memberships_preserved": preservation["membership_count"],
        "playlist_order_preserved": preservation["playlist_order_digest"],
        "metadata_history_remediation_preserved": (
            preexisting_metadata_preserved and schema_v6_field_states_complete
        ),
        "foreign_keys_enabled": after_db["foreign_keys_enabled"],
        "foreign_key_check_clean": after_db["foreign_key_issue_count"] == 0,
        "integrity_ok": after_db["integrity_ok"],
        "required_indexes_present": structural["required_indexes_present"],
        "no_orphan_memberships": structural["orphan_membership_count"] == 0,
        "no_duplicate_playlist_positions": (
            structural["duplicate_playlist_position_count"] == 0
        ),
        "no_orphan_origins": structural["origin_orphan_count"] == 0,
        "all_memberships_have_origins": structural["uncovered_membership_count"] == 0,
        "no_duplicate_origins": (
            structural["duplicate_manual_origin_count"] == 0
            and structural["duplicate_managed_origin_count"] == 0
        ),
        "existing_memberships_seeded_manual": structural[
            "exact_manual_seed"
        ],
        "source_count_matches_legacy_expectation": source_count_matches,
        "legacy_source_is_library_only": legacy_library_source_matches,
        "identity_mapping_matches_baseline_expectation": identity_mapping_matches,
        "identity_conflicts_match_baseline_expectation": identity_conflicts_match,
        "acceptance_rollback_backup_created": acceptance_backup_created,
        "acceptance_rollback_backup_verified": explicit_backup_verified,
        "automatic_migration_backup_created": automatic_migration_backup_created,
        "media_unchanged": media_unchanged,
        "api_key_metadata_unchanged": api_key_unchanged,
        "runtime_guard_files_unchanged": runtime_guards_unchanged,
        "sqlite_sidecars_absent": sidecars_clean,
        "packaged_data_folder_absent": not current["packaged_data_folder_exists"],
        "app_status_compatible": status_compatible,
        "app_status_aggregate_private": status_aggregate_private,
    }
    return {
        "baseline_format_version": BASELINE_FORMAT_VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "tracks": after_db["track_count"],
            "playlists": after_db["playlist_count"],
            "memberships": after_db["membership_count"],
            "origins": after_db["origin_count"],
            "saved_sources": after_db["sources"]["saved_count"],
            "identities": after_db["sources"]["identity_count"],
            "identity_conflicts": after_db["sources"]["identity_conflict_count"],
            "missing_required_indexes": structural["missing_required_index_count"],
            "orphan_memberships": structural["orphan_membership_count"],
            "duplicate_playlist_positions": structural[
                "duplicate_playlist_position_count"
            ],
            "backup_count_increase": backup_count_increase,
            "new_backup_file_count": len(new_backup_files),
            "automatic_backup_candidate_count": len(automatic_backup_candidates),
            "verified_automatic_backup_count": verified_automatic_backup_count,
            "unique_youtube_identity_claims": before_db["sources"][
                "unique_youtube_identity_count"
            ],
            "duplicate_youtube_identity_claims": before_db["sources"][
                "duplicate_youtube_identity_claim_count"
            ],
        },
        "database_file": {
            "before": {
                "sha256": before_db["sha256"],
                "size": before_db["size"],
                "mtime_ns": before_db["mtime_ns"],
            },
            "after": {
                "sha256": after_db["sha256"],
                "size": after_db["size"],
                "mtime_ns": after_db["mtime_ns"],
            },
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateFailure("baseline_read_failed") from exc
    if not isinstance(payload, dict):
        raise GateFailure("baseline_read_failed")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the aggregate-only Batch 10 controlled-migration gate."
    )
    parser.add_argument(
        "mode",
        choices=("baseline", "create-backup", "verify"),
    )
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    data_dir = (args.data_dir or (project_root / "data")).resolve()
    database = (args.database or (data_dir / "music_vault.sqlite3")).resolve()
    if args.mode == "baseline":
        result = capture_baseline(
            project_root=project_root,
            data_dir=data_dir,
            database=database,
        )
    elif args.mode == "create-backup":
        result = create_verified_backup(
            database=database,
            backup_dir=data_dir / "backups",
            backup_path=args.backup,
        )
    else:
        if args.baseline is None:
            raise GateFailure("baseline_required")
        result = verify_migration(
            baseline=_load_json(args.baseline),
            project_root=project_root,
            data_dir=data_dir,
            database=database,
            backup_path=args.backup,
        )
    if args.output is not None:
        _write_json(args.output, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok", True) else 1
    except (GateFailure, OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        # Never include exception text: filesystem/database errors can echo a
        # private path or a value from the live library.
        print('{"error_code":"batch10_live_migration_gate_failed","ok":false}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
