
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .paths import database_path


class MusicVaultDB:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else database_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        cur = self.conn.cursor()

        cur.execute("""
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (playlist_id, track_id),
                FOREIGN KEY (playlist_id) REFERENCES playlists(id),
                FOREIGN KEY (track_id) REFERENCES tracks(id)
            )
        """)

        self.conn.commit()

    def upsert_track(
        self,
        path: str | Path,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        duration_seconds: float | None = None
    ) -> None:
        path = str(Path(path).resolve())

        self.conn.execute("""
            INSERT INTO tracks (path, title, artist, album, duration_seconds)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title=COALESCE(excluded.title, tracks.title),
                artist=COALESCE(excluded.artist, tracks.artist),
                album=COALESCE(excluded.album, tracks.album),
                duration_seconds=COALESCE(excluded.duration_seconds, tracks.duration_seconds),
                updated_at=CURRENT_TIMESTAMP
        """, (path, title, artist, album, duration_seconds))

        self.conn.commit()

    def update_track_metadata(self, track_id: int, **fields) -> None:
        allowed = {
            "title", "artist", "album", "album_artist", "year", "duration_seconds",
            "cover_path", "source_url", "musicbrainz_recording_id", "musicbrainz_release_id"
        }

        updates = {k: v for k, v in fields.items() if k in allowed}

        if not updates:
            return

        set_clause = ", ".join([f"{key}=?" for key in updates])
        values = list(updates.values()) + [track_id]

        self.conn.execute(
            f"UPDATE tracks SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            values
        )

        self.conn.commit()

    def list_tracks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT id, title, artist, album, year, path, cover_path, duration_seconds, created_at
            FROM tracks
            ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, title COLLATE NOCASE
        """))

    def list_recent_tracks(self, limit: int = 150) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT id, title, artist, album, year, path, cover_path, duration_seconds, created_at
            FROM tracks
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """, (limit,)))

    def list_downloaded_tracks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT id, title, artist, album, year, path, cover_path, duration_seconds, created_at
            FROM tracks
            WHERE path LIKE '%youtube_downloads%'
            ORDER BY created_at DESC, artist COLLATE NOCASE, title COLLATE NOCASE
        """))

    def get_track(self, track_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,))
        return cur.fetchone()

    def create_playlist(self, name: str) -> int:
        clean_name = name.strip()

        if not clean_name:
            raise ValueError("Playlist name cannot be empty.")

        self.conn.execute(
            "INSERT OR IGNORE INTO playlists(name) VALUES (?)",
            (clean_name,)
        )
        self.conn.commit()

        row = self.conn.execute("SELECT id FROM playlists WHERE name=?", (clean_name,)).fetchone()
        return int(row["id"])

    def list_playlists(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT id, name
            FROM playlists
            ORDER BY name COLLATE NOCASE
        """))

    def get_playlist_tracks(self, playlist_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("""
            SELECT t.id, t.title, t.artist, t.album, t.year, t.path, t.cover_path,
                   t.duration_seconds, t.created_at, pt.position
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            WHERE pt.playlist_id=?
            ORDER BY pt.position ASC, t.artist COLLATE NOCASE, t.title COLLATE NOCASE
        """, (playlist_id,)))

    def add_track_to_playlist(self, playlist_id: int, track_id: int) -> None:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos FROM playlist_tracks WHERE playlist_id=?",
            (playlist_id,)
        ).fetchone()

        self.conn.execute("""
            INSERT OR IGNORE INTO playlist_tracks(playlist_id, track_id, position)
            VALUES (?, ?, ?)
        """, (playlist_id, track_id, row["next_pos"]))

        self.conn.commit()

    def remove_track_from_playlist(self, playlist_id: int, track_id: int) -> None:
        self.conn.execute("""
            DELETE FROM playlist_tracks
            WHERE playlist_id=? AND track_id=?
        """, (playlist_id, track_id))

        self.conn.commit()

    def delete_playlist(self, playlist_id: int) -> None:
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
        self.conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        self.conn.commit()
