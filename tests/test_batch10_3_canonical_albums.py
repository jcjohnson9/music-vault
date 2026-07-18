from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.metadata.canonical_albums import (
    ARTIST_ALIASES_TABLE,
    ARTIST_RELATIONSHIPS_TABLE,
    CANONICAL_ALBUMS_TABLE,
    TRACK_ALBUM_MEMBERSHIPS_TABLE,
    analyze_canonical_album_backfill,
    canonical_album_identity,
    classify_album_kind,
    representative_album_cover,
    representative_album_covers,
    required_canonical_media_indexes,
    seed_existing_canonical_albums,
    split_edition_label,
    upsert_track_canonical_album,
)
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore


def _insert_track(
    db: MusicVaultDB,
    path: Path,
    *,
    title: str,
    artist: str,
    album: str | None,
    album_artist: str | None,
    release_date: str | None = None,
    cover_path: Path | None = None,
    discogs_release_id: str | None = None,
    discogs_master_id: str | None = None,
) -> int:
    return int(
        db.conn.execute(
            """
            INSERT INTO tracks (
                path,title,artist,album,album_artist,release_date,year,cover_path,
                discogs_release_id,discogs_master_id,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (
                str(path),
                title,
                artist,
                album,
                album_artist,
                release_date,
                release_date[:4] if release_date else None,
                str(cover_path) if cover_path else None,
                discogs_release_id,
                discogs_master_id,
            ),
        ).lastrowid
    )


def _seed_field_rows(db: MusicVaultDB) -> None:
    from music_vault.metadata.intelligence_schema import (
        seed_existing_metadata_field_extensions,
    )
    from music_vault.metadata.schema import seed_existing_metadata

    seed_existing_metadata(db.conn)
    seed_existing_metadata_field_extensions(db.conn)


def test_album_identity_is_provider_first_and_edition_year_cover_independent():
    original = canonical_album_identity(
        "Synthetic Record",
        "The Artist",
        discogs_master_id="123",
    )
    deluxe = canonical_album_identity(
        "Synthetic Record (Deluxe Edition)",
        "The Artist",
        discogs_master_id="123",
    )
    release_group = canonical_album_identity(
        "Different Provider Display",
        "Different Display",
        musicbrainz_release_group_id="group-id",
    )
    assert original.canonical_key == deluxe.canonical_key == "discogs-master:123"
    assert deluxe.title == "Synthetic Record"
    assert deluxe.edition_label == "Deluxe Edition"
    assert release_group.canonical_key == "musicbrainz-release-group:group-id"

    fallback = canonical_album_identity("Synthetic Record", "The Artist")
    fallback_deluxe = canonical_album_identity(
        "Synthetic Record — Remastered", "The Artist"
    )
    assert fallback.canonical_key == fallback_deluxe.canonical_key
    assert canonical_album_identity("Synthetic Record Live", "The Artist").canonical_key != (
        fallback.canonical_key
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("A Live Album", "live_album"),
        ("Original Motion Picture Soundtrack", "soundtrack"),
        ("Original Motion Picture Score", "score"),
        ("Original Broadway Cast Recording", "cast_recording"),
        ("Greatest Hits", "greatest_hits"),
        ("The Remix Album", "remix_album"),
        ("Demo Collection", "demo_collection"),
        ("A Compilation", "compilation"),
        ("Small EP", "ep"),
        ("Debut Album", "album"),
    ],
)
def test_album_kind_preserves_distinct_work_classes(title: str, expected: str):
    assert classify_album_kind(title) == expected


def test_edition_parser_does_not_strip_meaningful_work_identity():
    assert split_edition_label("Record (Expanded Edition)") == (
        "Record",
        "Expanded Edition",
    )
    assert split_edition_label("Record: Live at the Hall") == (
        "Record: Live at the Hall",
        None,
    )
    assert split_edition_label("Original Broadway Cast") == (
        "Original Broadway Cast",
        None,
    )


def test_schema_v7_tables_indexes_constraints_and_field_level_outcomes(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "schema.sqlite3")
    assert CURRENT_SCHEMA_VERSION == 7
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    tables = {
        str(row[0])
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        CANONICAL_ALBUMS_TABLE,
        TRACK_ALBUM_MEMBERSHIPS_TABLE,
        ARTIST_ALIASES_TABLE,
        ARTIST_RELATIONSHIPS_TABLE,
    } <= tables
    indexes = {
        str(row[0])
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert set(required_canonical_media_indexes()) <= indexes

    job_columns = {
        str(row[1])
        for row in db.conn.execute("PRAGMA table_info(metadata_intelligence_jobs)")
    }
    assert {"applied_with_gaps_items", "source_fallback_items"} <= job_columns

    first_artist = db.conn.execute(
        """
        INSERT INTO artists (
            display_name,normalized_name,sort_name,entity_type,created_at,updated_at
        ) VALUES ('Artist One','artist one','artist one','person','t0','t0')
        """
    ).lastrowid
    second_artist = db.conn.execute(
        """
        INSERT INTO artists (
            display_name,normalized_name,sort_name,entity_type,created_at,updated_at
        ) VALUES ('Group One','group one','group one','group','t0','t0')
        """
    ).lastrowid
    db.conn.execute(
        """
        INSERT INTO artist_aliases (
            artist_id,alias_name,normalized_alias,alias_kind,provenance,created_at
        ) VALUES (?, 'Artist 1', 'artist 1', 'display_variant', 'manual', 't0')
        """,
        (first_artist,),
    )
    db.conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,provenance,
            confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','discogs',100,'t0','t0')
        """,
        (first_artist, second_artist),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO artist_relationships (
                subject_artist_id,related_artist_id,relationship_kind,provenance,
                created_at,updated_at
            ) VALUES (?,?,'member_of','manual','t0','t0')
            """,
            (first_artist, first_artist),
        )
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_schema6_migration_creates_verified_backup_and_preserves_legacy_track_fields(
    tmp_path: Path,
):
    path = tmp_path / "library.sqlite3"
    backups = tmp_path / "backups"
    cover_a = tmp_path / "cover-a.jpg"
    cover_b = tmp_path / "cover-b.jpg"
    cover_a.write_bytes(b"cover-a")
    cover_b.write_bytes(b"cover-b")
    db = MusicVaultDB(path, backup_dir=backups)
    first = _insert_track(
        db,
        tmp_path / "one.flac",
        title="One",
        artist="Artist",
        album="Canonical Record",
        album_artist="Artist",
        release_date="1999-01-01",
        cover_path=cover_a,
        discogs_release_id="release-a",
        discogs_master_id="master-1",
    )
    second = _insert_track(
        db,
        tmp_path / "two.flac",
        title="Two",
        artist="Artist",
        album="Canonical Record (Deluxe Edition)",
        album_artist="Artist",
        release_date="2009",
        cover_path=cover_b,
        discogs_release_id="release-b",
        discogs_master_id="master-1",
    )
    _seed_field_rows(db)
    before = [
        tuple(row)
        for row in db.conn.execute(
            """
            SELECT id,path,title,artist,album,album_artist,release_date,year,
                   cover_path,discogs_release_id,discogs_master_id
            FROM tracks ORDER BY id
            """
        )
    ]
    db.conn.execute("DROP TABLE track_album_memberships")
    db.conn.execute("DROP TABLE canonical_albums")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    db.close()

    migrated = MusicVaultDB(path, backup_dir=backups)
    assert migrated.last_migration_backup is not None
    assert migrated.last_migration_backup.is_file()
    assert migrated.last_migration_backup.stat().st_size > 0
    with sqlite3.connect(migrated.last_migration_backup) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    after = [
        tuple(row)
        for row in migrated.conn.execute(
            """
            SELECT id,path,title,artist,album,album_artist,release_date,year,
                   cover_path,discogs_release_id,discogs_master_id
            FROM tracks ORDER BY id
            """
        )
    ]
    assert after == before
    assert migrated.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 1
    assert migrated.conn.execute("SELECT COUNT(*) FROM track_album_memberships").fetchone()[0] == 2
    memberships = migrated.conn.execute(
        """
        SELECT track_id,discogs_release_id,edition_label
        FROM track_album_memberships ORDER BY track_id
        """
    ).fetchall()
    assert [tuple(row) for row in memberships] == [
        (first, "release-a", None),
        (second, "release-b", "Deluxe Edition"),
    ]
    assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    migrated.close()

    backup_count = len(list(backups.glob("*.sqlite3")))
    reopened = MusicVaultDB(path, backup_dir=backups)
    assert reopened.last_migration_backup is None
    assert len(list(backups.glob("*.sqlite3"))) == backup_count
    assert reopened.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 1
    assert reopened.conn.execute("SELECT COUNT(*) FROM track_album_memberships").fetchone()[0] == 2
    reopened.close()


def test_schema6_migration_accepts_only_reported_safe_artist_reductions(
    tmp_path: Path,
):
    path = tmp_path / "duplicate-artists.sqlite3"
    db = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    first_track = _insert_track(
        db,
        tmp_path / "one.flac",
        title="One",
        artist="Synthetic Artist",
        album="Record",
        album_artist="Synthetic Artist",
    )
    second_track = _insert_track(
        db,
        tmp_path / "two.flac",
        title="Two",
        artist="Synthetic-Artist",
        album="Record",
        album_artist="Synthetic-Artist",
    )
    artist_ids = []
    for display in ("Synthetic Artist", "Synthetic-Artist"):
        artist_ids.append(
            int(
                db.conn.execute(
                    """
                    INSERT INTO artists (
                        display_name,normalized_name,sort_name,entity_type,
                        created_at,updated_at
                    ) VALUES (?, 'synthetic artist', 'synthetic artist', 'person',
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (display,),
                ).lastrowid
            )
        )
    for track_id, artist_id in zip(
        (first_track, second_track), artist_ids, strict=True
    ):
        db.conn.execute(
            """
            INSERT INTO track_artist_credits (
                track_id,artist_id,role,credit_order,join_phrase,provenance,
                confidence,is_manual,is_locked,created_at,updated_at
            ) VALUES (?,?,'primary',0,'','legacy',100,0,0,
                      CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (track_id, artist_id),
        )
    db.conn.execute("DROP TABLE track_album_memberships")
    db.conn.execute("DROP TABLE canonical_albums")
    db.conn.execute("DROP TABLE artist_relationships")
    db.conn.execute("DROP TABLE artist_aliases")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    db.close()

    migrated = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert migrated.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert migrated.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 1
    assert migrated.conn.execute(
        "SELECT COUNT(*) FROM track_artist_credits"
    ).fetchone()[0] == 2
    assert migrated.conn.execute(
        "SELECT COUNT(*) FROM canonical_albums"
    ).fetchone()[0] == 1
    assert migrated.conn.execute("SELECT COUNT(*) FROM artist_aliases").fetchone()[0] == 1
    assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated.close()


def test_dry_run_is_aggregate_only_and_does_not_write(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "dry-run.sqlite3")
    _insert_track(
        db,
        tmp_path / "one.flac",
        title="One",
        artist="Artist",
        album="Record (Special Edition)",
        album_artist="Artist",
    )
    _insert_track(
        db,
        tmp_path / "unknown.flac",
        title="Unknown",
        artist="Artist",
        album=None,
        album_artist=None,
    )
    before_changes = db.conn.total_changes
    report = analyze_canonical_album_backfill(db.conn)
    assert report == {
        "track_count": 2,
        "eligible_track_count": 1,
        "missing_album_count": 1,
        "existing_membership_count": 0,
        "proposed_membership_count": 1,
        "proposed_canonical_album_count": 1,
        "ambiguous_group_count": 0,
        "edition_label_count": 1,
        "identity_strategy_counts": {"fallback": 1},
        "album_kind_counts": {"album": 1},
        "would_modify_track_rows": 0,
        "would_modify_media_files": 0,
        "would_modify_artwork_files": 0,
    }
    assert db.conn.total_changes == before_changes
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 0
    db.close()


def test_dry_run_counts_partial_provider_coverage_as_one_safe_group(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "partial-provider-dry-run.sqlite3")
    _insert_track(
        db,
        tmp_path / "provider.flac",
        title="Provider Track",
        artist="Synthetic Artist",
        album="Shared Record",
        album_artist="Synthetic Artist",
        discogs_master_id="master-shared",
    )
    _insert_track(
        db,
        tmp_path / "fallback.flac",
        title="Fallback Track",
        artist="Synthetic Artist",
        album="Shared Record",
        album_artist="Synthetic Artist",
    )
    before_changes = db.conn.total_changes

    report = analyze_canonical_album_backfill(db.conn)

    assert report["proposed_canonical_album_count"] == 1
    assert report["ambiguous_group_count"] == 0
    assert db.conn.total_changes == before_changes
    seed_existing_canonical_albums(db.conn)
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 1
    db.close()


def test_dry_run_reports_conflicting_strong_album_identities_as_ambiguous(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "conflicting-provider-dry-run.sqlite3")
    for index, master_id in enumerate(("master-one", "master-two"), start=1):
        _insert_track(
            db,
            tmp_path / f"provider-{index}.flac",
            title=f"Provider Track {index}",
            artist="Synthetic Artist",
            album="Shared Display",
            album_artist="Synthetic Artist",
            discogs_master_id=master_id,
        )
    before_changes = db.conn.total_changes

    report = analyze_canonical_album_backfill(db.conn)

    assert report["proposed_canonical_album_count"] == 0
    assert report["ambiguous_group_count"] == 1
    assert db.conn.total_changes == before_changes
    seed_existing_canonical_albums(db.conn)
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 2
    db.close()


def test_fallback_groups_year_and_cover_editions_but_not_live_work(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "fallback.sqlite3")
    first_cover = tmp_path / "first.jpg"
    second_cover = tmp_path / "second.jpg"
    first_cover.write_bytes(b"first")
    second_cover.write_bytes(b"second")
    _insert_track(
        db,
        tmp_path / "original.flac",
        title="Original",
        artist="Artist",
        album="Record",
        album_artist="Artist",
        release_date="1990",
        cover_path=first_cover,
    )
    _insert_track(
        db,
        tmp_path / "deluxe.flac",
        title="Deluxe",
        artist="Artist",
        album="Record (Deluxe Edition)",
        album_artist="Artist",
        release_date="2020",
        cover_path=second_cover,
    )
    _insert_track(
        db,
        tmp_path / "live.flac",
        title="Live",
        artist="Artist",
        album="Record Live",
        album_artist="Artist",
        release_date="2021",
    )
    _seed_field_rows(db)
    seed_existing_canonical_albums(db.conn)
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 2
    grouped = db.conn.execute(
        """
        SELECT album.album_kind,COUNT(*)
        FROM canonical_albums album
        JOIN track_album_memberships membership
          ON membership.canonical_album_id=album.id
        GROUP BY album.id,album.album_kind ORDER BY album.album_kind
        """
    ).fetchall()
    assert [tuple(row) for row in grouped] == [("album", 2), ("live_album", 1)]
    db.close()


def test_score_uses_release_context_across_performers_and_stays_distinct(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "score.sqlite3")
    for index, artist in enumerate(("Composer One", "Orchestra Two"), start=1):
        _insert_track(
            db,
            tmp_path / f"score-{index}.flac",
            title=f"Cue {index}",
            artist=artist,
            album="Synthetic Motion Picture Score",
            album_artist=None,
        )
    _insert_track(
        db,
        tmp_path / "soundtrack.flac",
        title="Featured Song",
        artist="Performer Three",
        album="Synthetic Motion Picture Soundtrack",
        album_artist=None,
    )

    seed_existing_canonical_albums(db.conn)
    grouped = db.conn.execute(
        """
        SELECT album.album_kind,album.album_artist_display,COUNT(*) AS track_count
        FROM canonical_albums AS album
        JOIN track_album_memberships AS membership
          ON membership.canonical_album_id=album.id
        GROUP BY album.id
        ORDER BY album.album_kind
        """
    ).fetchall()

    assert [tuple(row) for row in grouped] == [
        ("score", "Various Artists", 2),
        ("soundtrack", "Various Artists", 1),
    ]
    db.close()


def test_post_init_track_upsert_creates_idempotent_durable_membership(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "incremental.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "new.flac",
        title="New Track",
        artist="Artist",
        album="New Record",
        album_artist="Artist",
        release_date="2024",
    )
    membership = db.conn.execute(
        """
        SELECT membership.track_id,album.title,album.album_kind
        FROM track_album_memberships membership
        JOIN canonical_albums album ON album.id=membership.canonical_album_id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert tuple(membership) == (track_id, "New Record", "album")
    before = db.conn.total_changes
    assert upsert_track_canonical_album(db.conn, track_id) == int(
        db.conn.execute(
            "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
            (track_id,),
        ).fetchone()[0]
    )
    assert db.conn.total_changes == before
    db.close()


def test_metadata_album_change_reassigns_fallback_and_provider_identity_promotes_it(
    tmp_path: Path,
):
    from music_vault.metadata.service import MetadataAction, MetadataService

    db = MusicVaultDB(tmp_path / "promotion.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "track.flac",
        title="Track",
        artist="Artist",
        album="Working Title",
        album_artist="Artist",
    )
    first_id = int(
        db.conn.execute(
            "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
            (track_id,),
        ).fetchone()[0]
    )
    MetadataService(db).apply_actions(
        track_id,
        {"album": MetadataAction.set("Accepted Record (Deluxe Edition)")},
    )
    fallback = db.conn.execute(
        """
        SELECT membership.canonical_album_id,membership.edition_label,album.canonical_key
        FROM track_album_memberships membership
        JOIN canonical_albums album ON album.id=membership.canonical_album_id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert int(fallback["canonical_album_id"]) != first_id
    assert fallback["edition_label"] == "Deluxe Edition"
    assert str(fallback["canonical_key"]).startswith("fallback:")

    db.conn.execute(
        "UPDATE tracks SET discogs_master_id='master-promoted' WHERE id=?",
        (track_id,),
    )
    promoted_id = upsert_track_canonical_album(db.conn, track_id)
    promoted = db.conn.execute(
        """
        SELECT membership.canonical_album_id,album.canonical_key
        FROM track_album_memberships membership
        JOIN canonical_albums album ON album.id=membership.canonical_album_id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert int(promoted["canonical_album_id"]) == promoted_id
    assert promoted["canonical_key"] == "discogs-master:master-promoted"
    assert db.conn.execute("SELECT album FROM tracks WHERE id=?", (track_id,)).fetchone()[0] == (
        "Accepted Record (Deluxe Edition)"
    )
    db.close()


def test_incremental_membership_does_not_displace_existing_provider_identity(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "provider-conflict.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "track.flac",
        title="Track",
        artist="Artist",
        album="Provider Record",
        album_artist="Artist",
    )
    timestamp = "2026-01-01T00:00:00Z"
    musicbrainz_album_id = int(
        db.conn.execute(
            """
            INSERT INTO canonical_albums (
                canonical_key,title,normalized_title,album_artist_display,
                normalized_album_artist,album_kind,musicbrainz_release_group_id,
                created_at,updated_at
            ) VALUES (
                'musicbrainz-release-group:synthetic-group','Provider Record',
                'provider record','Artist','artist','album','synthetic-group',?,?
            )
            """,
            (timestamp, timestamp),
        ).lastrowid
    )
    db.conn.execute(
        """
        UPDATE track_album_memberships
        SET canonical_album_id=?,provenance='musicbrainz',updated_at=?
        WHERE track_id=?
        """,
        (musicbrainz_album_id, timestamp, track_id),
    )
    db.conn.execute(
        "UPDATE tracks SET discogs_master_id='different-master' WHERE id=?",
        (track_id,),
    )

    assert upsert_track_canonical_album(db.conn, track_id) == musicbrainz_album_id
    assert db.conn.execute(
        "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
        (track_id,),
    ).fetchone()[0] == musicbrainz_album_id
    db.close()


def test_one_membership_per_track_and_representative_cover_is_read_only(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "cover.sqlite3")
    locked_cover = tmp_path / "locked.jpg"
    discogs_cover = tmp_path / "discogs.jpg"
    missing_cover = tmp_path / "missing.jpg"
    locked_cover.write_bytes(b"locked")
    discogs_cover.write_bytes(b"discogs")
    locked = _insert_track(
        db,
        tmp_path / "locked.flac",
        title="Locked",
        artist="Artist",
        album="Record",
        album_artist="Artist",
        cover_path=locked_cover,
    )
    discogs = _insert_track(
        db,
        tmp_path / "discogs.flac",
        title="Discogs",
        artist="Artist",
        album="Record",
        album_artist="Artist",
        cover_path=discogs_cover,
    )
    _insert_track(
        db,
        tmp_path / "missing.flac",
        title="Missing",
        artist="Artist",
        album="Record",
        album_artist="Artist",
        cover_path=missing_cover,
    )
    _seed_field_rows(db)
    db.conn.execute(
        """
        UPDATE track_metadata_fields SET provenance='manual',is_manual=1,is_locked=1
        WHERE track_id=? AND field_name='artwork'
        """,
        (locked,),
    )
    db.conn.execute(
        """
        UPDATE track_metadata_fields SET provenance='discogs',is_manual=0,is_locked=0
        WHERE track_id=? AND field_name='artwork'
        """,
        (discogs,),
    )
    seed_existing_canonical_albums(db.conn)
    album_id = int(db.conn.execute("SELECT id FROM canonical_albums").fetchone()[0])
    before = db.conn.total_changes
    assert representative_album_cover(db.conn, album_id) == str(locked_cover)
    assert representative_album_covers(db.conn, [album_id]) == {
        album_id: str(locked_cover)
    }
    assert db.conn.total_changes == before
    assert [
        str(row[0]) for row in db.conn.execute("SELECT cover_path FROM tracks ORDER BY id")
    ] == [str(locked_cover), str(discogs_cover), str(missing_cover)]

    other_album_id = int(
        db.conn.execute(
            """
            INSERT INTO canonical_albums (
                canonical_key,title,normalized_title,album_artist_display,
                normalized_album_artist,album_kind,created_at,updated_at
            ) VALUES ('fallback:album:other:other','Other','other','Other','other',
                      'album',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """
        ).lastrowid
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO track_album_memberships (
                track_id,canonical_album_id,provenance,created_at,updated_at
            ) VALUES (?,?,'manual',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (locked, other_album_id),
        )
    db.close()


def test_field_level_success_states_round_trip_and_refresh_aggregates(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "outcomes.sqlite3")
    track_a = _insert_track(
        db,
        tmp_path / "a.flac",
        title="A",
        artist="Artist",
        album="Record",
        album_artist="Artist",
    )
    track_b = _insert_track(
        db,
        tmp_path / "b.flac",
        title="B",
        artist="Artist",
        album="Record",
        album_artist="Artist",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_a, track_b])
    first = store.claim_next_item(job_id)
    assert first is not None
    store.mark_item(first.id, "applied_with_gaps")
    second = store.claim_next_item(job_id)
    assert second is not None
    store.mark_item(second.id, "source_fallback")
    summary = store.job_summary(job_id)
    assert summary.status == "complete"
    assert summary.applied_items == 0
    assert summary.applied_with_gaps_items == 1
    assert summary.source_fallback_items == 1
    assert store.aggregate_counts(job_id)["applied_with_gaps"] == 1
    assert store.aggregate_counts(job_id)["source_fallback"] == 1
    db.close()


def test_schema6_item_check_is_rebuilt_additively_and_preserves_queued_work(
    tmp_path: Path,
):
    path = tmp_path / "old-outcomes.sqlite3"
    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)
    track_id = _insert_track(
        db,
        tmp_path / "queued.flac",
        title="Queued",
        artist="Artist",
        album="Record",
        album_artist="Artist",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item_before = tuple(
        db.conn.execute(
            """
            SELECT id,job_id,track_id,state,reason,priority,parsed_hints,
                   field_proposal,field_confidence,attempt_count,created_at,updated_at
            FROM metadata_intelligence_items
            """
        ).fetchone()
    )
    db.conn.commit()
    current_sql = str(
        db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='metadata_intelligence_items'"
        ).fetchone()[0]
    )
    legacy_sql = current_sql.replace(
        "CREATE TABLE metadata_intelligence_items",
        "CREATE TABLE metadata_intelligence_items_v6",
        1,
    ).replace("'applied_with_gaps', 'source_fallback', ", "")
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(legacy_sql)
    columns = [
        str(row[1])
        for row in db.conn.execute("PRAGMA table_info(metadata_intelligence_items)")
    ]
    column_sql = ",".join(columns)
    db.conn.execute(
        f"INSERT INTO metadata_intelligence_items_v6 ({column_sql}) "
        f"SELECT {column_sql} FROM metadata_intelligence_items"
    )
    db.conn.execute("DROP TABLE metadata_intelligence_items")
    db.conn.execute(
        "ALTER TABLE metadata_intelligence_items_v6 RENAME TO metadata_intelligence_items"
    )
    db.conn.execute("DROP TABLE track_album_memberships")
    db.conn.execute("DROP TABLE canonical_albums")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys=ON")
    db.close()

    migrated = MusicVaultDB(path, backup_dir=backups)
    assert migrated.last_migration_backup is not None
    assert tuple(
        migrated.conn.execute(
            """
            SELECT id,job_id,track_id,state,reason,priority,parsed_hints,
                   field_proposal,field_confidence,attempt_count,created_at,updated_at
            FROM metadata_intelligence_items
            """
        ).fetchone()
    ) == item_before
    upgraded_sql = str(
        migrated.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='metadata_intelligence_items'"
        ).fetchone()[0]
    )
    assert "applied_with_gaps" in upgraded_sql
    assert "source_fallback" in upgraded_sql
    migrated_store = MetadataIntelligenceJobStore(migrated)
    claimed = migrated_store.claim_next_item(job_id)
    assert claimed is not None
    migrated_store.mark_item(claimed.id, "source_fallback")
    assert migrated_store.job_summary(job_id).source_fallback_items == 1
    assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    migrated.close()


def test_offline_business_steps_run_only_while_entering_schema7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import music_vault.metadata.artist_consolidation as artist_consolidation
    import music_vault.metadata.review_reclassification as review_reclassification

    calls = {"artists": 0, "review": 0}

    def consolidate_spy(connection: sqlite3.Connection, *, dry_run: bool = False):
        assert isinstance(connection, sqlite3.Connection)
        assert dry_run is False
        calls["artists"] += 1
        return artist_consolidation.ArtistConsolidationReport(
            dry_run=False,
            merge_group_count=0,
            merged_artist_count=0,
            reassigned_credit_count=0,
            aliases_preserved=0,
            relationships_preserved=0,
            version_repairs=0,
            full_credit_repairs=0,
            conflict_count=0,
            deleted_artist_count=0,
        )

    def review_spy(database: object, *, apply: bool = True):
        assert isinstance(database, MusicVaultDB)
        assert apply is True
        calls["review"] += 1

    monkeypatch.setattr(
        artist_consolidation, "consolidate_existing_artists", consolidate_spy
    )
    monkeypatch.setattr(
        review_reclassification, "reclassify_stored_review_items", review_spy
    )

    path = tmp_path / "once.sqlite3"
    new = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert calls == {"artists": 0, "review": 0}
    _insert_track(
        new,
        tmp_path / "track.flac",
        title="Track",
        artist="Artist",
        album="Record",
        album_artist="Artist",
    )
    new.conn.execute("DROP TABLE track_album_memberships")
    new.conn.execute("DROP TABLE canonical_albums")
    new.conn.execute("PRAGMA user_version=6")
    new.conn.commit()
    new.close()

    migrated = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert calls == {"artists": 1, "review": 1}
    migrated.close()
    reopened = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert calls == {"artists": 1, "review": 1}
    reopened.close()
