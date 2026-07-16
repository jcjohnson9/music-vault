from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SYNC_SOURCES_TABLE = "sync_sources"
SYNC_SOURCE_ITEMS_TABLE = "sync_source_items"
SOURCE_TRACK_IDENTITIES_TABLE = "source_track_identities"
SOURCE_IDENTITY_CONFLICTS_TABLE = "source_identity_conflicts"
PLAYLIST_TRACK_ORIGINS_TABLE = "playlist_track_origins"
SYNC_SOURCE_RUNS_TABLE = "sync_source_runs"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def create_sync_schema(conn: sqlite3.Connection) -> None:
    """Create the additive, runtime-only Batch 10 synchronization schema."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SYNC_SOURCES_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind TEXT NOT NULL
                CHECK (source_kind = 'youtube_playlist'),
            external_id TEXT NOT NULL CHECK (length(trim(external_id)) > 0),
            source_url TEXT NOT NULL CHECK (length(trim(source_url)) > 0),
            label TEXT,
            remote_title TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
            destination_kind TEXT NOT NULL DEFAULT 'library'
                CHECK (destination_kind IN ('library', 'playlist')),
            destination_playlist_id INTEGER,
            storage_key TEXT NOT NULL
                CHECK (length(trim(storage_key)) BETWEEN 1 AND 96),
            last_sync_at TEXT,
            last_sync_status TEXT
                CHECK (
                    last_sync_status IS NULL
                    OR last_sync_status IN ('complete', 'complete_with_issues', 'failed')
                ),
            last_visible_count INTEGER CHECK (last_visible_count IS NULL OR last_visible_count >= 0),
            last_new_count INTEGER CHECK (last_new_count IS NULL OR last_new_count >= 0),
            last_downloaded_count INTEGER
                CHECK (last_downloaded_count IS NULL OR last_downloaded_count >= 0),
            last_imported_count INTEGER
                CHECK (last_imported_count IS NULL OR last_imported_count >= 0),
            last_existing_count INTEGER
                CHECK (last_existing_count IS NULL OR last_existing_count >= 0),
            last_failed_count INTEGER CHECK (last_failed_count IS NULL OR last_failed_count >= 0),
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE (source_kind, external_id),
            UNIQUE (storage_key),
            CHECK (
                (destination_kind = 'library' AND destination_playlist_id IS NULL)
                OR
                (destination_kind = 'playlist' AND destination_playlist_id IS NOT NULL)
            ),
            FOREIGN KEY (destination_playlist_id) REFERENCES playlists(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SYNC_SOURCE_ITEMS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            source_item_id TEXT NOT NULL CHECK (length(trim(source_item_id)) > 0),
            video_id TEXT,
            source_position INTEGER CHECK (source_position IS NULL OR source_position >= 0),
            source_title TEXT,
            availability_status TEXT NOT NULL
                CHECK (length(trim(availability_status)) > 0),
            track_id INTEGER,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            removed_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source_id, source_item_id),
            FOREIGN KEY (source_id) REFERENCES sync_sources(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SOURCE_TRACK_IDENTITIES_TABLE} (
            source_kind TEXT NOT NULL CHECK (length(trim(source_kind)) > 0),
            external_track_id TEXT NOT NULL CHECK (length(trim(external_track_id)) > 0),
            track_id INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_kind, external_track_id),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SOURCE_IDENTITY_CONFLICTS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind TEXT NOT NULL CHECK (length(trim(source_kind)) > 0),
            external_track_id TEXT NOT NULL CHECK (length(trim(external_track_id)) > 0),
            canonical_track_id INTEGER NOT NULL,
            conflicting_track_id INTEGER NOT NULL,
            reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            CHECK (canonical_track_id <> conflicting_track_id),
            UNIQUE (source_kind, external_track_id, conflicting_track_id),
            FOREIGN KEY (canonical_track_id) REFERENCES tracks(id) ON DELETE CASCADE,
            FOREIGN KEY (conflicting_track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PLAYLIST_TRACK_ORIGINS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            origin_kind TEXT NOT NULL CHECK (origin_kind IN ('manual', 'sync_source')),
            sync_source_id INTEGER,
            origin_position INTEGER NOT NULL DEFAULT 0 CHECK (origin_position >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (origin_kind = 'manual' AND sync_source_id IS NULL)
                OR
                (origin_kind = 'sync_source' AND sync_source_id IS NOT NULL)
            ),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
            FOREIGN KEY (sync_source_id) REFERENCES sync_sources(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SYNC_SOURCE_RUNS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            batch_token TEXT NOT NULL CHECK (length(trim(batch_token)) > 0),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('complete', 'complete_with_issues', 'failed')),
            visible_item_count INTEGER NOT NULL DEFAULT 0 CHECK (visible_item_count >= 0),
            new_item_count INTEGER NOT NULL DEFAULT 0 CHECK (new_item_count >= 0),
            downloaded_count INTEGER NOT NULL DEFAULT 0 CHECK (downloaded_count >= 0),
            imported_count INTEGER NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
            existing_count INTEGER NOT NULL DEFAULT 0 CHECK (existing_count >= 0),
            failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
            removed_count INTEGER NOT NULL DEFAULT 0 CHECK (removed_count >= 0),
            duplicate_occurrence_count INTEGER NOT NULL DEFAULT 0
                CHECK (duplicate_occurrence_count >= 0),
            first_error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sync_sources(id) ON DELETE CASCADE
        )
        """
    )

    failure_columns = _columns(conn, "sync_failures")
    if "sync_source_id" not in failure_columns:
        conn.execute(
            "ALTER TABLE sync_failures ADD COLUMN sync_source_id INTEGER "
            "REFERENCES sync_sources(id) ON DELETE SET NULL"
        )
    if "source_item_id" not in failure_columns:
        conn.execute("ALTER TABLE sync_failures ADD COLUMN source_item_id TEXT")

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_sync_sources_active_order "
        "ON sync_sources(archived_at, sort_order, id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_sources_enabled_order "
        "ON sync_sources(enabled, archived_at, sort_order, id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_sources_active_destination "
        "ON sync_sources(destination_playlist_id) "
        "WHERE archived_at IS NULL AND destination_kind='playlist'",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_source "
        "ON sync_source_items(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_source_position "
        "ON sync_source_items(source_id, source_position, id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_source_video "
        "ON sync_source_items(source_id, video_id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_video "
        "ON sync_source_items(video_id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_track "
        "ON sync_source_items(track_id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_present "
        "ON sync_source_items(source_id, source_position, id) WHERE removed_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_items_removed "
        "ON sync_source_items(source_id, removed_at) WHERE removed_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_source_track_identities_track "
        "ON source_track_identities(track_id)",
        "CREATE INDEX IF NOT EXISTS idx_source_identity_conflicts_open "
        "ON source_identity_conflicts(resolved_at, source_kind, external_track_id)",
        "CREATE INDEX IF NOT EXISTS idx_source_identity_conflicts_canonical "
        "ON source_identity_conflicts(canonical_track_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_playlist_origins_manual "
        "ON playlist_track_origins(playlist_id, track_id) WHERE origin_kind='manual'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_playlist_origins_source "
        "ON playlist_track_origins(playlist_id, track_id, sync_source_id) "
        "WHERE origin_kind='sync_source'",
        "CREATE INDEX IF NOT EXISTS idx_playlist_origins_playlist_order "
        "ON playlist_track_origins(playlist_id, origin_kind, origin_position, id)",
        "CREATE INDEX IF NOT EXISTS idx_playlist_origins_source "
        "ON playlist_track_origins(sync_source_id, playlist_id, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_playlist_origins_track "
        "ON playlist_track_origins(track_id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_runs_source_recent "
        "ON sync_source_runs(source_id, started_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_runs_batch "
        "ON sync_source_runs(batch_token, id)",
        "CREATE INDEX IF NOT EXISTS idx_sync_source_runs_status "
        "ON sync_source_runs(status, started_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sync_failures_source_status "
        "ON sync_failures(sync_source_id, status, last_attempt_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sync_failures_source_item "
        "ON sync_failures(sync_source_id, source_item_id)",
    ):
        conn.execute(statement)


def seed_existing_playlist_origins(conn: sqlite3.Connection) -> int:
    """Classify pre-v5 materialized memberships as manual without reordering them."""

    timestamp = utc_now()
    before = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {PLAYLIST_TRACK_ORIGINS_TABLE} "
            "WHERE origin_kind='manual'"
        ).fetchone()[0]
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {PLAYLIST_TRACK_ORIGINS_TABLE} (
            playlist_id, track_id, origin_kind, sync_source_id,
            origin_position, created_at, updated_at
        )
        SELECT playlist_id, track_id, 'manual', NULL, position, ?, ?
        FROM playlist_tracks
        ORDER BY playlist_id, position, track_id
        """,
        (timestamp, timestamp),
    )
    after = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {PLAYLIST_TRACK_ORIGINS_TABLE} "
            "WHERE origin_kind='manual'"
        ).fetchone()[0]
    )
    return after - before


def _track_file_exists(path: object) -> bool:
    try:
        return Path(str(path)).is_file()
    except (OSError, TypeError, ValueError):
        return False


def backfill_source_track_identities(conn: sqlite3.Connection) -> tuple[int, int]:
    """Backfill canonical YouTube identities without merging or deleting tracks."""

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, path, source_video_id
        FROM tracks
        WHERE lower(trim(COALESCE(source_kind, '')))='youtube'
          AND length(trim(COALESCE(source_video_id, ''))) > 0
        ORDER BY source_video_id, id
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["source_video_id"]).strip(), []).append(row)

    timestamp = utc_now()
    mapping_count = 0
    conflict_count = 0
    for external_id in sorted(grouped, key=str.casefold):
        claims = grouped[external_id]
        existing = conn.execute(
            f"""
            SELECT track_id FROM {SOURCE_TRACK_IDENTITIES_TABLE}
            WHERE source_kind='youtube' AND external_track_id=?
            """,
            (external_id,),
        ).fetchone()
        claim_ids = {int(row["id"]) for row in claims}
        if existing is not None and int(existing[0]) in claim_ids:
            canonical_id = int(existing[0])
        else:
            canonical = min(
                claims,
                key=lambda row: (
                    0 if _track_file_exists(row["path"]) else 1,
                    int(row["id"]),
                ),
            )
            canonical_id = int(canonical["id"])
        conn.execute(
            f"""
            INSERT INTO {SOURCE_TRACK_IDENTITIES_TABLE} (
                source_kind, external_track_id, track_id, first_seen_at, updated_at
            ) VALUES ('youtube', ?, ?, ?, ?)
            ON CONFLICT(source_kind, external_track_id) DO UPDATE SET
                track_id=excluded.track_id,
                updated_at=excluded.updated_at
            """,
            (external_id, canonical_id, timestamp, timestamp),
        )
        mapping_count += 1

        for row in claims:
            conflicting_id = int(row["id"])
            if conflicting_id == canonical_id:
                continue
            conn.execute(
                f"""
                INSERT INTO {SOURCE_IDENTITY_CONFLICTS_TABLE} (
                    source_kind, external_track_id, canonical_track_id,
                    conflicting_track_id, reason, created_at, resolved_at
                ) VALUES ('youtube', ?, ?, ?, 'duplicate_existing_source_identity', ?, NULL)
                ON CONFLICT(source_kind, external_track_id, conflicting_track_id)
                DO UPDATE SET canonical_track_id=excluded.canonical_track_id
                """,
                (external_id, canonical_id, conflicting_id, timestamp),
            )
            conflict_count += 1
    return mapping_count, conflict_count


def required_sync_indexes() -> Iterable[str]:
    return (
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
    )
