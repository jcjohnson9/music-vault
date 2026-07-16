from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.playlist_membership import (
    ManagedPlaylistError,
    PlaylistMembershipService,
)


def _tracks(db: MusicVaultDB, tmp_path: Path, count: int = 5) -> list[int]:
    return [
        db.upsert_track(tmp_path / f"track-{index}.media", title=f"Track {index}")
        for index in range(count)
    ]


def _source(db: MusicVaultDB, external_id: str, playlist_id: int | None) -> int:
    destination_kind = "playlist" if playlist_id is not None else "library"
    cursor = db.conn.execute(
        """
        INSERT INTO sync_sources (
            source_kind,external_id,source_url,label,enabled,sort_order,
            destination_kind,destination_playlist_id,storage_key,created_at,updated_at
        ) VALUES ('youtube_playlist',?,?,?,?,0,?,?,?,?,?)
        """,
        (
            external_id,
            f"https://www.youtube.com/playlist?list={external_id}",
            "Synthetic Source",
            1,
            destination_kind,
            playlist_id,
            f"youtube_{external_id.casefold()}",
            "t0",
            "t0",
        ),
    )
    db.conn.commit()
    return int(cursor.lastrowid)


def test_materialization_orders_source_first_collapses_duplicates_and_keeps_manual(tmp_path):
    db = MusicVaultDB(tmp_path / "membership.sqlite3")
    first, second, manual, *_ = _tracks(db, tmp_path)
    playlist = db.create_playlist("Managed")
    source = _source(db, "PL-source", playlist)
    memberships = PlaylistMembershipService(db)

    db.add_track_to_playlist(playlist, second)
    db.add_track_to_playlist(playlist, manual)
    assert memberships.set_source_origins(
        source,
        playlist,
        [(first, 4), (second, 1), (first, 0)],
    ) == 2

    rows = db.get_playlist_tracks(playlist)
    assert [int(row["id"]) for row in rows] == [first, second, manual]
    assert [int(row["position"]) for row in rows] == [0, 1, 2]
    assert rows[0]["source_managed"] == 1 and rows[0]["manual_origin"] == 0
    assert rows[1]["source_managed"] == 1 and rows[1]["manual_origin"] == 1
    assert rows[2]["source_managed"] == 0 and rows[2]["manual_origin"] == 1

    result = db.remove_track_from_playlist(playlist, second)
    assert result.manual_origin_removed is True
    assert result.source_managed is True
    assert result.remains_visible is True
    result = db.remove_track_from_playlist(playlist, manual)
    assert result.manual_origin_removed is True
    assert result.source_managed is False
    assert result.remains_visible is False
    assert [int(row["id"]) for row in db.get_playlist_tracks(playlist)] == [first, second]
    db.close()


def test_reconcile_uses_first_present_occurrence_and_one_materialized_track(tmp_path):
    db = MusicVaultDB(tmp_path / "snapshot.sqlite3")
    first, second, *_ = _tracks(db, tmp_path)
    playlist = db.create_playlist("Snapshot")
    source = _source(db, "PL-snapshot", playlist)
    db.conn.executemany(
        """
        INSERT INTO sync_source_items (
            source_id,source_item_id,video_id,source_position,source_title,
            availability_status,track_id,first_seen_at,last_seen_at,
            created_at,updated_at
        ) VALUES (?,?,?,?,?,'available',?,'t0','t0','t0','t0')
        """,
        [
            (source, "item-a", "video-a", 8, "A", first),
            (source, "item-b", "video-a", 2, "A duplicate", first),
            (source, "item-c", "video-b", 4, "B", second),
        ],
    )
    db.conn.commit()

    count = PlaylistMembershipService(db).reconcile_source(source)
    assert count == 2
    assert [
        (int(row["track_id"]), int(row["origin_position"]))
        for row in db.conn.execute(
            """
            SELECT track_id,origin_position FROM playlist_track_origins
            WHERE sync_source_id=? ORDER BY origin_position
            """,
            (source,),
        )
    ] == [(first, 2), (second, 4)]
    assert [int(row["id"]) for row in db.get_playlist_tracks(playlist)] == [first, second]
    db.close()


def test_detach_converts_visible_order_to_manual_and_preserves_everything(tmp_path):
    db = MusicVaultDB(tmp_path / "detach.sqlite3")
    first, second, extra, *_ = _tracks(db, tmp_path)
    playlist = db.create_playlist("Detach")
    source = _source(db, "PL-detach", playlist)
    memberships = PlaylistMembershipService(db)
    db.add_track_to_playlist(playlist, extra)
    memberships.set_source_origins(source, playlist, [(second, 0), (first, 1)])
    before = [int(row["id"]) for row in db.get_playlist_tracks(playlist)]

    result = memberships.detach_source(source)
    after = [int(row["id"]) for row in db.get_playlist_tracks(playlist)]
    assert before == [second, first, extra] == after
    assert result.affected_playlist_ids == (playlist,)
    assert result.preserved_track_count == 3
    assert db.conn.execute(
        "SELECT COUNT(*) FROM playlist_track_origins WHERE sync_source_id=?",
        (source,),
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM playlist_track_origins WHERE origin_kind='manual'"
    ).fetchone()[0] == 3
    source_row = db.conn.execute(
        "SELECT destination_kind,destination_playlist_id FROM sync_sources WHERE id=?",
        (source,),
    ).fetchone()
    assert tuple(source_row) == ("library", None)
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 5
    with pytest.raises(ValueError):
        memberships.detach_source(source, preserve_visible=False)
    db.close()


def test_managed_playlist_delete_is_guarded_and_rename_keeps_id_link(tmp_path):
    db = MusicVaultDB(tmp_path / "guard.sqlite3")
    playlist = db.create_playlist("Before")
    source = _source(db, "PL-guard", playlist)
    with pytest.raises(ManagedPlaylistError):
        db.delete_playlist(playlist)
    db.rename_playlist(playlist, "After")
    assert db.conn.execute(
        "SELECT destination_playlist_id FROM sync_sources WHERE id=?", (source,)
    ).fetchone()[0] == playlist
    assert db.list_playlists()[0]["name"] == "After"
    PlaylistMembershipService(db).detach_source(source)
    db.delete_playlist(playlist)
    assert db.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 0
    db.close()


def test_failures_resolve_and_clear_only_inside_requested_source(tmp_path):
    db = MusicVaultDB(tmp_path / "failures.sqlite3")
    source_a = _source(db, "PL-a", None)
    source_b = _source(db, "PL-b", None)
    for playlist_id, source_id, item_id in (
        ("PL-a", source_a, "item-a"),
        ("PL-b", source_b, "item-b"),
    ):
        db.record_sync_failure(
            playlist_id=playlist_id,
            playlist_title="Synthetic",
            video_id="abcdefghijk",
            title="Synthetic",
            reason="Synthetic failure",
            error_category="download",
            sync_source_id=source_id,
            source_item_id=item_id,
        )
    assert db.unresolved_failure_count() == 2
    assert db.unresolved_failure_count(source_a) == 1
    db.resolve_sync_failure("abcdefghijk", sync_source_id=source_a)
    assert db.unresolved_failure_count(source_a) == 0
    assert db.unresolved_failure_count(source_b) == 1
    assert len(db.list_sync_failures("resolved", sync_source_id=source_a)) == 1
    db.clear_failure_history(source_a)
    assert len(db.list_sync_failures(sync_source_id=source_a)) == 0
    assert len(db.list_sync_failures(sync_source_id=source_b)) == 1
    db.close()


def test_track_cleanup_cascades_origins_and_identity_but_keeps_source_item_history(tmp_path):
    db = MusicVaultDB(tmp_path / "cleanup.sqlite3")
    media = tmp_path / "track.media"
    media.write_bytes(b"synthetic")
    track = db.upsert_track(
        media, source_kind="youtube", source_video_id="abcdefghijk"
    )
    playlist = db.create_playlist("Cleanup")
    source = _source(db, "PL-cleanup", playlist)
    db.add_track_to_playlist(playlist, track)
    db.conn.execute(
        """
        INSERT INTO sync_source_items (
            source_id,source_item_id,video_id,source_position,availability_status,
            track_id,first_seen_at,last_seen_at,created_at,updated_at
        ) VALUES (?,'item','abcdefghijk',0,'available',?,'t0','t0','t0','t0')
        """,
        (source, track),
    )
    db.conn.commit()
    # Match the existing Remove Missing workflow, which clears the legacy
    # materialized membership before deleting the track row.
    db.conn.execute("DELETE FROM playlist_tracks WHERE track_id=?", (track,))
    db.conn.execute("DELETE FROM tracks WHERE id=?", (track,))
    db.conn.commit()
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_track_origins").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0] == 0
    item = db.conn.execute("SELECT track_id FROM sync_source_items").fetchone()
    assert item[0] is None
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
