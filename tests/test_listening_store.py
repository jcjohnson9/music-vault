from __future__ import annotations

import sqlite3

import pytest

from music_vault.core.listening_schema import create_listening_schema
from music_vault.core.listening_store import ListeningStore


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE tracks(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT,title TEXT,artist TEXT,album TEXT,created_at TEXT);
        CREATE TABLE playlists(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE playlist_tracks(playlist_id INTEGER,track_id INTEGER);
        INSERT INTO tracks VALUES(1,'synthetic1','First','Artist 1','Album 1','2026-03-03');
        INSERT INTO tracks VALUES(2,'synthetic2','Second','Artist 2','Album 2','2026-03-02');
        INSERT INTO tracks VALUES(3,'synthetic3','Third','Artist 3','Album 3','2026-03-01');
        INSERT INTO playlists VALUES(1,'Synthetic Playlist');
        INSERT INTO playlist_tracks VALUES(1,1);
    """)
    create_listening_schema(conn)
    result = ListeningStore(conn, clock=lambda: "2026-04-01T00:00:00Z")
    yield result
    conn.close()


def event(**changes):
    result = dict(
        event_id="occurrence-a", run_id="run-a", track_id=1, recorded_track_id=1,
        title_at_start="Original First", artist_at_start="Original Artist", album_at_start="Original Album",
        started_at="2026-04-01T00:00:00Z", last_observed_at="2026-04-01T00:00:01Z",
        ended_at=None, qualified_at=None, listened_ms=1000, duration_ms=60000,
        end_reason=None, playback_origin="manual_queue", context_kind="playlist", context_playlist_id=1,
        context_label="Synthetic Playlist", update_sequence=1,
    )
    result.update(changes)
    return result


def test_favorite_desired_state_is_idempotent_preserves_timestamp_and_canonical_identity(store):
    original = store._rows("SELECT * FROM tracks")
    assert store.set_favorite(1, True)
    row = store.liked_tracks()[0]
    assert not store.set_favorite(1, True)
    assert store.liked_tracks()[0] == row
    assert store.is_favorite(1)
    assert store.favorite_ids([1, 2, 2]) == {1}
    assert store.favorite_ids([]) == set()
    assert not store.set_favorite(999, True)
    assert store._rows("SELECT * FROM tracks") == original
    assert store._rows("SELECT * FROM playlist_tracks") == [{"playlist_id": 1, "track_id": 1}]
    assert store.write_count == store.revision == 1
    assert store.set_favorite(1, False)
    assert not store.set_favorite(1, False)
    assert store.favorite_ids() == set()


def test_active_favorites_and_history_render_current_canonical_blanks_not_stale_snapshots(store):
    store.set_favorite(1, True)
    store.save_event(event())
    with store.conn:
        store.conn.execute("UPDATE tracks SET title=NULL,artist='',album='Revised' WHERE id=1")
    liked = store.liked_tracks()[0]
    history = store.history_page()[0]
    assert liked["title"] is None and liked["artist"] == ""
    assert history["title"] == history["artist"] == ""
    assert history["album"] == "Revised"


def test_track_and_playlist_delete_preserve_tombstones_without_rebinding(store):
    store.set_favorite(1, True)
    store.save_event(event())
    with store.conn:
        store.conn.execute("DELETE FROM tracks WHERE id=1")
        store.conn.execute("DELETE FROM playlists WHERE id=1")
    assert store.favorite_ids() == set()
    assert store.liked_tracks() == []
    missing = store.unavailable_favorites()[0]
    assert missing["recorded_track_id"] == 1
    assert missing["title"] == "First" and missing["track_id"] is None
    history = store.history_page()[0]
    assert history["title"] == "Original First"
    assert not history["available"] and history["track_id"] is None
    assert history["context_playlist_id"] is None
    assert history["recorded_track_id"] == 1
    with store.conn:
        new_id = store.conn.execute("INSERT INTO tracks(path,title) VALUES('synthetic1','First')").lastrowid
    assert new_id != 1 and not store.is_favorite(new_id)
    assert store.save_event(event(update_sequence=2, listened_ms=2000))
    assert store.history_page()[0]["track_id"] is None
    assert store.remove_favorite(missing["favorite_id"])
    assert not store.remove_favorite(missing["favorite_id"])
    assert store.unavailable_favorites() == []
    assert len(store.history_page()) == 1
    assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_event_upsert_rejects_stale_retries_regression_collisions_and_reopening(store):
    initial = event()
    assert store.save_event(initial)
    assert not store.save_event(initial)
    assert not store.save_event(event(update_sequence=0, listened_ms=2000))
    assert not store.save_event(event(update_sequence=2, listened_ms=500))
    assert not store.save_event(event(update_sequence=2, run_id="other-run"))
    assert not store.save_event(event(update_sequence=2, track_id=2, recorded_track_id=2))
    qualified = "2026-04-01T00:00:30Z"
    assert store.save_event(event(update_sequence=3, listened_ms=30000, qualified_at=qualified))
    assert store.save_event(event(update_sequence=4, listened_ms=31000, qualified_at="2026-04-01T00:00:31Z"))
    assert store.save_event(event(update_sequence=5, listened_ms=32000, ended_at="2026-04-01T00:00:32Z", end_reason="next"))
    final = store.history_page()[0]
    assert final["qualified_at"].startswith("2026-04-01T00:00:30.")
    assert final["end_reason"] == "next" and final["listened_ms"] == 32000
    assert not store.save_event(event(update_sequence=99, listened_ms=90000))
    assert store.history_page()[0] == final
    assert store.write_count == store.revision == 4


def test_playback_occurrence_duplicates_are_independent_and_recency_not_import_order(store):
    store.save_event(event(event_id="queue-copy-a", track_id=3, recorded_track_id=3))
    store.save_event(event(event_id="queue-copy-b", track_id=3, recorded_track_id=3, started_at="2026-04-02T00:00:00Z", playback_origin="repeat"))
    store.save_event(event(event_id="queue-copy-c", track_id=2, recorded_track_id=2, started_at="2026-04-03T00:00:00Z", qualified_at="2026-04-03T00:00:30Z", listened_ms=30000))
    assert len(store.history_page()) == 3
    recent = store.recently_played_tracks()
    assert [row["id"] for row in recent] == [2, 3]
    assert [row["qualified_listen_count"] for row in recent] == [1, 0]
    assert store.recently_played_tracks(limit=1, offset=1)[0]["id"] == 3


def test_keyset_history_pagination_and_open_prior_run_interrupted_are_read_only(store):
    for suffix in "abcde":
        store.save_event(event(event_id=suffix))
    changes = store.conn.total_changes
    first = store.history_page(limit=2, run_id="run-a")
    second = store.history_page(limit=2, before=(first[-1]["started_at"], first[-1]["event_id"]), run_id="run-b")
    third = store.history_page(limit=2, before=(second[-1]["started_at"], second[-1]["event_id"]))
    assert [row["event_id"] for row in first + second + third] == list("edcba")
    assert all(not row["interrupted"] for row in first)
    assert all(row["interrupted"] and row["ended_at"] is None for row in second + third)
    assert store.conn.total_changes == changes
    assert len(store.history_page(limit=101)) == 5


def test_local_rediscovery_is_deterministic_bounded_and_honestly_named(store):
    for track_id in (1, 2, 3):
        store.set_favorite(track_id, True)
    store.save_event(event(track_id=2, recorded_track_id=2))
    store.save_event(event(event_id="newer", track_id=1, recorded_track_id=1, started_at="2026-04-02T00:00:00Z"))
    assert [row["id"] for row in store.rediscover_tracks()] == [3, 2, 1]
    assert [row["id"] for row in store.rediscover_tracks(limit=1, offset=1)] == [2]
    no_plays = store.rediscover_tracks("no_recorded_plays")
    assert [row["id"] for row in no_plays] == [3]
    assert no_plays[0]["reason"] == "no_recorded_plays"
    with pytest.raises(ValueError):
        store.rediscover_tracks("provider_radio")


def test_all_favorites_and_chunked_batch_states(store):
    with store.conn:
        store.conn.executemany("INSERT INTO tracks(path,title) VALUES(?,?)", [(f"fake-{i}", "Synthetic") for i in range(1005)])
        store.conn.execute("INSERT INTO track_favorites(track_id,recorded_track_id,title_at_favorite,artist_at_favorite,favorited_at) SELECT id,id,title,'','2026-04-01T00:00:00Z' FROM tracks")
    assert len(store.liked_tracks(limit=None)) == 1008
    assert len(store.liked_tracks(limit=5, offset=1005)) == 3
    assert store.favorite_ids(range(1, 1200)) == set(range(1, 1009))


def test_existing_outer_transaction_is_neither_committed_nor_modified(store):
    store.conn.execute("UPDATE tracks SET title='Pending' WHERE id=1")
    with pytest.raises(RuntimeError, match="independent transaction"):
        store.set_favorite(1, True)
    assert store.conn.in_transaction
    store.conn.rollback()
    assert store._rows("SELECT title FROM tracks WHERE id=1")[0]["title"] == "First"
    assert store.write_count == store.revision == 0


def test_failed_listening_transaction_preserves_revision_and_library_stamp(store):
    before = store.conn.total_changes - store.write_count

    def failing_write():
        store.conn.execute("INSERT INTO track_favorites VALUES(1,1,1,'First','Artist','2026-04-01T00:00:00Z')")
        raise OSError("synthetic write failure")

    with pytest.raises(OSError):
        store._write(failing_write)
    assert store.favorite_ids() == set()
    assert store.revision == 0 and store.write_count == 1
    assert store.conn.total_changes - store.write_count == before
    assert store.set_favorite(1, True)
    assert store.conn.total_changes - store.write_count == before
    with store.conn:
        store.conn.execute("UPDATE tracks SET title='Canonical change' WHERE id=1")
    assert store.conn.total_changes - store.write_count == before + 1


@pytest.mark.parametrize("change", [
    {"listened_ms": -1}, {"update_sequence": -1}, {"duration_ms": -1},
    {"playback_origin": "guessed"}, {"end_reason": "disliked"},
    {"ended_at": "2026-04-01T01:00:00Z"}, {"recorded_track_id": 2},
    {"started_at": "2026-04-01T00:00:00"},
])
def test_invalid_event_does_not_write(store, change):
    with pytest.raises(ValueError):
        store.save_event(event(**change))
    assert store.history_page() == []
    assert store.write_count == store.revision == 0


def test_foreign_key_constraints_and_bounded_private_snapshot_fields(store):
    store.save_event(event(track_id=999, recorded_track_id=999, context_playlist_id=999, title_at_start="a" * 5000, context_label="b" * 5000))
    row = store.history_page()[0]
    assert row["track_id"] is None and row["context_playlist_id"] is None
    assert len(row["title_at_start"]) == 1024 and len(row["context_label"]) == 256
    assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
