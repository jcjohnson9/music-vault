from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.library_browser import (
    ArtistKey,
    query_artist_summaries,
    query_artist_track_sections,
)
from music_vault.metadata.artist_consolidation import ArtistConsolidationService
from music_vault.metadata.artist_credits import ArtistCreditService


STAMP = "2026-07-17T00:00:00Z"


def _track(db: MusicVaultDB, root: Path, name: str, artist: str) -> int:
    return db.upsert_track(
        root / f"{name}.synthetic-audio",
        title=name,
        artist=artist,
        album="Synthetic Collection",
    )


def _artist(
    db: MusicVaultDB,
    name: str,
    *,
    kind: str = "unknown",
    discogs: str | None = None,
    musicbrainz: str | None = None,
) -> int:
    return int(
        db.conn.execute(
            """
            INSERT INTO artists (
                display_name, normalized_name, sort_name, entity_type,
                discogs_artist_id, musicbrainz_artist_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, " ".join(name.casefold().split()), name, kind, discogs, musicbrainz, STAMP, STAMP),
        ).lastrowid
    )


def _credit(
    db: MusicVaultDB,
    track_id: int,
    artist_id: int,
    role: str = "primary",
    order: int = 0,
    join: str = "",
    provenance: str = "synthetic_provider",
) -> None:
    db.conn.execute(
        """
        INSERT INTO track_artist_credits (
            track_id, artist_id, role, credit_order, join_phrase,
            provenance, confidence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 100, ?, ?)
        """,
        (track_id, artist_id, role, order, join, provenance, STAMP, STAMP),
    )


def _empty_generated_graph(db: MusicVaultDB) -> None:
    db.conn.execute("DELETE FROM track_artist_credits")
    db.conn.execute("DELETE FROM artists")


def _accepted_artist_proposal(
    db: MusicVaultDB,
    *,
    job_id: str,
    track_id: int,
    provider_key: str,
    artist_name: str,
    id_key: str,
    provider_artist_id: str,
) -> None:
    db.conn.execute(
        """
        INSERT OR IGNORE INTO metadata_intelligence_jobs (
            id,job_kind,status,created_at,updated_at
        ) VALUES (?,'existing_library','complete',?,?)
        """,
        (job_id, STAMP, STAMP),
    )
    proposal = {
        provider_key: {
            "artist_credits": [
                {
                    "name": artist_name,
                    "role": "primary",
                    id_key: provider_artist_id,
                }
            ]
        }
    }
    db.conn.execute(
        """
        INSERT INTO metadata_intelligence_items (
            job_id,track_id,state,field_proposal,field_confidence,
            created_at,updated_at
        ) VALUES (?,?,'applied',?,'{}',?,?)
        """,
        (job_id, track_id, json.dumps(proposal), STAMP, STAMP),
    )


def test_safe_presentation_duplicates_merge_transactionally_and_preserve_credit_details(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "artists.sqlite3", backup_dir=tmp_path / "backups")
    first_track = _track(db, tmp_path, "first", "Aster.Unit")
    second_track = _track(db, tmp_path, "second", "aster unit")
    _empty_generated_graph(db)
    canonical = _artist(db, "Aster Unit", discogs="7001")
    duplicate = _artist(db, "aster-unit")
    _credit(db, first_track, canonical, join="")
    _credit(db, second_track, duplicate, join=" feat. ")
    db.conn.commit()

    service = ArtistConsolidationService(db)
    plan = service.plan()
    assert plan.conflicts == ()
    assert plan.merges[0].canonical_artist_id == canonical
    assert plan.merges[0].duplicate_artist_ids == (duplicate,)
    before_display = [
        tuple(row)
        for row in db.conn.execute("SELECT id, artist FROM tracks ORDER BY id")
    ]
    dry = service.run(dry_run=True)
    assert dry.dry_run and dry.merged_artist_count == 1
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 2

    applied = service.apply(plan)
    assert not applied.dry_run
    assert applied.merged_artist_count == 1
    rows = db.conn.execute(
        "SELECT track_id,artist_id,role,credit_order,join_phrase,provenance "
        "FROM track_artist_credits ORDER BY track_id"
    ).fetchall()
    assert {int(row["artist_id"]) for row in rows} == {canonical}
    assert rows[1]["join_phrase"] == " feat. "
    assert rows[1]["provenance"] == "synthetic_provider"
    assert [tuple(row) for row in db.conn.execute("SELECT id, artist FROM tracks ORDER BY id")] == before_display
    alias = db.conn.execute(
        "SELECT alias_name,alias_kind FROM artist_aliases WHERE artist_id=?",
        (canonical,),
    ).fetchone()
    assert tuple(alias) == ("aster-unit", "display_variant")
    artist_count = db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    resolved = ArtistCreditService(db).upsert_artist("aster-unit")
    assert resolved.id == canonical
    assert resolved.display_name == "Aster Unit"
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == artist_count
    later_track = _track(db, tmp_path, "later-variant", "aster-unit")
    assert int(
        db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?",
            (later_track,),
        ).fetchone()[0]
    ) == canonical
    assert service.run(dry_run=True).merged_artist_count == 0
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_provider_conflict_and_person_group_conflict_remain_separate(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "conflicts.sqlite3")
    tracks = [_track(db, tmp_path, f"track-{index}", "Shared Name") for index in range(4)]
    _empty_generated_graph(db)
    ids = (
        _artist(db, "Shared Name", kind="person", discogs="1001"),
        _artist(db, "shared name", kind="person", discogs="2002"),
        _artist(db, "Unit Name", kind="person"),
        _artist(db, "unit-name", kind="group"),
    )
    for track_id, artist_id in zip(tracks, ids, strict=True):
        _credit(db, track_id, artist_id)
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()
    assert plan.merges == ()
    assert {item.reason for item in plan.conflicts} == {
        "discogs_id_conflict",
        "person_group_conflict",
    }
    assert len(query_artist_summaries(db.conn)) == 4
    db.close()


@pytest.mark.parametrize(
    ("provider_key", "id_key", "provider_ids", "expected_reason"),
    (
        (
            "_discogs",
            "artist_id",
            ("4101", "4102"),
            "accepted_discogs_artist_id_conflict",
        ),
        (
            "_musicbrainz",
            "musicbrainz_artist_id",
            (
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            ),
            "accepted_musicbrainz_artist_id_conflict",
        ),
    ),
)
def test_presentation_variants_with_conflicting_applied_provider_context_stay_separate(
    tmp_path: Path,
    provider_key: str,
    id_key: str,
    provider_ids: tuple[str, str],
    expected_reason: str,
):
    db = MusicVaultDB(tmp_path / f"{provider_key}-context.sqlite3")
    first_track = _track(db, tmp_path, "context-one", "Signal.Unit")
    second_track = _track(db, tmp_path, "context-two", "signal unit")
    _empty_generated_graph(db)
    first_artist = _artist(db, "Signal.Unit")
    second_artist = _artist(db, "signal unit")
    _credit(db, first_track, first_artist)
    _credit(db, second_track, second_artist)
    _accepted_artist_proposal(
        db,
        job_id="accepted-context",
        track_id=first_track,
        provider_key=provider_key,
        artist_name="Signal.Unit",
        id_key=id_key,
        provider_artist_id=provider_ids[0],
    )
    _accepted_artist_proposal(
        db,
        job_id="accepted-context",
        track_id=second_track,
        provider_key=provider_key,
        artist_name="signal unit",
        id_key=id_key,
        provider_artist_id=provider_ids[1],
    )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert plan.merges == ()
    assert [conflict.reason for conflict in plan.conflicts] == [expected_reason]
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 2
    db.close()


def test_malformed_accepted_provider_context_fails_closed(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "malformed-context.sqlite3")
    first_track = _track(db, tmp_path, "malformed-one", "Signal.Unit")
    second_track = _track(db, tmp_path, "malformed-two", "signal unit")
    _empty_generated_graph(db)
    first_artist = _artist(db, "Signal.Unit")
    second_artist = _artist(db, "signal unit")
    _credit(db, first_track, first_artist)
    _credit(db, second_track, second_artist)
    db.conn.execute(
        """
        INSERT INTO metadata_intelligence_jobs (
            id,job_kind,status,created_at,updated_at
        ) VALUES ('malformed-context','existing_library','complete',?,?)
        """,
        (STAMP, STAMP),
    )
    db.conn.execute(
        """
        INSERT INTO metadata_intelligence_items (
            job_id,track_id,state,field_proposal,field_confidence,
            created_at,updated_at
        ) VALUES ('malformed-context',?,'applied',?,'{}',?,?)
        """,
        (
            first_track,
            json.dumps({"_discogs": {"artist_credits": "not-a-credit-list"}}),
            STAMP,
            STAMP,
        ),
    )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert plan.merges == ()
    assert [conflict.reason for conflict in plan.conflicts] == [
        "accepted_provider_context_malformed"
    ]
    db.close()


def test_complementary_provider_ids_move_to_one_canonical_artist(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "complementary-identities.sqlite3")
    first_track = _track(db, tmp_path, "complementary-one", "Aster.Unit")
    second_track = _track(db, tmp_path, "complementary-two", "aster unit")
    _empty_generated_graph(db)
    discogs_artist = _artist(db, "Aster.Unit", discogs="8101")
    musicbrainz_artist = _artist(
        db,
        "aster unit",
        musicbrainz="11111111-1111-4111-8111-111111111111",
    )
    _credit(db, first_track, discogs_artist)
    _credit(db, second_track, musicbrainz_artist)
    db.conn.commit()

    report = ArtistConsolidationService(db).run(dry_run=False)

    assert report.merged_artist_count == 1
    assert report.deleted_artist_count == 1
    canonical = db.conn.execute(
        """
        SELECT discogs_artist_id,musicbrainz_artist_id FROM artists
        WHERE id IN (
            SELECT artist_id FROM track_artist_credits
            WHERE track_id IN (?,?)
        )
        """,
        (first_track, second_track),
    ).fetchone()
    assert tuple(canonical) == (
        "8101",
        "11111111-1111-4111-8111-111111111111",
    )
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT artist_id) FROM track_artist_credits"
    ).fetchone()[0] == 1
    db.close()


def test_identical_legacy_credit_collision_deduplicates_during_merge():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE artists (
            id INTEGER PRIMARY KEY, display_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL, sort_name TEXT NOT NULL,
            entity_type TEXT NOT NULL, discogs_artist_id TEXT,
            musicbrainz_artist_id TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE tracks (id INTEGER PRIMARY KEY);
        CREATE TABLE track_artist_credits (
            id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL,
            artist_id INTEGER NOT NULL, role TEXT NOT NULL,
            credit_order INTEGER NOT NULL, join_phrase TEXT NOT NULL,
            provenance TEXT NOT NULL, provider_reference TEXT,
            confidence REAL, is_manual INTEGER NOT NULL,
            is_locked INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE artist_aliases (
            id INTEGER PRIMARY KEY, artist_id INTEGER NOT NULL,
            alias_name TEXT NOT NULL, normalized_alias TEXT NOT NULL,
            alias_kind TEXT NOT NULL, provenance TEXT NOT NULL,
            provider_reference TEXT, confidence REAL, created_at TEXT NOT NULL,
            UNIQUE(artist_id,normalized_alias,alias_kind)
        );
        CREATE TABLE artist_relationships (
            id INTEGER PRIMARY KEY, subject_artist_id INTEGER NOT NULL,
            related_artist_id INTEGER NOT NULL, relationship_kind TEXT NOT NULL,
            provenance TEXT NOT NULL, provider_reference TEXT,
            confidence REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO artists VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            (1, "Aster.Unit", "aster.unit", "aster.unit", "person", None, None, STAMP, STAMP),
            (2, "aster unit", "aster unit", "aster unit", "person", None, None, STAMP, STAMP),
        ),
    )
    conn.execute("INSERT INTO tracks VALUES (1)")
    conn.executemany(
        """
        INSERT INTO track_artist_credits VALUES (
            ?,1,?,'primary',0,'','synthetic',NULL,99,0,0,?,?
        )
        """,
        ((1, 1, STAMP, STAMP), (2, 2, STAMP, STAMP)),
    )
    conn.commit()

    report = ArtistConsolidationService(conn).run(dry_run=False)

    assert report.merged_artist_count == 1
    assert report.deleted_credit_count == 1
    assert conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM track_artist_credits").fetchone()[0] == 1
    assert conn.execute(
        "SELECT artist_id FROM track_artist_credits"
    ).fetchone()[0] == 1
    conn.close()


def test_exact_same_name_with_provider_backed_and_legacy_rows_consolidates(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "same-name.sqlite3")
    first_track = _track(db, tmp_path, "same-name-one", "Shared Synthetic Name")
    second_track = _track(db, tmp_path, "same-name-two", "Shared Synthetic Name")
    _empty_generated_graph(db)
    first = _artist(db, "Shared Synthetic Name", discogs="1001")
    second = _artist(db, "Shared Synthetic Name")
    _credit(db, first_track, first)
    _credit(db, second_track, second)
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert len(plan.merges) == 1
    assert plan.merges[0].canonical_artist_id == first
    assert plan.merges[0].duplicate_artist_ids == (second,)
    assert plan.conflicts == ()
    report = ArtistConsolidationService(db).apply(plan)
    assert report.merged_artist_count == 1
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 1
    db.close()


def test_featured_collaboration_and_verified_group_appearances_are_distinct_sections(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "sections.sqlite3")
    solo_track = _track(db, tmp_path, "solo", "Solo Unit")
    featured_track = _track(db, tmp_path, "featured", "Lead Unit feat. Solo Unit")
    collaboration_track = _track(db, tmp_path, "collab", "Lead Unit x Solo Unit")
    group_track = _track(db, tmp_path, "group", "Verified Group")
    invalid_track = _track(db, tmp_path, "label", "Synthetic Records")
    context_track = _track(db, tmp_path, "context", "Various Artists")
    _empty_generated_graph(db)
    solo = _artist(db, "Solo Unit", kind="person")
    lead = _artist(db, "Lead Unit", kind="person")
    group = _artist(db, "Verified Group", kind="group")
    label = _artist(db, "Synthetic Records")
    various = _artist(db, "Various Artists")
    _credit(db, solo_track, solo)
    _credit(db, featured_track, lead, order=0, join=" feat. ")
    _credit(db, featured_track, solo, "featured", 1)
    _credit(db, collaboration_track, lead, order=0, join=" x ")
    _credit(db, collaboration_track, solo, "collaborator", 1)
    _credit(db, group_track, group)
    _credit(db, invalid_track, label, provenance="youtube_uploader_fallback")
    _credit(db, context_track, various)
    db.conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,provenance,
            confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','synthetic_provider',100,?,?)
        """,
        (solo, group, STAMP, STAMP),
    )
    db.conn.commit()

    summaries = {item.key.normalized_name: item for item in query_artist_summaries(db.conn)}
    assert set(summaries) == {"solo unit", "lead unit", "verified group"}
    summary = summaries["solo unit"]
    assert (summary.track_count, summary.featured_track_count) == (1, 1)
    assert (summary.collaboration_track_count, summary.group_appearance_track_count) == (1, 1)
    sections = query_artist_track_sections(db.conn, ArtistKey("solo unit"))
    assert [row["id"] for row in sections.tracks] == [solo_track]
    assert [row["id"] for row in sections.featured_on] == [featured_track]
    assert [row["id"] for row in sections.collaborations] == [collaboration_track]
    assert [row["id"] for row in sections.group_appearances] == [group_track]
    assert group_track not in {row["id"] for row in sections.tracks}
    db.close()


def test_artist_merge_preserves_authoritative_manual_relationship_evidence(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "relationship-merge.sqlite3")
    first_track = _track(db, tmp_path, "relation-one", "Aster.Unit")
    second_track = _track(db, tmp_path, "relation-two", "aster unit")
    _empty_generated_graph(db)
    canonical = _artist(db, "Aster.Unit", kind="person", discogs="7001")
    duplicate = _artist(db, "aster unit", kind="person")
    group = _artist(db, "Verified Ensemble", kind="group")
    _credit(db, first_track, canonical)
    _credit(db, second_track, duplicate)
    db.conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,provenance,
            provider_reference,confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','discogs','discogs:relationship',95,?,?)
        """,
        (canonical, group, STAMP, STAMP),
    )
    db.conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,provenance,
            provider_reference,confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','manual','manual:confirmed',100,?,?)
        """,
        (duplicate, group, STAMP, STAMP),
    )
    db.conn.commit()

    report = ArtistConsolidationService(db).run(dry_run=False)

    assert report.merged_artist_count == 1
    relation = db.conn.execute(
        """
        SELECT subject_artist_id,related_artist_id,provenance,
               provider_reference,confidence
        FROM artist_relationships
        """
    ).fetchone()
    assert tuple(relation) == (
        canonical,
        group,
        "manual",
        "manual:confirmed",
        100.0,
    )
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artist_relationships"
    ).fetchone()[0] == 1
    db.close()


def test_artist_merge_fails_closed_for_incompatible_relationship_audits(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "relationship-conflict.sqlite3")
    first_track = _track(db, tmp_path, "relation-conflict-one", "Aster.Unit")
    second_track = _track(db, tmp_path, "relation-conflict-two", "aster unit")
    _empty_generated_graph(db)
    first = _artist(db, "Aster.Unit", kind="person")
    second = _artist(db, "aster unit", kind="person")
    group = _artist(db, "Verified Ensemble", kind="group")
    _credit(db, first_track, first)
    _credit(db, second_track, second)
    for artist_id, provenance, reference in (
        (first, "discogs", "discogs:first"),
        (second, "musicbrainz", "musicbrainz:second"),
    ):
        db.conn.execute(
            """
            INSERT INTO artist_relationships (
                subject_artist_id,related_artist_id,relationship_kind,
                provenance,provider_reference,confidence,created_at,updated_at
            ) VALUES (?,?,'member_of',?,?,95,?,?)
            """,
            (artist_id, group, provenance, reference, STAMP, STAMP),
        )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert plan.merges == ()
    assert plan.conflicts[0].reason == "relationship_evidence_conflict"
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 3
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artist_relationships"
    ).fetchone()[0] == 2
    db.close()


def test_version_suffix_repair_uses_existing_version_evidence_and_records_history(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "version.sqlite3")
    track_id = _track(db, tmp_path, "live", "Aster Unit Live at Synthetic Hall")
    _empty_generated_graph(db)
    canonical = _artist(db, "Aster Unit", kind="person", discogs="9191")
    malformed = _artist(db, "Aster Unit Live at Synthetic Hall", kind="person")
    _credit(db, track_id, malformed)
    db.conn.execute(
        """
        INSERT INTO artist_aliases (
            artist_id,alias_name,normalized_alias,alias_kind,provenance,
            confidence,created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            malformed,
            "Aster Unit at Synthetic Hall",
            "aster unit at synthetic hall",
            "source_title_variant",
            "synthetic_legacy",
            95,
            STAMP,
        ),
    )
    for field_name, value in (
        ("artist", "Aster Unit Live at Synthetic Hall"),
        ("version_type", "live"),
        ("version_label", "Live at Synthetic Hall"),
    ):
        db.conn.execute(
            """
            UPDATE track_metadata_fields SET value=?,provenance='provider_confirmed',
                confidence=100,is_manual=0,is_locked=0
            WHERE track_id=? AND field_name=?
            """,
            (value, track_id, field_name),
        )
    db.conn.commit()

    service = ArtistConsolidationService(db)
    db.conn.execute(
        "UPDATE track_artist_credits SET is_manual=1 WHERE track_id=?", (track_id,)
    )
    assert service.plan().version_repairs == ()
    db.conn.execute(
        "UPDATE track_artist_credits SET is_manual=0 WHERE track_id=?", (track_id,)
    )
    plan = service.plan()
    assert len(plan.version_repairs) == 1
    assert plan.version_repairs[0].canonical_artist_id == canonical
    service.apply(plan)
    track = db.get_track(track_id)
    assert track["artist"] == "Aster Unit"
    assert track["version_type"] == "live"
    assert track["version_label"] == "Live at Synthetic Hall"
    credit = db.conn.execute(
        "SELECT artist_id FROM track_artist_credits WHERE track_id=?", (track_id,)
    ).fetchone()
    assert int(credit[0]) == canonical
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=?", (track_id,)
    ).fetchone()[0] >= 1
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    aliases = {
        tuple(row)
        for row in db.conn.execute(
            "SELECT alias_name,alias_kind FROM artist_aliases WHERE artist_id=?",
            (canonical,),
        )
    }
    assert aliases == {
        ("Aster Unit Live at Synthetic Hall", "corrected_version_suffix"),
        ("Aster Unit at Synthetic Hall", "source_title_variant"),
    }
    artist_count = db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    resolved = ArtistCreditService(db).upsert_artist(
        "Aster Unit Live at Synthetic Hall"
    )
    assert resolved.id == canonical
    assert resolved.display_name == "Aster Unit"
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == artist_count
    provider_resolved = ArtistCreditService(db).upsert_artist(
        "Aster Unit Live at Synthetic Hall", discogs_artist_id="9191"
    )
    assert provider_resolved.id == canonical
    assert provider_resolved.display_name == "Aster Unit"
    followup_track = _track(
        db, tmp_path, "live-followup", "Aster Unit Live at Synthetic Hall"
    )
    assert int(
        db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?",
            (followup_track,),
        ).fetchone()[0]
    ) == canonical
    db.close()


def test_tiny_desk_suffix_uses_shared_live_taxonomy_for_repair(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "tiny-desk.sqlite3")
    track_id = _track(db, tmp_path, "tiny-desk", "Desk Unit Tiny Desk Concert")
    _empty_generated_graph(db)
    canonical = _artist(db, "Desk Unit", kind="person")
    malformed = _artist(db, "Desk Unit Tiny Desk Concert", kind="person")
    _credit(db, track_id, malformed)
    for field_name, value in (
        ("version_type", "live"),
        ("version_label", "Tiny Desk Concert"),
    ):
        db.conn.execute(
            """
            UPDATE track_metadata_fields SET value=?,provenance='provider_confirmed',
                confidence=100,is_manual=0,is_locked=0
            WHERE track_id=? AND field_name=?
            """,
            (value, track_id, field_name),
        )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert len(plan.version_repairs) == 1
    assert plan.version_repairs[0].canonical_artist_id == canonical
    assert plan.version_repairs[0].version_type == "live"
    db.close()


def test_full_credit_repair_requires_saved_structured_provider_evidence(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "credits.sqlite3")
    track_id = _track(db, tmp_path, "credit", "Lead Unit feat. Guest Unit")
    original_path = db.get_track(track_id)["path"]
    original_artist = int(
        db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?", (track_id,)
        ).fetchone()[0]
    )
    db.conn.execute(
        """
        INSERT INTO metadata_intelligence_jobs (
            id,job_kind,status,created_at,updated_at
        ) VALUES ('synthetic-credit','existing_library','complete',?,?)
        """,
        (STAMP, STAMP),
    )
    proposal = {
        "_discogs": {
            "artist": "Lead Unit feat. Guest Unit",
            "provider_reference": "https://www.discogs.com/release/123",
            "artist_credits": [
                {
                    "name": "Lead Unit",
                    "role": "primary",
                    "artist_id": "111",
                },
                {
                    "name": "Guest Unit",
                    "role": "featured",
                    "artist_id": "222",
                    "join_phrase": " feat. ",
                },
            ],
        }
    }
    db.conn.execute(
        """
        INSERT INTO metadata_intelligence_items (
            job_id,track_id,state,field_proposal,field_confidence,
            created_at,updated_at
        ) VALUES ('synthetic-credit',?,'applied',?,?,?,?)
        """,
        (
            track_id,
            json.dumps(proposal),
            json.dumps({"artist_credits": 99}),
            STAMP,
            STAMP,
        ),
    )
    db.conn.commit()

    service = ArtistConsolidationService(db)
    db.conn.execute(
        "UPDATE track_artist_credits SET is_locked=1 WHERE track_id=?", (track_id,)
    )
    assert service.plan().full_credit_repairs == ()
    db.conn.execute(
        "UPDATE track_artist_credits SET is_locked=0 WHERE track_id=?", (track_id,)
    )
    db.conn.execute(
        "UPDATE metadata_intelligence_items SET state='review' WHERE track_id=?",
        (track_id,),
    )
    assert service.plan().full_credit_repairs == ()
    db.conn.execute(
        "UPDATE metadata_intelligence_items SET state='applied' WHERE track_id=?",
        (track_id,),
    )
    plan = service.plan()
    assert len(plan.full_credit_repairs) == 1
    service.apply(plan)
    credits = db.conn.execute(
        """
        SELECT a.display_name,c.role,c.join_phrase
        FROM track_artist_credits c JOIN artists a ON a.id=c.artist_id
        WHERE c.track_id=? ORDER BY c.credit_order
        """,
        (track_id,),
    ).fetchall()
    assert [tuple(row) for row in credits] == [
        ("Lead Unit", "primary", ""),
        ("Guest Unit", "featured", " feat. "),
    ]
    assert db.get_track(track_id)["artist"] == "Lead Unit feat. Guest Unit"
    assert db.get_track(track_id)["path"] == original_path
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artists WHERE id=?", (original_artist,)
    ).fetchone()[0] == 0
    assert tuple(
        db.conn.execute(
            """
            SELECT alias.alias_name,alias.alias_kind,alias.provider_reference
            FROM artist_aliases alias
            JOIN artists artist ON artist.id=alias.artist_id
            WHERE artist.display_name='Lead Unit'
            """
        ).fetchone()
    ) == (
        "Lead Unit feat. Guest Unit",
        "legacy_credit_string",
        "https://www.discogs.com/release/123",
    )
    artist_count = db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    resolved = ArtistCreditService(db).upsert_artist(
        "Lead Unit feat. Guest Unit"
    )
    assert resolved.display_name == "Lead Unit"
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == artist_count
    db.close()


def test_version_repair_preserves_nonconflicting_provider_identity_and_blocks_conflict(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "version-provider.sqlite3")
    track_id = _track(
        db, tmp_path, "version-provider", "Canonical Unit Live at Synthetic Hall"
    )
    _empty_generated_graph(db)
    canonical = _artist(db, "Canonical Unit", kind="person")
    malformed = _artist(
        db,
        "Canonical Unit Live at Synthetic Hall",
        kind="person",
        discogs="9191",
    )
    _credit(db, track_id, malformed)
    for field_name, value in (
        ("version_type", "live"),
        ("version_label", "Live at Synthetic Hall"),
    ):
        db.conn.execute(
            """
            UPDATE track_metadata_fields SET value=?,provenance='provider_confirmed',
                confidence=100,is_manual=0,is_locked=0
            WHERE track_id=? AND field_name=?
            """,
            (value, track_id, field_name),
        )
    db.conn.commit()

    service = ArtistConsolidationService(db)
    plan = service.plan()
    assert len(plan.version_repairs) == 1
    service.apply(plan)
    assert db.conn.execute(
        "SELECT discogs_artist_id FROM artists WHERE id=?", (canonical,)
    ).fetchone()[0] == "9191"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artists WHERE id=?", (malformed,)
    ).fetchone()[0] == 0

    conflict_track = _track(
        db, tmp_path, "version-conflict", "Other Unit Live at Synthetic Hall"
    )
    other_canonical = _artist(db, "Other Unit", kind="person", discogs="9292")
    other_malformed = _artist(
        db,
        "Other Unit Live at Synthetic Hall",
        kind="person",
        discogs="9393",
    )
    db.conn.execute(
        "DELETE FROM track_artist_credits WHERE track_id=?", (conflict_track,)
    )
    _credit(db, conflict_track, other_malformed)
    for field_name, value in (
        ("version_type", "live"),
        ("version_label", "Live at Synthetic Hall"),
    ):
        db.conn.execute(
            """
            UPDATE track_metadata_fields SET value=?,provenance='provider_confirmed',
                confidence=100,is_manual=0,is_locked=0
            WHERE track_id=? AND field_name=?
            """,
            (value, conflict_track, field_name),
        )
    db.conn.commit()
    assert all(
        repair.track_id != conflict_track
        for repair in ArtistConsolidationService(db).plan().version_repairs
    )
    assert db.conn.execute(
        "SELECT discogs_artist_id FROM artists WHERE id=?", (other_canonical,)
    ).fetchone()[0] == "9292"
    db.close()


def test_version_repair_keeps_still_referenced_identity_and_blocks_entity_type_change(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "version-shared.sqlite3")
    repaired_track = _track(
        db, tmp_path, "version-shared-repair", "Shared Unit Live at Synthetic Hall"
    )
    retained_track = _track(
        db, tmp_path, "version-shared-retain", "Shared Unit Live at Synthetic Hall"
    )
    _empty_generated_graph(db)
    canonical = _artist(db, "Shared Unit", kind="person")
    malformed = _artist(
        db, "Shared Unit Live at Synthetic Hall", kind="person"
    )
    _credit(db, repaired_track, malformed)
    _credit(db, retained_track, malformed)
    for field_name, value in (
        ("version_type", "live"),
        ("version_label", "Live at Synthetic Hall"),
    ):
        db.conn.execute(
            """
            UPDATE track_metadata_fields SET value=?,provenance='provider_confirmed',
                confidence=100,is_manual=0,is_locked=0
            WHERE track_id=? AND field_name=?
            """,
            (value, repaired_track, field_name),
        )
    db.conn.commit()

    service = ArtistConsolidationService(db)
    report = service.apply(service.plan())
    assert report.version_repairs == 1
    assert report.deleted_artist_count == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artists WHERE id=?", (malformed,)
    ).fetchone()[0] == 1
    assert int(
        db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?",
            (repaired_track,),
        ).fetchone()[0]
    ) == canonical
    assert int(
        db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?",
            (retained_track,),
        ).fetchone()[0]
    ) == malformed

    typed_track = _track(
        db, tmp_path, "version-typed", "Typed Unit Live at Synthetic Hall"
    )
    typed_canonical = _artist(db, "Typed Unit", kind="person")
    typed_malformed = _artist(
        db, "Typed Unit Live at Synthetic Hall", kind="group"
    )
    db.conn.execute("DELETE FROM track_artist_credits WHERE track_id=?", (typed_track,))
    _credit(db, typed_track, typed_malformed)
    for field_name, value in (
        ("version_type", "live"),
        ("version_label", "Live at Synthetic Hall"),
    ):
        db.conn.execute(
            """
            UPDATE track_metadata_fields SET value=?,provenance='provider_confirmed',
                confidence=100,is_manual=0,is_locked=0
            WHERE track_id=? AND field_name=?
            """,
            (value, typed_track, field_name),
        )
    db.conn.commit()
    assert all(
        repair.track_id != typed_track
        for repair in ArtistConsolidationService(db).plan().version_repairs
    )
    assert db.conn.execute(
        "SELECT entity_type FROM artists WHERE id=?", (typed_canonical,)
    ).fetchone()[0] == "person"
    db.close()


def test_internal_member_of_relationship_blocks_merge_without_changing_evidence(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "internal-relationship.sqlite3")
    first_track = _track(db, tmp_path, "internal-first", "Shared Relation")
    second_track = _track(db, tmp_path, "internal-second", "shared relation")
    _empty_generated_graph(db)
    first = _artist(db, "Shared Relation")
    second = _artist(db, "shared relation")
    _credit(db, first_track, first)
    _credit(db, second_track, second)
    db.conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,
            provenance,provider_reference,confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','manual','manual:internal',100,?,?)
        """,
        (first, second, STAMP, STAMP),
    )
    db.conn.commit()
    before = tuple(
        db.conn.execute(
            """
            SELECT subject_artist_id,related_artist_id,relationship_kind,
                   provenance,provider_reference,confidence,created_at,updated_at
            FROM artist_relationships
            """
        ).fetchone()
    )

    service = ArtistConsolidationService(db)
    plan = service.plan()
    report = service.apply(plan)

    assert plan.merges == ()
    assert {conflict.reason for conflict in plan.conflicts} == {
        "relationship_evidence_conflict"
    }
    assert report.merged_artist_count == 0
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 2
    after = tuple(
        db.conn.execute(
            """
            SELECT subject_artist_id,related_artist_id,relationship_kind,
                   provenance,provider_reference,confidence,created_at,updated_at
            FROM artist_relationships
            """
        ).fetchone()
    )
    assert after == before
    db.close()
