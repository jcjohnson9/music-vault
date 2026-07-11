from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.library_browser import (
    AlbumKey,
    ArtistKey,
    BrowserInvalidationReason,
    BrowserKind,
    BrowserRevision,
    BrowserSummaryCache,
    browser_revision,
    load_album_summaries,
    load_album_tracks,
    load_artist_summaries,
    load_artist_tracks,
    normalize_identity,
    open_readonly_database,
    query_album_summaries,
    query_artist_summaries,
)


def _add_track(
    db: MusicVaultDB,
    root: Path,
    name: str,
    *,
    artist: str | None,
    album: str | None,
    album_artist: str | None = None,
    year: str | None = None,
    cover_path: str | None = None,
    source_upload_date: str | None = None,
) -> int:
    path = root / f"{name}.synthetic-audio"
    db.upsert_track(
        path,
        title=name,
        artist=artist,
        album=album,
        source_kind="youtube" if source_upload_date else "local",
        source_upload_date=source_upload_date,
    )
    row = db.conn.execute("SELECT id FROM tracks WHERE path=?", (str(path.resolve()),)).fetchone()
    track_id = int(row["id"])
    updates = {
        "album_artist": album_artist,
        "year": year,
        "cover_path": cover_path,
    }
    db.update_track_metadata(
        track_id,
        **{key: value for key, value in updates.items() if value is not None},
    )
    return track_id


@pytest.fixture
def browser_db(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3", backup_dir=tmp_path / "backups")
    yield db
    db.close()


def test_album_summaries_group_by_full_identity_and_use_canonical_year(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    first = _add_track(
        browser_db,
        tmp_path,
        "first",
        artist="Artist A",
        album="Shared Title",
        year="2001",
    )
    second_cover = str(tmp_path / "cover-second.png")
    second = _add_track(
        browser_db,
        tmp_path,
        "second",
        artist="ARTIST A",
        album="  shared title  ",
        album_artist=" artist a ",
        year="2001",
        cover_path=second_cover,
    )
    _add_track(
        browser_db,
        tmp_path,
        "third",
        artist="Artist B",
        album="Shared Title",
        year="2001",
    )
    _add_track(
        browser_db,
        tmp_path,
        "fourth",
        artist="Artist A",
        album="Shared Title",
        year="2002",
    )
    _add_track(
        browser_db,
        tmp_path,
        "upload-only",
        artist="Uploader",
        album="Upload Date Is Not A Release",
        source_upload_date="2024-03-02",
    )

    summaries = query_album_summaries(browser_db.conn)
    shared = [summary for summary in summaries if summary.key.title_key == "shared title"]

    assert len(shared) == 3
    grouped = next(
        summary
        for summary in shared
        if summary.key.artist_key == "artist a" and summary.key.year_key == "2001"
    )
    assert grouped.track_count == 2
    assert grouped.album_title == "Shared Title"
    assert grouped.album_artist == "Artist A"
    assert grouped.canonical_year == "2001"
    assert grouped.representative_cover_path == second_cover
    assert {row["id"] for row in load_album_tracks(browser_db.db_path, grouped.key)} == {
        first,
        second,
    }

    upload_summary = next(
        summary for summary in summaries if summary.album_title == "Upload Date Is Not A Release"
    )
    assert upload_summary.canonical_year is None
    assert upload_summary.key.year_key == ""


def test_missing_album_identity_does_not_collide_with_literal_unknown_album(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    _add_track(browser_db, tmp_path, "missing", artist="Artist", album=None)
    _add_track(browser_db, tmp_path, "literal", artist="Artist", album="Unknown Album")

    unknowns = [
        summary
        for summary in query_album_summaries(browser_db.conn)
        if summary.album_title == "Unknown Album"
    ]
    assert len(unknowns) == 2
    assert {summary.key.title_key for summary in unknowns} == {"", "unknown album"}
    assert len({summary.browser_key for summary in unknowns}) == 2


def test_album_cover_selection_is_lowest_track_id_with_nonblank_cover(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    _add_track(browser_db, tmp_path, "one", artist="Artist", album="Album")
    expected = str(tmp_path / "first-cover.png")
    _add_track(
        browser_db,
        tmp_path,
        "two",
        artist="Artist",
        album="Album",
        cover_path=expected,
    )
    _add_track(
        browser_db,
        tmp_path,
        "three",
        artist="Artist",
        album="Album",
        cover_path=str(tmp_path / "later-cover.png"),
    )
    summary = query_album_summaries(browser_db.conn)[0]
    assert summary.representative_cover_path == expected


def test_artist_summaries_trim_casefold_and_never_split_credits(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    ids = {
        _add_track(browser_db, tmp_path, "one", artist="  The Echoes  ", album="A"),
        _add_track(browser_db, tmp_path, "two", artist="the echoes", album="B"),
        _add_track(browser_db, tmp_path, "three", artist="THE ECHOES", album="C"),
    }
    compound_id = _add_track(
        browser_db,
        tmp_path,
        "compound",
        artist="A & B feat. C",
        album="Collaboration",
    )

    summaries = query_artist_summaries(browser_db.conn)
    echoes = next(summary for summary in summaries if summary.key.normalized_name == "the echoes")
    compound = next(
        summary for summary in summaries if summary.key.normalized_name == "a & b feat. c"
    )

    assert echoes.display_name == "The Echoes"
    assert echoes.track_count == 3
    assert {row["id"] for row in load_artist_tracks(browser_db.db_path, echoes.key)} == ids
    assert compound.display_name == "A & B feat. C"
    assert compound.track_count == 1
    assert [row["id"] for row in load_artist_tracks(browser_db.db_path, compound.key)] == [
        compound_id
    ]
    assert all(summary.display_name not in {"A", "B", "C"} for summary in summaries)


def test_missing_artist_is_distinct_from_literal_unknown_artist(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    _add_track(browser_db, tmp_path, "missing", artist=None, album="A")
    _add_track(browser_db, tmp_path, "literal", artist="Unknown Artist", album="B")
    summaries = [
        summary
        for summary in query_artist_summaries(browser_db.conn)
        if summary.display_name == "Unknown Artist"
    ]
    assert len(summaries) == 2
    assert {summary.key.normalized_name for summary in summaries} == {"", "unknown artist"}


def test_keys_are_stable_and_artist_image_state_can_be_overlaid(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    _add_track(browser_db, tmp_path, "one", artist="Artist", album="Album", year="1999")
    album = query_album_summaries(browser_db.conn)[0]
    artist = query_artist_summaries(browser_db.conn)[0]

    assert album.key == AlbumKey("album", "artist", "1999")
    assert album.browser_key == AlbumKey("album", "artist", "1999").browser_key
    assert album.browser_key != AlbumKey("album", "artist", "2000").browser_key
    assert artist.key == ArtistKey("artist")

    overlaid = query_artist_summaries(
        browser_db.conn,
        image_states={artist.browser_key: "resolved"},
    )[0]
    assert overlaid.image_state == "resolved"


def test_revision_is_stable_without_track_change_and_changes_after_mutations(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    empty = browser_revision(browser_db.conn)
    assert empty == browser_revision(browser_db.conn)

    track_id = _add_track(browser_db, tmp_path, "one", artist="Artist", album="Album")
    inserted = browser_revision(browser_db.conn)
    assert inserted != empty
    assert inserted.track_count == 1

    browser_db.conn.execute(
        "UPDATE tracks SET cover_path=?, updated_at=? WHERE id=?",
        (str(tmp_path / "cover.png"), "2099-01-01 00:00:00", track_id),
    )
    browser_db.conn.commit()
    updated = browser_revision(browser_db.conn)
    assert updated != inserted
    assert updated.artwork_count == 1

    # Reads and non-track state do not perturb the browser fingerprint.
    browser_db.create_playlist("Synthetic Playlist")
    assert browser_revision(browser_db.conn) == updated


def test_readonly_loaders_are_worker_safe_and_cannot_write(
    browser_db: MusicVaultDB,
    tmp_path: Path,
):
    _add_track(browser_db, tmp_path, "one", artist="Artist", album="Album")
    path = browser_db.db_path

    with ThreadPoolExecutor(max_workers=2) as pool:
        album_future = pool.submit(load_album_summaries, path)
        artist_future = pool.submit(load_artist_summaries, path)
        assert len(album_future.result(timeout=5)) == 1
        assert len(artist_future.result(timeout=5)) == 1

    with open_readonly_database(path) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO playlists(name) VALUES ('Not Allowed')")


def test_cache_reuses_revision_and_rejects_results_started_before_invalidation():
    cache = BrowserSummaryCache()
    revision = BrowserRevision(1, 1, "2026-01-01", 0)
    album_token = cache.token(BrowserKind.ALBUMS, revision)
    artist_token = cache.token(BrowserKind.ARTISTS, revision)

    assert cache.get(BrowserKind.ALBUMS, revision) is None
    assert cache.put(album_token, ()) is True
    assert cache.put(artist_token, ()) is True
    assert cache.get(BrowserKind.ALBUMS, revision) == ()
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1

    plan = cache.invalidate(BrowserInvalidationReason.IMPORT_FOLDER)
    assert plan.album_summaries is True
    assert plan.artist_summaries is True
    assert plan.album_thumbnails is True
    assert cache.put(album_token, ()) is False
    assert cache.put(artist_token, ()) is False


def test_invalidation_is_scoped_between_library_artwork_and_artist_images():
    cache = BrowserSummaryCache()
    revision = BrowserRevision(1, 1, "2026-01-01", 1)
    assert cache.put(cache.token("albums", revision), ())
    assert cache.put(cache.token("artists", revision), ())

    artwork_plan = cache.invalidate(BrowserInvalidationReason.ARTWORK_REFRESH)
    assert artwork_plan.album_summaries is True
    assert artwork_plan.artist_summaries is False
    assert artwork_plan.album_thumbnails is True
    assert cache.get("albums", revision) is None
    assert cache.get("artists", revision) == ()

    artist_image_plan = cache.invalidate(BrowserInvalidationReason.ARTIST_IMAGE_CACHE)
    assert artist_image_plan.album_summaries is False
    assert artist_image_plan.artist_summaries is False
    assert artist_image_plan.artist_thumbnails is True
    assert cache.get("artists", revision) == ()


def test_normalization_is_nfkc_whitespace_and_casefold_without_credit_splitting():
    assert normalize_identity("  A & B feat. C  ") == "a & b feat. c"
    assert normalize_identity(None) == ""
    assert normalize_identity("A  B") == normalize_identity("A B")
    assert normalize_identity("\uff21rtist") == "artist"
