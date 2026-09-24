from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from music_vault.core.source_download_folders import SourceDownloadFolders
from music_vault.core.youtube_sync import AuthorizedYouTubePlaylistSyncer, YouTubeSyncConfig


@pytest.fixture
def folders(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE app_meta(key TEXT PRIMARY KEY, value TEXT)")
    service = SourceDownloadFolders(SimpleNamespace(conn=conn), tmp_path / "downloads")
    yield service
    conn.close()


def source(identity="PLsyntheticA"):
    return SimpleNamespace(source_kind="youtube_playlist", external_id=identity)


def test_human_title_binding_is_stable_across_syncs_renames_and_root_changes(folders, tmp_path):
    first = folders.resolve(source(), "Home Trip")
    assert first == folders.root / "Home Trip"
    assert folders.resolve(source(), "Remote renamed") == first
    assert folders.get(source()) == first
    second = folders.resolve(source("PLsyntheticB"), "Road Trip")
    assert second == folders.root / "Road Trip"
    assert folders.resolve(source(), "Home Trip") == first
    other_root = SourceDownloadFolders(SimpleNamespace(conn=folders.conn), tmp_path / "elsewhere")
    assert other_root.get(source()) is None
    assert other_root.resolve(source(), "Home Trip") == other_root.root / "Home Trip"


def test_case_collision_and_existing_unrelated_folder_are_not_adopted(folders):
    folders.root.mkdir()
    unrelated = folders.root / "home trip"
    unrelated.mkdir()
    marker = unrelated / "untouched.txt"
    marker.write_text("unrelated")
    first = folders.resolve(source(), "Home Trip")
    second = folders.resolve(source("PLsyntheticB"), "HOME TRIP")
    assert first.name.startswith("Home Trip [")
    assert second.name.casefold() != first.name.casefold()
    assert marker.read_text() == "unrelated"


def test_explicit_existing_binding_is_atomic_and_collision_checked(folders):
    destination = folders.root / "Home Trip"
    destination.mkdir(parents=True)
    folders.bind_existing(source(), "Home Trip", commit=False)
    assert folders.conn.in_transaction
    folders.conn.rollback()
    assert folders.get(source()) is None
    with folders.conn:
        assert folders.bind_existing(source(), "Home Trip") == destination
    assert folders.resolve(source(), "Home Trip") == destination
    with pytest.raises(ValueError, match="another source"):
        folders.bind_existing(source("PLsyntheticB"), "Home Trip")


@pytest.mark.parametrize("title", ["../escape", "C:\\escape", "CON", "NUL.txt", "a/b", "x" * 300, ""])
def test_remote_titles_are_safe_bounded_leaf_folders(folders, title):
    path = folders.resolve(source(), title)
    assert path.parent == folders.root
    assert len(path.name) <= 120
    assert path.is_dir()


@pytest.mark.parametrize("leaf", ["..", "../outside", "C:\\outside", "x/y", "CON", "bad."])
def test_explicit_binding_rejects_unsafe_leaf(folders, leaf):
    with pytest.raises(ValueError):
        folders.bind_existing(source(), leaf)


def test_reparse_attribute_is_rejected(folders, monkeypatch):
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == folders.root:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    folders.root.mkdir()
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="reparse"):
        folders.resolve(source(), "Home Trip")


def test_bound_reparse_folder_cannot_escape(folders, monkeypatch):
    path = folders.resolve(source(), "Home Trip")
    original = Path.lstat

    def lstat(candidate, *args, **kwargs):
        result = original(candidate, *args, **kwargs)
        if candidate == path:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="reparse"):
        folders.get(source())


def test_provider_resolves_remote_title_before_any_acquisition(folders, monkeypatch):
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=PLsyntheticA", folders.root,
        folders.root / "archive.txt", source_destination_dir=folders.root / "Wrong label",
        source_destination_resolver=lambda title, identity: folders.resolve(source(identity), title),
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    monkeypatch.setattr(syncer, "_resolve_ffmpeg_once", lambda: None)
    monkeypatch.setattr(syncer, "_extract_playlist_entries_via_api", lambda: ("PLsyntheticA", "Home Trip", []))
    monkeypatch.setattr(syncer, "_existing_downloads", lambda: {})
    monkeypatch.setattr(syncer, "_archive_ids", lambda: set())
    result = syncer.sync()
    assert result.status == "complete"
    assert folders.get(source()) == folders.root / "Home Trip"
    assert syncer._download_destination("Renamed", "PLsyntheticA") == folders.root / "Home Trip"
    assert not (folders.root / "Wrong label").exists()


def test_failed_enumeration_does_not_allocate_a_folder(folders, monkeypatch):
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=PLsyntheticA", folders.root,
        folders.root / "archive.txt",
        source_destination_resolver=lambda title, identity: folders.resolve(source(identity), title),
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    monkeypatch.setattr(syncer, "_resolve_ffmpeg_once", lambda: None)

    def fail():
        raise RuntimeError("Synthetic enumeration failure")

    monkeypatch.setattr(syncer, "_extract_playlist_entries_via_api", fail)
    assert syncer.sync().status == "failed"
    assert folders.get(source()) is None
    assert not list(folders.root.iterdir())


def test_failed_binding_rolls_back_and_removes_only_new_empty_directory(folders, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic persistence failure")

    monkeypatch.setattr(folders, "bind_existing", fail)
    with pytest.raises(RuntimeError, match="persistence"):
        folders.resolve(source(), "Home Trip")
    assert folders.get(source()) is None
    assert not list(folders.root.iterdir())
    assert not folders.conn.in_transaction
