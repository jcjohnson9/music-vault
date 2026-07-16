from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.metadata.artist_credits import (
    ArtistCreditInput,
    ArtistCreditService,
    seed_existing_artist_credits,
)
from music_vault.metadata.intelligence_schema import (
    INTELLIGENCE_ITEMS_TABLE,
    INTELLIGENCE_JOBS_TABLE,
    MetadataIntelligenceJobStore,
    required_intelligence_indexes,
)
from music_vault.metadata.service import (
    AutomaticMetadataField,
    MetadataAction,
    MetadataService,
)


def _track(db: MusicVaultDB, path: Path, *, artist: str = "Synthetic Duo & Co") -> int:
    return db.upsert_track(
        path,
        title="Synthetic Signal",
        artist=artist,
        album="Synthetic Collection",
    )


def _downgrade_metadata_checks_to_v5(path: Path) -> None:
    """Create a realistic v5 CHECK surface while retaining Batch 10 tables."""

    old_fields = "'title','artist','album','album_artist','release_date','artwork'"
    old_observations = old_fields + ",'source_upload_date','source_video_id'"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "metadata_intelligence_items",
            "metadata_intelligence_jobs",
            "track_release_context",
            "track_artist_credits",
            "artists",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table in (
            "track_metadata_history",
            "track_metadata_observations",
            "track_metadata_fields",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            f"""
            CREATE TABLE track_metadata_fields (
                track_id INTEGER NOT NULL,
                field_name TEXT NOT NULL CHECK (field_name IN ({old_fields})),
                value TEXT, provenance TEXT NOT NULL, provider_reference TEXT,
                confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
                is_manual INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0,1)),
                is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0,1)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(track_id,field_name),
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE track_metadata_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_key TEXT NOT NULL UNIQUE,
                track_id INTEGER NOT NULL, provider TEXT NOT NULL,
                field_name TEXT NOT NULL CHECK (field_name IN ({old_observations})),
                value TEXT, provider_reference TEXT,
                confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
                observed_at TEXT NOT NULL,
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE track_metadata_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_group_id TEXT NOT NULL, track_id INTEGER NOT NULL,
                field_name TEXT NOT NULL CHECK (field_name IN ({old_fields})),
                old_value TEXT, new_value TEXT, old_provenance TEXT,
                new_provenance TEXT, old_provider_reference TEXT,
                new_provider_reference TEXT, old_confidence REAL,
                new_confidence REAL, old_is_manual INTEGER NOT NULL,
                new_is_manual INTEGER NOT NULL, old_is_locked INTEGER NOT NULL,
                new_is_locked INTEGER NOT NULL, actor TEXT NOT NULL,
                reason TEXT NOT NULL, changed_at TEXT NOT NULL,
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO track_metadata_fields (
                track_id,field_name,value,provenance,confidence,
                is_manual,is_locked,updated_at
            ) VALUES(1,'artist','Synthetic Duo & Co','manual',100,1,1,'t0')
            """
        )
        conn.execute(
            """
            INSERT INTO track_metadata_observations (
                observation_key,track_id,provider,field_name,value,confidence,observed_at
            ) VALUES('existing-observation',1,'embedded','artist','Synthetic Duo & Co',80,'t0')
            """
        )
        conn.execute(
            """
            INSERT INTO track_metadata_history (
                change_group_id,track_id,field_name,old_value,new_value,
                old_provenance,new_provenance,old_confidence,new_confidence,
                old_is_manual,new_is_manual,old_is_locked,new_is_locked,
                actor,reason,changed_at
            ) VALUES('existing-history',1,'artist','Old Duo','Synthetic Duo & Co',
                     'embedded','manual',80,100,0,1,0,1,'user','edit','t0')
            """
        )
        conn.execute("PRAGMA user_version=5")


def test_new_database_initializes_schema_v6_models_and_constraints(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "new.sqlite3")
    assert CURRENT_SCHEMA_VERSION == 6
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(tracks)")}
    assert {
        "original_release_date",
        "version_type",
        "version_label",
        "discogs_release_id",
        "discogs_master_id",
        "discogs_track_position",
        "recording_group_key",
    } <= columns
    tables = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "artists",
        "track_artist_credits",
        "track_release_context",
        INTELLIGENCE_JOBS_TABLE,
        INTELLIGENCE_ITEMS_TABLE,
    } <= tables
    indexes = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert set(required_intelligence_indexes()) <= indexes
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    track_id = _track(db, tmp_path / "version.media")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE tracks SET version_type='bootleg-ish' WHERE id=?", (track_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE track_metadata_fields SET value='bootleg-ish' "
            "WHERE track_id=? AND field_name='version_type'",
            (track_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE track_artist_credits SET role='label' WHERE track_id=?",
            (track_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE track_artist_credits SET credit_order=-1 WHERE track_id=?",
            (track_id,),
        )
    db.close()


def test_v5_migration_verified_backup_preserves_state_and_seeds_one_exact_credit(
    tmp_path: Path,
):
    path = tmp_path / "library.sqlite3"
    setup = MusicVaultDB(path)
    track_id = _track(setup, tmp_path / "existing.media")
    playlist_id = setup.create_playlist("Synthetic List")
    setup.add_track_to_playlist(playlist_id, track_id)
    setup.conn.execute(
        """
        INSERT INTO metadata_remediation_jobs(
            id,created_at,updated_at,mode,provider,library_revision
        ) VALUES('preserved-job','t0','t0','dry_run','synthetic','revision')
        """
    )
    setup.conn.commit()
    setup.close()
    _downgrade_metadata_checks_to_v5(path)

    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert db.last_migration_backup and db.last_migration_backup.is_file()
    with sqlite3.connect(db.last_migration_backup) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
        baseline_counts = {
            str(row[0]): int(
                backup.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
            )
            for row in backup.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    current_counts = {
        str(row[0]): int(
            db.conn.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
        )
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert all(current_counts[table] >= count for table, count in baseline_counts.items())

    assert db.conn.execute("SELECT artist FROM tracks WHERE id=1").fetchone()[0] == "Synthetic Duo & Co"
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_track_origins").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_remediation_jobs").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM track_metadata_observations").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0] == 1
    fields = {
        row[0] for row in db.conn.execute("SELECT field_name FROM track_metadata_fields")
    }
    assert {"artist", "original_release_date", "version_type", "version_label"} <= fields
    credit = db.conn.execute(
        """
        SELECT artist.display_name,artist.entity_type,credit.role,
               credit.is_manual,credit.is_locked
        FROM track_artist_credits credit JOIN artists artist ON artist.id=credit.artist_id
        """
    ).fetchone()
    assert tuple(credit) == ("Synthetic Duo & Co", "unknown", "primary", 1, 1)
    assert db.conn.execute("SELECT COUNT(*) FROM track_artist_credits").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()

    before = len(list(backups.glob("*.sqlite3")))
    reopened = MusicVaultDB(path, backup_dir=backups)
    assert reopened.last_migration_backup is None
    assert len(list(backups.glob("*.sqlite3"))) == before
    assert reopened.conn.execute("SELECT COUNT(*) FROM track_artist_credits").fetchone()[0] == 1
    reopened.close()


def test_artist_credits_are_structured_ordered_and_respect_artist_lock(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "credits.sqlite3")
    track_id = _track(db, tmp_path / "credits.media", artist="Legacy & Band")
    seed_existing_artist_credits(db.conn, (track_id,))
    service = ArtistCreditService(db)
    seeded = service.track_credits(track_id)
    assert len(seeded) == 1
    assert seeded[0].artist.display_name == "Legacy & Band"

    credits = service.replace_track_credits(
        track_id,
        (
            ArtistCreditInput("Primary Unit", entity_type="group", discogs_artist_id="10"),
            ArtistCreditInput(
                "Guest Person",
                role="featured",
                join_phrase="feat.",
                entity_type="person",
                discogs_artist_id="20",
            ),
        ),
        provenance="discogs_high_confidence",
        provider_reference="release:100",
        confidence=96,
    )
    assert [credit.role for credit in credits] == ["primary", "featured"]
    assert [credit.credit_order for credit in credits] == [0, 1]
    assert service.formatted_credit(credits) == "Primary Unit feat. Guest Person"
    assert db.get_track(track_id)["artist"] == "Primary Unit feat. Guest Person"
    assert db.conn.execute(
        "SELECT entity_type FROM artists WHERE discogs_artist_id='10'"
    ).fetchone()[0] == "group"

    MetadataService(db).apply_actions(track_id, {"artist": MetadataAction.lock()})
    unchanged = service.replace_track_credits(
        track_id,
        (ArtistCreditInput("Should Not Replace"),),
        provenance="discogs_high_confidence",
        confidence=99,
    )
    assert [credit.artist.display_name for credit in unchanged] == [
        "Primary Unit",
        "Guest Person",
    ]
    with pytest.raises(ValueError, match="primary"):
        service.replace_track_credits(
            track_id,
            (ArtistCreditInput("Only Guest", role="featured"),),
            provenance="manual",
            is_manual=True,
        )
    with pytest.raises(ValueError, match="Duplicate"):
        service.replace_track_credits(
            track_id,
            (
                ArtistCreditInput("Repeated Artist"),
                ArtistCreditInput("Repeated Artist"),
            ),
            provenance="manual",
            is_manual=True,
        )
    db.close()


def test_flat_artist_edit_reconciles_one_unsplit_locked_credit(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "flat-credit.sqlite3")
    track_id = _track(db, tmp_path / "flat-credit.media", artist="Old Unit")
    credits = ArtistCreditService(db)
    credits.replace_track_credits(
        track_id,
        (
            ArtistCreditInput("Old Unit"),
            ArtistCreditInput("Old Guest", role="featured", join_phrase="feat."),
        ),
        provenance="discogs_high_confidence",
        confidence=97,
    )

    MetadataService(db).apply_manual_patch(
        track_id,
        {"artist": "New Unit & Unsplit Name"},
    )
    stored = credits.track_credits(track_id)
    assert len(stored) == 1
    assert stored[0].artist.display_name == "New Unit & Unsplit Name"
    assert stored[0].artist.entity_type == "unknown"
    assert stored[0].role == "primary"
    assert stored[0].is_manual is True
    assert stored[0].is_locked is True
    assert db.get_track(track_id)["artist"] == "New Unit & Unsplit Name"
    db.close()


def test_field_level_automatic_apply_uses_confidence_locks_dates_and_history(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "fields.sqlite3")
    track_id = _track(db, tmp_path / "fields.media", artist="Current Artist")
    metadata = MetadataService(db)

    medium = metadata.apply_automatic_fields(
        track_id,
        {"album": AutomaticMetadataField("Uncertain Album", 74, provider="discogs")},
    )
    assert not medium.changed
    assert metadata.snapshot(track_id).value("album") == "Synthetic Collection"
    assert metadata.observations(track_id, "album")[0].value == "Uncertain Album"

    high = metadata.apply_automatic_fields(
        track_id,
        {
            "title": AutomaticMetadataField("Catalogue Title", 97, provider="discogs"),
            "original_release_date": AutomaticMetadataField("1984-03", 96, provider="discogs"),
            "version_type": AutomaticMetadataField("Live", 95, provider="discogs"),
            "version_label": AutomaticMetadataField("Live at Synthetic Hall", 95, provider="discogs"),
            "album": AutomaticMetadataField("Conflict Album", 99, provider="discogs", conflict=True),
        },
        provider_reference="release:101",
    )
    assert high.changed_fields == {
        "title",
        "original_release_date",
        "version_type",
        "version_label",
    }
    row = db.get_track(track_id)
    assert row["title"] == "Catalogue Title"
    assert row["original_release_date"] == "1984-03"
    assert row["version_type"] == "live"
    assert row["version_label"] == "Live at Synthetic Hall"
    assert row["album"] == "Synthetic Collection"
    assert {entry.field_name for entry in metadata.history_groups(track_id)[0].entries} == high.changed_fields

    weaker_same_authority = metadata.apply_automatic_fields(
        track_id,
        {"title": AutomaticMetadataField("Weaker Replacement", 90, provider="discogs")},
    )
    assert not weaker_same_authority.changed
    assert metadata.snapshot(track_id).value("title") == "Catalogue Title"

    metadata.apply_actions(track_id, {"title": MetadataAction.set("Locked Manual")})
    locked = metadata.apply_automatic_fields(
        track_id,
        {"title": AutomaticMetadataField("Provider Override", 100, provider="discogs")},
    )
    assert not locked.changed
    assert metadata.snapshot(track_id).value("title") == "Locked Manual"

    youtube_date = metadata.record_source_observations(
        track_id,
        provider="youtube_title_parsed",
        values={"release_date": "2024", "original_release_date": "1984"},
        confidence=99,
    )
    assert not youtube_date.changed
    assert metadata.snapshot(track_id).value("release_date") is None
    db.close()


def test_intelligence_jobs_deduplicate_resume_and_store_only_bounded_summaries(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "jobs.sqlite3")
    first = _track(db, tmp_path / "first.media")
    second = _track(db, tmp_path / "second.media")
    store = MetadataIntelligenceJobStore(db)

    queued = store.enqueue_track(first)
    assert store.enqueue_track(first).id == queued.id
    claimed = store.claim_next_item()
    assert claimed and claimed.track_id == first and claimed.attempt_count == 1
    completed = store.mark_item(
        claimed.id,
        "review",
        parsed_hints={"version_type": "live"},
        field_proposal={"title": "Synthetic"},
        field_confidence={"title": 78},
        provider_agreement="conflict",
        review_reason="Provider disagreement",
    )
    assert completed.state == "review"
    assert store.aggregate_counts()["total"] == 1

    job_id = store.create_existing_library_job([first, first, second])
    assert store.job_summary(job_id).total_items == 2
    store.pause(job_id)
    assert store.claim_next_item(job_id) is None
    store.resume(job_id)
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(item.id, "applied", applied_history_group="group-1")
    next_item = store.claim_next_item(job_id)
    assert next_item is not None and next_item.track_id != item.track_id
    store.mark_item(next_item.id, "no_match")
    assert store.job_summary(job_id).status == "complete_with_issues"

    retry = store.enqueue_track(second)
    claimed_retry = store.claim_next_item()
    assert claimed_retry and claimed_retry.id == retry.id
    with pytest.raises(ValueError, match="Raw provider"):
        store.mark_item(claimed_retry.id, "failed", field_proposal={"raw_response": {}})
    with pytest.raises(ValueError, match="Raw provider"):
        store.mark_item(
            claimed_retry.id,
            "failed",
            field_proposal={"candidate": {"authorization": "private"}},
        )
    store.mark_item(claimed_retry.id, "failed", error="Authorization: Discogs token=secret")
    error = db.conn.execute(
        "SELECT last_error FROM metadata_intelligence_items WHERE id=?",
        (claimed_retry.id,),
    ).fetchone()[0]
    assert "secret" not in error
    db.close()


def test_intelligence_pause_and_interruption_recovery_are_persistent(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "resume.sqlite3")
    first = _track(db, tmp_path / "resume-first.media")
    second = _track(db, tmp_path / "resume-second.media")
    store = MetadataIntelligenceJobStore(db)

    paused_job = store.create_existing_library_job([first, second])
    in_flight = store.claim_next_item(paused_job)
    assert in_flight is not None
    store.pause(paused_job)
    store.mark_item(in_flight.id, "applied")
    assert store.job_summary(paused_job).status == "paused"
    assert store.claim_next_item(paused_job) is None
    store.resume(paused_job)
    assert store.claim_next_item(paused_job) is not None

    interrupted_job = store.create_existing_library_job([first])
    interrupted = store.claim_next_item(interrupted_job)
    assert interrupted is not None and interrupted.attempt_count == 1
    assert store.recover_interrupted(interrupted_job) == 1
    reclaimed = store.claim_next_item(interrupted_job)
    assert reclaimed is not None
    assert reclaimed.id == interrupted.id
    assert reclaimed.attempt_count == 2

    empty_job = store.create_existing_library_job([])
    empty = store.job_summary(empty_job)
    assert empty.total_items == 0
    assert empty.status == "complete"
    with pytest.raises(ValueError, match="active"):
        store.pause(empty_job)
    db.close()
