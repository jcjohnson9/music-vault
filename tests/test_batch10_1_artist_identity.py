from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.library_browser import (
    query_artist_summaries,
    query_artist_track_sections,
)
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService


def _track(db: MusicVaultDB, root: Path, name: str, artist: str) -> int:
    path = root / f"{name}.synthetic-audio"
    return db.upsert_track(path, title=name, artist=artist, album="Synthetic Album")


def _single_column_is_unique(conn: sqlite3.Connection, column: str) -> bool:
    for row in conn.execute("PRAGMA index_list('artists')"):
        if not bool(row[2]):
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = [
            str(item[2])
            for item in conn.execute(f'PRAGMA index_info("{index_name}")')
        ]
        if columns == [column]:
            return True
    return False


def test_same_name_provider_artists_remain_distinct_in_credits_and_browser(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "identities.sqlite3")
    first_track = _track(db, tmp_path, "first", "Shared Public Name")
    second_track = _track(db, tmp_path, "second", "Shared Public Name")
    credits = ArtistCreditService(db)

    first = credits.replace_track_credits(
        first_track,
        (
            ArtistCreditInput(
                "Shared Public Name",
                discogs_artist_id="101",
                musicbrainz_artist_id="mb-first",
            ),
        ),
        provenance="discogs_high_confidence",
        confidence=99,
    )[0]
    second = credits.replace_track_credits(
        second_track,
        (
            ArtistCreditInput(
                "Shared Public Name",
                discogs_artist_id="202",
                musicbrainz_artist_id="mb-second",
            ),
        ),
        provenance="discogs_high_confidence",
        confidence=99,
    )[0]
    legacy_track = _track(db, tmp_path, "legacy", "Shared Public Name")
    db.conn.execute(
        "DELETE FROM track_artist_credits WHERE track_id=?",
        (legacy_track,),
    )
    db.conn.commit()

    assert first.artist.id != second.artist.id
    assert first.artist.normalized_name == second.artist.normalized_name
    provider_rows = db.conn.execute(
        """
        SELECT discogs_artist_id FROM artists
        WHERE normalized_name='shared public name'
          AND discogs_artist_id IS NOT NULL
        ORDER BY discogs_artist_id
        """
    ).fetchall()
    assert [row[0] for row in provider_rows] == ["101", "202"]

    summaries = [
        summary
        for summary in query_artist_summaries(db.conn)
        if summary.key.normalized_name == "shared public name"
    ]
    assert len(summaries) == 3
    assert len({summary.browser_key for summary in summaries}) == 3
    assert {
        summary.key.provider_identity
        for summary in summaries
        if summary.key.provider_identity
    } == {
        "discogs:101",
        "discogs:202",
    }
    tracks_by_provider = {
        summary.key.provider_identity: {
            row["id"]
            for row in query_artist_track_sections(db.conn, summary.key).tracks
        }
        for summary in summaries
    }
    assert tracks_by_provider == {
        "discogs:101": {first_track},
        "discogs:202": {second_track},
        "": {legacy_track},
    }
    db.close()


def test_artist_upsert_rejects_provider_conflicts_and_keeps_legacy_fallback(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "conflicts.sqlite3")
    service = ArtistCreditService(db)
    first = service.upsert_artist(
        "Same Name",
        discogs_artist_id="101",
        musicbrainz_artist_id="mb-first",
    )
    second = service.upsert_artist(
        "Same Name",
        discogs_artist_id="202",
        musicbrainz_artist_id="mb-second",
    )
    assert first.id != second.id

    with pytest.raises(ValueError, match="conflicting artist identities"):
        service.upsert_artist(
            "Same Name",
            discogs_artist_id="101",
            musicbrainz_artist_id="mb-second",
        )
    with pytest.raises(ValueError, match="MusicBrainz artist ID cannot be reassigned"):
        service.upsert_artist(
            "Same Name",
            discogs_artist_id="101",
            musicbrainz_artist_id="mb-reassigned",
        )

    fallback = service.upsert_artist("Same Name")
    assert fallback.id not in {first.id, second.id}
    assert service.upsert_artist(" same   name ").id == fallback.id
    assert fallback.discogs_artist_id is None
    assert fallback.musicbrainz_artist_id is None
    db.close()


def test_early_v6_unique_name_schema_upgrades_without_losing_artist_graph(
    tmp_path: Path,
):
    path = tmp_path / "early-v6.sqlite3"
    db = MusicVaultDB(path)
    track_id = _track(db, tmp_path, "preserved", "Preserved Artist")
    original_credit = db.conn.execute(
        "SELECT id, artist_id FROM track_artist_credits WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert original_credit is not None
    db.close()

    # Reproduce the short-lived prerelease v6 uniqueness surface.  Reopening
    # through MusicVaultDB must repair it additively while preserving IDs.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE track_artist_credits RENAME TO credits_current")
        conn.execute("ALTER TABLE artists RENAME TO artists_current")
        conn.execute(
            """
            CREATE TABLE artists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL CHECK (TRIM(display_name) != ''),
                normalized_name TEXT NOT NULL UNIQUE CHECK (TRIM(normalized_name) != ''),
                sort_name TEXT NOT NULL CHECK (TRIM(sort_name) != ''),
                entity_type TEXT NOT NULL DEFAULT 'unknown',
                discogs_artist_id TEXT,
                musicbrainz_artist_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE track_artist_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                artist_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                credit_order INTEGER NOT NULL,
                join_phrase TEXT NOT NULL DEFAULT '',
                provenance TEXT NOT NULL,
                provider_reference TEXT,
                confidence REAL,
                is_manual INTEGER NOT NULL DEFAULT 0,
                is_locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (track_id, artist_id, role),
                UNIQUE (track_id, credit_order),
                FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE RESTRICT
            )
            """
        )
        conn.execute("INSERT INTO artists SELECT * FROM artists_current")
        conn.execute(
            "INSERT INTO track_artist_credits SELECT * FROM credits_current"
        )
        conn.execute("DROP TABLE credits_current")
        conn.execute("DROP TABLE artists_current")

    reopened = MusicVaultDB(path)
    assert not _single_column_is_unique(reopened.conn, "normalized_name")
    preserved = reopened.conn.execute(
        "SELECT id, artist_id FROM track_artist_credits WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert tuple(preserved) == tuple(original_credit)
    assert reopened.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    service = ArtistCreditService(reopened)
    first = service.upsert_artist("Preserved Artist", discogs_artist_id="101")
    second = service.upsert_artist("Preserved Artist", discogs_artist_id="202")
    assert first.id != second.id
    reopened.close()
