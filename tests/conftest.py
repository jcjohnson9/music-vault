from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def v0_database(tmp_path: Path):
    def create(*, with_rows: bool = True) -> Path:
        path = tmp_path / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE tracks (
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
            );
            CREATE TABLE playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE playlist_tracks (
                playlist_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (playlist_id, track_id)
            );
            """
        )
        if with_rows:
            youtube_file = tmp_path / "Song [abcdefghijk].mp3"
            canonical_file = tmp_path / "Canonical [lmnopqrstuv].mp3"
            local_file = tmp_path / "Local Song.mp3"
            for file in (youtube_file, canonical_file, local_file):
                file.write_bytes(b"synthetic")
            conn.execute(
                "INSERT INTO tracks(path,title,year) VALUES (?,?,?)",
                (str(youtube_file), "Source date", "2021"),
            )
            conn.execute(
                """INSERT INTO tracks(path,title,year,musicbrainz_recording_id)
                   VALUES (?,?,?,?)""",
                (str(canonical_file), "Canonical", "1984", "mb-recording"),
            )
            conn.execute(
                "INSERT INTO tracks(path,title,year) VALUES (?,?,?)",
                (str(local_file), "Local", "1999"),
            )
            conn.execute("INSERT INTO playlists(name) VALUES ('Mix')")
            conn.execute(
                "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES (1,1,0)"
            )
        conn.commit()
        conn.close()
        return path

    return create
