from __future__ import annotations

import re

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.sync_sources import (
    SyncSourceDestinationError,
    SyncSourceError,
    SyncSourceService,
    normalize_youtube_playlist_source,
    stable_source_storage_key,
)


@pytest.mark.parametrize(
    "value",
    [
        "PLabcdefghij",
        "https://www.youtube.com/playlist?list=PLabcdefghij",
        "https://music.youtube.com/playlist?list=PLabcdefghij&feature=share",
    ],
)
def test_playlist_source_normalization_is_local_and_deterministic(value):
    normalized = normalize_youtube_playlist_source(value)
    assert normalized.external_id == "PLabcdefghij"
    assert normalized.source_url == (
        "https://www.youtube.com/playlist?list=PLabcdefghij"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        r"C:\private\playlist",
        "http://www.youtube.com/playlist?list=PLabcdefghij",
        "https://example.test/playlist?list=PLabcdefghij",
        "https://user@example.com/playlist?list=PLabcdefghij",
        "https://www.youtube.com/watch?v=abcdefghijk",
    ],
)
def test_playlist_source_normalization_rejects_unsupported_input(value):
    with pytest.raises(SyncSourceError):
        normalize_youtube_playlist_source(value)


def test_storage_key_is_safe_stable_and_identity_based():
    first = stable_source_storage_key("youtube_playlist", "PLabcdefghij")
    second = stable_source_storage_key("youtube_playlist", "PLabcdefghij")
    other = stable_source_storage_key("youtube_playlist", "PLabcdefghik")
    assert first == second and first != other
    assert len(first) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)


def test_source_crud_reorder_archive_and_restore(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    playlist_id = db.create_playlist("Managed")
    service = SyncSourceService(db)
    first = service.create_source(
        "PLabcdefghij",
        label="First",
        destination_kind="playlist",
        destination_playlist_id=playlist_id,
    )
    second = service.create_source("PLabcdefghik", enabled=False)

    assert [source.id for source in service.list_active()] == [first.id, second.id]
    assert [source.id for source in service.list_active(enabled_only=True)] == [first.id]
    assert service.set_enabled(second.id, True).enabled
    assert [source.id for source in service.move(second.id, -1)] == [second.id, first.id]
    archived = service.archive(first.id)
    assert archived.archived_at and archived.destination_kind == "library"
    restored = service.create_source("PLabcdefghij", label="Restored")
    assert restored.id == first.id
    assert restored.archived_at is None and restored.label == "Restored"
    assert service.create_source("PLabcdefghij").id == first.id
    db.close()


def test_only_one_active_source_can_manage_a_playlist(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    playlist_id = db.create_playlist("Managed")
    service = SyncSourceService(db)
    service.create_source(
        "PLabcdefghij",
        destination_kind="playlist",
        destination_playlist_id=playlist_id,
    )
    with pytest.raises(SyncSourceDestinationError):
        service.create_source(
            "PLabcdefghik",
            destination_kind="playlist",
            destination_playlist_id=playlist_id,
        )
    db.close()
