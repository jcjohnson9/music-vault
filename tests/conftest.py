from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from _runtime_data_guard import RuntimeDataGuard


# Install before test-module collection. An audit hook survives monkeypatching
# open()/connect(), and its records still fail the suite if application code
# catches PermissionError (for example, a best-effort status writer).
_runtime_data_guard = RuntimeDataGuard(Path(__file__).resolve().parents[1])
sys.addaudithook(_runtime_data_guard.audit)


@pytest.fixture(autouse=True)
def _deny_personal_runtime_access():
    before = len(_runtime_data_guard.violations)
    yield
    new = _runtime_data_guard.violations[before:]
    if new:
        events = ", ".join(sorted({record["event"] for record in new}))
        pytest.fail(f"Personal runtime access was blocked during this test: {events}", pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    if _runtime_data_guard.violations:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    count = len(_runtime_data_guard.violations)
    if count:
        terminalreporter.write_sep("!", f"Runtime isolation: {count} denied personal-data access attempt(s)")


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


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
