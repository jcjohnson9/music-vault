from __future__ import annotations

from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.library_browser import (
    ArtistKey,
    query_artist_summaries,
    query_artist_track_sections,
)
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.artist_relationships import (
    ArtistRelationshipEvidenceError,
    ArtistRelationshipService,
)
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore


def _artist_page_key(artist_id: int, name: str) -> ArtistKey:
    return ArtistKey(
        " ".join(name.casefold().split()),
        artist_id=artist_id,
        identity_key=f"artist:{artist_id}",
    )


def _provider_group_scenario(
    tmp_path: Path,
    *,
    database_name: str,
    member_discogs_id: str,
    group_discogs_id: str,
) -> tuple[MusicVaultDB, int, int, int]:
    db = MusicVaultDB(tmp_path / database_name)
    credits = ArtistCreditService(db)
    member = credits.upsert_artist(
        "Synthetic Member",
        entity_type="person",
        discogs_artist_id=member_discogs_id,
    )
    group = credits.upsert_artist(
        "Synthetic Ensemble",
        entity_type="group",
        discogs_artist_id=group_discogs_id,
    )
    track_id = db.upsert_track(
        tmp_path / f"{database_name}.synthetic-audio",
        title="Synthetic Group Recording",
        artist="Synthetic Ensemble",
        album="Synthetic Collection",
    )
    credits.replace_track_credits(
        track_id,
        (
            ArtistCreditInput(
                "Synthetic Ensemble",
                entity_type="group",
                discogs_artist_id=group_discogs_id,
            ),
        ),
        provenance="discogs",
        provider_reference="discogs:synthetic-release",
        confidence=99,
    )
    return db, member.id, group.id, track_id


def _saved_relationship_item(
    db: MusicVaultDB,
    track_id: int,
    relationship: dict[str, object],
) -> int:
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job((track_id,))
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "applied",
        field_proposal={
            "_discogs": {
                "provider_reference": "discogs:synthetic-membership-evidence",
                "artist_relationships": [relationship],
            }
        },
        field_confidence={"artist_relationships": 98},
        provider_agreement="discogs_only",
    )
    return item.id


def test_provider_id_writer_populates_group_appearances_through_browser_api(
    tmp_path: Path,
):
    db, member_id, _group_id, group_track = _provider_group_scenario(
        tmp_path,
        database_name="provider.sqlite3",
        member_discogs_id="member-101",
        group_discogs_id="group-202",
    )

    relationship = ArtistRelationshipService(db).record_provider_member_of(
        provider="discogs",
        member_provider_id="member-101",
        group_provider_id="group-202",
        provider_reference="discogs:synthetic-membership",
        confidence=99,
    )

    assert relationship.provenance == "discogs"
    sections = query_artist_track_sections(
        db.conn, _artist_page_key(member_id, "Synthetic Member")
    )
    assert sections.tracks == ()
    assert [int(row["id"]) for row in sections.group_appearances] == [group_track]
    summaries = {summary.key.artist_id: summary for summary in query_artist_summaries(db.conn)}
    assert summaries[member_id].group_appearance_track_count == 1
    db.close()


def test_accepted_saved_provider_evidence_populates_group_appearances(
    tmp_path: Path,
):
    db, member_id, _group_id, group_track = _provider_group_scenario(
        tmp_path,
        database_name="saved.sqlite3",
        member_discogs_id="member-303",
        group_discogs_id="group-404",
    )
    item_id = _saved_relationship_item(
        db,
        group_track,
        {
            "relationship_kind": "member_of",
            "member": {"discogs_artist_id": "member-303"},
            "group": {"discogs_artist_id": "group-404"},
            "confidence": 98,
        },
    )

    stored = ArtistRelationshipService(db).record_member_of_from_saved_evidence(
        item_id
    )

    assert len(stored) == 1
    sections = query_artist_track_sections(
        db.conn, _artist_page_key(member_id, "Synthetic Member")
    )
    assert [int(row["id"]) for row in sections.group_appearances] == [group_track]
    assert group_track not in {int(row["id"]) for row in sections.tracks}
    db.close()


def test_schema7_migration_imports_relationship_after_review_becomes_applied_with_gaps(
    tmp_path: Path,
):
    path = tmp_path / "schema6-review-relationship.sqlite3"
    db, member_id, group_id, group_track = _provider_group_scenario(
        tmp_path,
        database_name=path.name,
        member_discogs_id="review-member-305",
        group_discogs_id="review-group-406",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job((group_track,))
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        field_proposal={
            "_current": {
                "title": "Synthetic Group Recording",
                "artist": "Synthetic Ensemble",
            },
            "_discogs": {
                "title": "Synthetic Group Recording",
                "score": 98,
                "provider_reference": "discogs:review-membership-evidence",
                "artist_relationships": [
                    {
                        "relationship_kind": "member_of",
                        "member": {"discogs_artist_id": "review-member-305"},
                        "group": {"discogs_artist_id": "review-group-406"},
                        "confidence": 98,
                    }
                ],
            },
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
            "_reasons": {},
        },
        field_confidence={"artist_relationships": 98},
        provider_agreement="discogs_only",
        review_reason="album_ambiguity",
    )

    for table in (
        "track_album_memberships",
        "canonical_albums",
        "artist_relationships",
        "artist_aliases",
    ):
        db.conn.execute(f"DROP TABLE {table}")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    db.close()

    migrated = MusicVaultDB(path, backup_dir=tmp_path / "review-migration-backups")

    assert migrated.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == "applied_with_gaps"
    relationship = migrated.conn.execute(
        """
        SELECT subject_artist_id,related_artist_id,relationship_kind,provenance
        FROM artist_relationships
        """
    ).fetchone()
    assert tuple(relationship) == (member_id, group_id, "member_of", "discogs")
    migrated.close()


def test_manual_confirmation_uses_explicit_artist_ids_and_reaches_browser(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "manual.sqlite3")
    credits = ArtistCreditService(db)
    member = credits.upsert_artist("Manual Member", entity_type="person")
    group = credits.upsert_artist("Manual Ensemble", entity_type="group")
    track_id = db.upsert_track(
        tmp_path / "manual.synthetic-audio",
        title="Manual Group Recording",
        artist="Manual Ensemble",
    )
    credits.replace_track_credits(
        track_id,
        (ArtistCreditInput("Manual Ensemble", entity_type="group"),),
        provenance="manual",
        provider_reference="manual:synthetic-credit",
        confidence=100,
        is_manual=True,
    )

    ArtistRelationshipService(db).record_manual_member_of(
        member_artist_id=member.id,
        group_artist_id=group.id,
        confirmation_reference="manual:synthetic-confirmation",
    )

    sections = query_artist_track_sections(
        db.conn, _artist_page_key(member.id, "Manual Member")
    )
    assert [int(row["id"]) for row in sections.group_appearances] == [track_id]
    db.close()


@pytest.mark.parametrize(
    "relationship",
    [
        {
            "relationship_kind": "member_of",
            "member": {"name": "Synthetic Member"},
            "group": {"name": "Synthetic Ensemble"},
            "confidence": 99,
        },
        {
            "relationship_kind": "member_of",
            "member": {
                "discogs_artist_id": "member-505",
                "musicbrainz_artist_id": "conflicting-mb-identity",
            },
            "group": {"discogs_artist_id": "group-606"},
            "confidence": 99,
        },
    ],
    ids=("name_only", "conflicting_provider_identities"),
)
def test_saved_relationship_evidence_fails_closed_without_stable_identity(
    tmp_path: Path,
    relationship: dict[str, object],
):
    db, member_id, _group_id, group_track = _provider_group_scenario(
        tmp_path,
        database_name="rejected.sqlite3",
        member_discogs_id="member-505",
        group_discogs_id="group-606",
    )
    ArtistCreditService(db).upsert_artist(
        "Conflicting Identity",
        entity_type="person",
        musicbrainz_artist_id="conflicting-mb-identity",
    )
    item_id = _saved_relationship_item(db, group_track, relationship)

    with pytest.raises(ArtistRelationshipEvidenceError):
        ArtistRelationshipService(db).record_member_of_from_saved_evidence(item_id)

    sections = query_artist_track_sections(
        db.conn, _artist_page_key(member_id, "Synthetic Member")
    )
    assert sections.group_appearances == ()
    db.close()


def test_schema6_to_7_imports_only_accepted_unambiguous_member_of_evidence(
    tmp_path: Path,
):
    path = tmp_path / "schema6-relationships.sqlite3"
    db, valid_member_id, _valid_group_id, valid_group_track = (
        _provider_group_scenario(
            tmp_path,
            database_name=path.name,
            member_discogs_id="migration-member-701",
            group_discogs_id="migration-group-702",
        )
    )
    valid_item_id = _saved_relationship_item(
        db,
        valid_group_track,
        {
            "relationship_kind": "member_of",
            "member": {"discogs_artist_id": "migration-member-701"},
            "group": {"discogs_artist_id": "migration-group-702"},
            "confidence": 99,
        },
    )

    credits = ArtistCreditService(db)
    rejected_member = credits.upsert_artist(
        "Rejected Member",
        entity_type="person",
        discogs_artist_id="migration-member-801",
    )
    rejected_group = credits.upsert_artist(
        "Rejected Ensemble",
        entity_type="group",
        discogs_artist_id="migration-group-802",
    )
    credits.upsert_artist(
        "Conflicting Provider Identity",
        entity_type="person",
        musicbrainz_artist_id="migration-conflict-mb",
    )
    rejected_group_track = db.upsert_track(
        tmp_path / "rejected-migration.synthetic-audio",
        title="Rejected Group Recording",
        artist="Rejected Ensemble",
    )
    credits.replace_track_credits(
        rejected_group_track,
        (
            ArtistCreditInput(
                "Rejected Ensemble",
                entity_type="group",
                discogs_artist_id="migration-group-802",
            ),
        ),
        provenance="discogs",
        provider_reference="discogs:rejected-synthetic-release",
        confidence=99,
    )
    rejected_item_id = _saved_relationship_item(
        db,
        rejected_group_track,
        {
            "relationship_kind": "member_of",
            "member": {
                "discogs_artist_id": "migration-member-801",
                "musicbrainz_artist_id": "migration-conflict-mb",
            },
            "group": {"discogs_artist_id": "migration-group-802"},
            "confidence": 99,
        },
    )
    assert valid_item_id != rejected_item_id
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artist_relationships"
    ).fetchone()[0] == 0

    # Recreate the exact schema-6 boundary: accepted normalized evidence is
    # retained, while only the additive schema-7 structures are absent.
    db.conn.execute("DROP TABLE track_album_memberships")
    db.conn.execute("DROP TABLE canonical_albums")
    db.conn.execute("DROP TABLE artist_relationships")
    db.conn.execute("DROP TABLE artist_aliases")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    db.close()

    migrated = MusicVaultDB(path, backup_dir=tmp_path / "migration-backups")

    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    relationships = migrated.conn.execute(
        """
        SELECT subject_artist_id, related_artist_id, provenance
        FROM artist_relationships ORDER BY id
        """
    ).fetchall()
    assert len(relationships) == 1
    assert int(relationships[0]["subject_artist_id"]) == valid_member_id
    assert relationships[0]["provenance"] == "discogs"
    assert migrated.conn.execute(
        """
        SELECT COUNT(*) FROM artist_relationships
        WHERE subject_artist_id=? AND related_artist_id=?
        """,
        (rejected_member.id, rejected_group.id),
    ).fetchone()[0] == 0
    sections = query_artist_track_sections(
        migrated.conn,
        _artist_page_key(valid_member_id, "Synthetic Member"),
    )
    assert [int(row["id"]) for row in sections.group_appearances] == [
        valid_group_track
    ]
    assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated.close()
