from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .paths import database_path
from .safety import extract_source_video_id, normalize_source_upload_date, sanitize_error_text


CURRENT_SCHEMA_VERSION = 2
_LEGACY_FAILURE_IMPORT_KEY = "legacy_failure_file_imported_v2"
_VALID_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MusicVaultDB:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backup_dir: str | Path | None = None,
        youtube_download_root: str | Path | None = None,
        legacy_failure_file: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else database_path()
        self.backup_dir = (
            Path(backup_dir) if backup_dir is not None else self.db_path.parent / "backups"
        )
        self.youtube_download_root = (
            Path(youtube_download_root).expanduser().resolve()
            if youtube_download_root is not None
            else None
        )
        self.last_migration_backup: Path | None = None

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

        if legacy_failure_file is not None:
            self.import_legacy_failures(legacy_failure_file)

    def close(self) -> None:
        self.conn.close()

    def _table_names(self) -> set[str]:
        return {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _column_names(self, table: str) -> set[str]:
        return {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _has_user_data(self) -> bool:
        tables = self._table_names()
        for table in ("tracks", "playlists", "playlist_tracks"):
            if table in tables:
                row = self.conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                if row is not None:
                    return True
        return False

    def _create_pre_migration_backup(self, target_version: int) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        candidate = self.backup_dir / (
            f"music_vault_pre_schema_v{target_version}_{timestamp}.sqlite3"
        )
        counter = 1
        while candidate.exists():
            candidate = self.backup_dir / (
                f"music_vault_pre_schema_v{target_version}_{timestamp}_{counter}.sqlite3"
            )
            counter += 1

        destination = sqlite3.connect(candidate)
        try:
            self.conn.backup(destination)
        finally:
            destination.close()

        self.last_migration_backup = candidate
        return candidate

    def _create_base_tables(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                title TEXT,
                artist TEXT,
                album TEXT,
                album_artist TEXT,
                year TEXT,
                duration_seconds REAL,
                cover_path TEXT,
                source_url TEXT,
                musicbrainz_recording_id TEXT,
                musicbrainz_release_id TEXT,
                source_kind TEXT,
                source_video_id TEXT,
                source_upload_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (playlist_id, track_id),
                FOREIGN KEY (playlist_id) REFERENCES playlists(id),
                FOREIGN KEY (track_id) REFERENCES tracks(id)
            )
        """)

    def _add_track_source_columns(self) -> None:
        columns = self._column_names("tracks")
        for column, definition in (
            ("source_kind", "TEXT"),
            ("source_video_id", "TEXT"),
            ("source_upload_date", "TEXT"),
        ):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {definition}")

    def _create_support_tables_and_indexes(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id TEXT NOT NULL,
                playlist_title TEXT,
                video_id TEXT NOT NULL,
                title TEXT,
                reason TEXT NOT NULL,
                error_category TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                first_attempt_at TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unresolved',
                resolved_at TEXT,
                UNIQUE (playlist_id, video_id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_source_video_id ON tracks(source_video_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_source_kind ON tracks(source_kind)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_failures_status ON sync_failures(status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_failures_video_id ON sync_failures(video_id)"
        )

    def _path_is_in_youtube_root(self, value: str) -> bool:
        if self.youtube_download_root is None:
            return False
        try:
            return Path(value).expanduser().resolve().is_relative_to(self.youtube_download_root)
        except Exception:
            return False

    def _backfill_youtube_source_fields(self) -> None:
        rows = self.conn.execute("""
            SELECT id, path, year, musicbrainz_recording_id, musicbrainz_release_id,
                   source_kind, source_video_id, source_upload_date
            FROM tracks
        """).fetchall()

        for row in rows:
            video_id = row["source_video_id"] or extract_source_video_id(row["path"])
            is_youtube = bool(video_id) or self._path_is_in_youtube_root(row["path"])
            if not is_youtube:
                continue

            updates: dict[str, object] = {}
            if not row["source_kind"]:
                updates["source_kind"] = "youtube"
            if video_id and not row["source_video_id"]:
                updates["source_video_id"] = video_id

            credible_canonical = bool(
                str(row["musicbrainz_recording_id"] or "").strip()
                or str(row["musicbrainz_release_id"] or "").strip()
            )
            if not credible_canonical and row["year"]:
                if not row["source_upload_date"]:
                    source_date = normalize_source_upload_date(row["year"])
                    if source_date:
                        updates["source_upload_date"] = source_date
                updates["year"] = None

            if updates:
                assignments = ", ".join(f"{column}=?" for column in updates)
                self.conn.execute(
                    f"UPDATE tracks SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [*updates.values(), row["id"]],
                )

    def migrate(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than supported version "
                f"{CURRENT_SCHEMA_VERSION}."
            )

        tables = self._table_names()
        is_new_database = not tables.intersection({"tracks", "playlists", "playlist_tracks"})

        if is_new_database:
            with self.conn:
                self._create_base_tables()
                self._create_support_tables_and_indexes()
                self.conn.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
            return

        if version < CURRENT_SCHEMA_VERSION:
            if self._has_user_data():
                self._create_pre_migration_backup(CURRENT_SCHEMA_VERSION)

            with self.conn:
                self._create_base_tables()
                self._add_track_source_columns()
                self._create_support_tables_and_indexes()
                self._backfill_youtube_source_fields()
                self.conn.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")

    def upsert_track(
        self,
        path: str | Path,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        duration_seconds: float | None = None,
        source_kind: str | None = None,
        source_video_id: str | None = None,
        source_upload_date: str | None = None,
    ) -> None:
        resolved_path = str(Path(path).resolve())
        self.conn.execute("""
            INSERT INTO tracks (
                path, title, artist, album, duration_seconds,
                source_kind, source_video_id, source_upload_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title=COALESCE(excluded.title, tracks.title),
                artist=COALESCE(excluded.artist, tracks.artist),
                album=COALESCE(excluded.album, tracks.album),
                duration_seconds=COALESCE(excluded.duration_seconds, tracks.duration_seconds),
                source_kind=COALESCE(excluded.source_kind, tracks.source_kind),
                source_video_id=COALESCE(excluded.source_video_id, tracks.source_video_id),
                source_upload_date=COALESCE(excluded.source_upload_date, tracks.source_upload_date),
                updated_at=CURRENT_TIMESTAMP
        """, (
            resolved_path,
            title,
            artist,
            album,
            duration_seconds,
            source_kind,
            source_video_id,
            source_upload_date,
        ))
        self.conn.commit()

    def update_track_metadata(self, track_id: int, **fields) -> None:
        allowed = {
            "title", "artist", "album", "album_artist", "year", "duration_seconds",
            "cover_path", "source_url", "musicbrainz_recording_id",
            "musicbrainz_release_id", "source_kind", "source_video_id",
            "source_upload_date",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return

        set_clause = ", ".join(f"{key}=?" for key in updates)
        self.conn.execute(
            f"UPDATE tracks SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*updates.values(), track_id],
        )
        self.conn.commit()

    @staticmethod
    def _track_select() -> str:
        return (
            "id, title, artist, album, year, path, cover_path, duration_seconds, "
            "created_at, source_kind, source_video_id, source_upload_date"
        )

    def list_tracks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(f"""
            SELECT {self._track_select()}
            FROM tracks
            ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, title COLLATE NOCASE
        """))

    def list_recent_tracks(self, limit: int = 150) -> list[sqlite3.Row]:
        return list(self.conn.execute(f"""
            SELECT {self._track_select()}
            FROM tracks
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """, (limit,)))

    def list_downloaded_tracks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(f"""
            SELECT {self._track_select()}
            FROM tracks
            WHERE source_kind='youtube'
            ORDER BY created_at DESC, artist COLLATE NOCASE, title COLLATE NOCASE
        """))

    def get_track(self, track_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()

    def get_track_by_source_video_id(self, video_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tracks WHERE source_kind='youtube' AND source_video_id=? LIMIT 1",
            (video_id,),
        ).fetchone()

    def existing_youtube_video_ids(self) -> set[str]:
        rows = self.conn.execute("""
            SELECT source_video_id, path
            FROM tracks
            WHERE source_kind='youtube' AND source_video_id IS NOT NULL
        """).fetchall()
        return {
            row["source_video_id"]
            for row in rows
            if row["source_video_id"] and Path(row["path"]).is_file()
        }

    def create_playlist(self, name: str) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Playlist name cannot be empty.")
        self.conn.execute("INSERT OR IGNORE INTO playlists(name) VALUES (?)", (clean_name,))
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM playlists WHERE name=?", (clean_name,)).fetchone()
        return int(row["id"])

    def list_playlists(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT id, name FROM playlists ORDER BY name COLLATE NOCASE
        """))

    def get_playlist_tracks(self, playlist_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT t.id, t.title, t.artist, t.album, t.year, t.path, t.cover_path,
                   t.duration_seconds, t.created_at, t.source_kind, t.source_video_id,
                   t.source_upload_date, pt.position
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            WHERE pt.playlist_id=?
            ORDER BY pt.position ASC, t.artist COLLATE NOCASE, t.title COLLATE NOCASE
        """, (playlist_id,)))

    def add_track_to_playlist(self, playlist_id: int, track_id: int) -> None:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
            "FROM playlist_tracks WHERE playlist_id=?",
            (playlist_id,),
        ).fetchone()
        self.conn.execute("""
            INSERT OR IGNORE INTO playlist_tracks(playlist_id, track_id, position)
            VALUES (?, ?, ?)
        """, (playlist_id, track_id, row["next_pos"]))
        self.conn.commit()

    def remove_track_from_playlist(self, playlist_id: int, track_id: int) -> None:
        self.conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
            (playlist_id, track_id),
        )
        self.conn.commit()

    def delete_playlist(self, playlist_id: int) -> None:
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
        self.conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        self.conn.commit()

    def record_sync_failure(
        self,
        *,
        playlist_id: str,
        playlist_title: str | None,
        video_id: str,
        title: str | None,
        reason: str,
        error_category: str,
        attempted_at: str | None = None,
    ) -> None:
        timestamp = attempted_at or _utc_now()
        reason = sanitize_error_text(reason)
        self.conn.execute("""
            INSERT INTO sync_failures (
                playlist_id, playlist_title, video_id, title, reason, error_category,
                attempt_count, first_attempt_at, last_attempt_at, status, resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'unresolved', NULL)
            ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                playlist_title=excluded.playlist_title,
                title=COALESCE(excluded.title, sync_failures.title),
                reason=excluded.reason,
                error_category=excluded.error_category,
                attempt_count=sync_failures.attempt_count + 1,
                last_attempt_at=excluded.last_attempt_at,
                status='unresolved',
                resolved_at=NULL
        """, (
            playlist_id,
            playlist_title,
            video_id,
            title,
            reason,
            error_category,
            timestamp,
            timestamp,
        ))
        self.conn.commit()

    def resolve_sync_failure(self, video_id: str, resolved_at: str | None = None) -> None:
        self.conn.execute("""
            UPDATE sync_failures
            SET status='resolved', resolved_at=?
            WHERE video_id=? AND status='unresolved'
        """, (resolved_at or _utc_now(), video_id))
        self.conn.commit()

    def unresolved_failure_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM sync_failures WHERE status='unresolved'"
        ).fetchone()
        return int(row[0]) if row else 0

    def list_sync_failures(self, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            query = "SELECT * FROM sync_failures ORDER BY last_attempt_at DESC, id DESC"
            return list(self.conn.execute(query))
        return list(self.conn.execute(
            "SELECT * FROM sync_failures WHERE status=? ORDER BY last_attempt_at DESC, id DESC",
            (status,),
        ))

    def clear_failure_history(self) -> None:
        self.conn.execute("DELETE FROM sync_failures")
        self.conn.commit()

    def import_legacy_failures(self, failed_file: str | Path) -> int:
        marker = self.conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (_LEGACY_FAILURE_IMPORT_KEY,),
        ).fetchone()
        if marker is not None:
            return 0

        path = Path(failed_file)
        valid_ids: set[str] = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                video_id = line.strip()
                if _VALID_VIDEO_ID_RE.fullmatch(video_id):
                    valid_ids.add(video_id)

        timestamp = _utc_now()
        with self.conn:
            for video_id in sorted(valid_ids):
                self.conn.execute("""
                    INSERT OR IGNORE INTO sync_failures (
                        playlist_id, playlist_title, video_id, title, reason,
                        error_category, attempt_count, first_attempt_at,
                        last_attempt_at, status, resolved_at
                    )
                    VALUES ('legacy', 'Legacy failure history', ?, NULL,
                            'Imported from legacy failure history.', 'legacy',
                            1, ?, ?, 'unresolved', NULL)
                """, (video_id, timestamp, timestamp))
            self.conn.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                (_LEGACY_FAILURE_IMPORT_KEY, timestamp),
            )
        return len(valid_ids)
