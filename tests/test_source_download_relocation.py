"""Explicit relocation has verified rollback copies and exact preservation."""
from pathlib import Path
import sqlite3

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.sync_sources import SyncSourceService
from tools.dev import relocate_source_downloads as tool


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "assert_app_closed", lambda: None)
    root = tmp_path / "downloads"
    root.mkdir()
    db_path = tmp_path / "library.sqlite3"
    db = MusicVaultDB(db_path)
    source = SyncSourceService(db).create_source("PLsyntheticRelocate", label="Home Trip")
    old = root / "sources" / source.storage_key
    old.mkdir(parents=True)
    dest = root / "Home Trip"
    dest.mkdir()
    (dest / "already-here.mp3").write_bytes(b"existing untouched")
    first = old / "one.opus"
    first.write_bytes(b"synthetic one")
    (old / "two.jpg").write_bytes(b"synthetic image")
    track = db.upsert_track(first, title="Synthetic", artist="Fixture")
    playlist = db.create_playlist("Synthetic")
    db.add_track_to_playlist(playlist, track)
    db.close()
    backup = tmp_path / "rollback"
    return db_path, root, source, old, dest, backup, track


def run(fixture, **kwargs):
    db, root, source, _old, _dest, backup, _track = fixture
    return tool.relocate(db, root, source.storage_key, "Home Trip", backup, **kwargs)


def test_move_preserves_all_nonpath_values_and_verified_backups(fixture):
    db, root, source, old, dest, backup, track = fixture
    before_hash = tool.digest(db)
    result = run(fixture)
    assert result["files_moved"] == 2 and result["track_paths_updated"] == 1
    assert result["existing_destination_files_preserved"] == 1
    assert not list(old.iterdir())
    assert (dest / "one.opus").read_bytes() == b"synthetic one"
    assert tool.digest(backup / "rollback.sqlite3") == before_hash
    assert tool.stamp(backup / "media/one.opus") == tool.stamp(dest / "one.opus")
    with sqlite3.connect(db) as conn:
        assert conn.execute("select path from tracks where id=?", (track,)).fetchone()[0] == str(dest / "one.opus")
    assert result["metadata_memberships_history_unchanged"]


def test_collision_aborts_before_any_backup_or_move(fixture):
    db, _root, _source, old, dest, backup, _track = fixture
    (dest / "ONE.OPUS").write_bytes(b"must not overwrite")
    before = tool.digest(db)
    with pytest.raises(RuntimeError, match="collision"):
        run(fixture)
    assert tool.digest(db) == before and (old / "one.opus").is_file()
    assert not backup.exists()


def test_partial_move_failure_compensates_owned_moves_and_rolls_back_db(fixture):
    db, _root, _source, old, dest, backup, _track = fixture
    before = tool.digest(db)
    calls = []
    def interrupted(a, b):
        calls.append((a, b))
        if len(calls) == 2:
            raise OSError("synthetic move failure")
        tool.os.rename(a, b)
    with pytest.raises(OSError, match="synthetic move"):
        run(fixture, move_file=interrupted)
    assert tool.digest(db) == before
    assert {p.name for p in old.iterdir()} == {"one.opus", "two.jpg"}
    assert {p.name for p in dest.iterdir()} == {"already-here.mp3"}
    assert (backup / "rollback.sqlite3").is_file()


def test_additional_cover_or_history_reference_requires_review(fixture):
    db, _root, _source, old, _dest, backup, track = fixture
    with sqlite3.connect(db) as conn:
        conn.execute("update tracks set cover_path=? where id=?", (str(old / "two.jpg"), track))
    with pytest.raises(RuntimeError, match="Additional source-path references"):
        run(fixture)
    assert not backup.exists()


@pytest.mark.parametrize("kind", ["unexpected", "subdirectory", "sidecar", "running"])
def test_unsafe_or_active_state_stops_without_changes(fixture, monkeypatch, kind):
    db, _root, _source, old, _dest, backup, _track = fixture
    if kind == "unexpected":
        (old / "private.txt").write_bytes(b"not read")
    elif kind == "subdirectory":
        (old / "nested").mkdir()
    elif kind == "sidecar":
        Path(str(db) + "-wal").touch()
    else:
        def running():
            raise RuntimeError("Close Music Vault")
        monkeypatch.setattr(tool, "assert_app_closed", running)
    with pytest.raises(RuntimeError):
        run(fixture)
    assert (old / "one.opus").is_file() and not backup.exists()
