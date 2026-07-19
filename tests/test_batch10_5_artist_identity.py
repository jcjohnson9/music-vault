from __future__ import annotations

from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.library_browser import (
    ArtistKey,
    query_album_summaries,
    query_album_tracks,
    query_artist_summaries,
    query_artist_track_sections,
    resolve_artist_cluster_ids,
)
from music_vault.metadata.artist_consolidation import ArtistConsolidationService


STAMP = "2026-07-18T00:00:00Z"
MBID = "11111111-1111-4111-8111-111111111111"


def _track(
    db: MusicVaultDB,
    root: Path,
    name: str,
    artist: str,
    *,
    album: str | None = "Synthetic Album",
) -> int:
    return db.upsert_track(
        root / f"{name}.synthetic-audio",
        title=name,
        artist=artist,
        album=album,
    )


def _artist(
    db: MusicVaultDB,
    name: str,
    *,
    discogs: str | None = None,
    musicbrainz: str | None = None,
    entity_type: str = "person",
) -> int:
    return int(
        db.conn.execute(
            """
            INSERT INTO artists (
                display_name,normalized_name,sort_name,entity_type,
                discogs_artist_id,musicbrainz_artist_id,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                name,
                " ".join(name.casefold().split()),
                name,
                entity_type,
                discogs,
                musicbrainz,
                STAMP,
                STAMP,
            ),
        ).lastrowid
    )


def _credit(
    db: MusicVaultDB,
    track_id: int,
    artist_id: int,
    *,
    role: str = "primary",
    order: int = 0,
    provenance: str = "discogs_high_confidence",
) -> None:
    db.conn.execute(
        """
        INSERT INTO track_artist_credits (
            track_id,artist_id,role,credit_order,join_phrase,provenance,
            confidence,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,99,?,?)
        """,
        (track_id, artist_id, role, order, "", provenance, STAMP, STAMP),
    )


def _clear_seeded_graph(db: MusicVaultDB) -> None:
    db.conn.execute("DELETE FROM track_artist_credits")
    db.conn.execute("DELETE FROM artists")


def test_browser_clusters_complementary_provider_rows_and_unions_detail_roles(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "cluster.sqlite3")
    primary_track = _track(db, tmp_path, "primary", "Synthetic Artist")
    featured_track = _track(db, tmp_path, "featured", "Lead feat. Synthetic Artist")
    collaboration_track = _track(db, tmp_path, "collaboration", "Lead x Synthetic Artist")
    group_track = _track(db, tmp_path, "group", "Synthetic Ensemble")
    _clear_seeded_graph(db)
    discogs = _artist(db, "Synthetic Artist", discogs="4101")
    musicbrainz = _artist(db, "Synthetic Artist", musicbrainz=MBID)
    db.conn.execute(
        """
        INSERT INTO artist_aliases (
            artist_id,alias_name,normalized_alias,alias_kind,provenance,
            confidence,created_at
        ) VALUES (?,'Synthetic Alias','synthetic alias','display_variant',
                  'canonical_consolidation',99,?)
        """,
        (discogs, STAMP),
    )
    lead = _artist(db, "Lead Artist", discogs="4102")
    group = _artist(db, "Synthetic Ensemble", discogs="4103", entity_type="group")
    _credit(db, primary_track, discogs)
    _credit(db, featured_track, lead)
    _credit(db, featured_track, musicbrainz, role="featured", order=1)
    _credit(db, collaboration_track, lead)
    _credit(db, collaboration_track, discogs, role="collaborator", order=1)
    # A duplicate compatible role row must not duplicate the detail track.
    _credit(db, collaboration_track, musicbrainz, role="collaborator", order=2)
    _credit(db, group_track, group)
    db.conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,provenance,
            confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','discogs',99,?,?)
        """,
        (musicbrainz, group, STAMP, STAMP),
    )
    db.conn.commit()

    matches = [
        summary
        for summary in query_artist_summaries(db.conn)
        if summary.key.normalized_name == "synthetic artist"
    ]
    assert len(matches) == 1
    summary = matches[0]
    assert set(summary.key.cluster_artist_ids) == {discogs, musicbrainz}
    assert summary.track_count == 1
    assert summary.featured_track_count == 1
    assert summary.collaboration_track_count == 1
    assert summary.group_appearance_track_count == 1
    assert summary.canonical_artist_id in {discogs, musicbrainz}
    assert summary.discogs_artist_id == "4101"
    assert summary.musicbrainz_artist_id == MBID
    assert summary.historical_aliases == ("synthetic alias",)
    from music_vault.app import MusicVaultWindow

    image_identity = MusicVaultWindow.artist_image_identity(summary)
    assert "discogs:4101" in image_identity.cache_identities
    assert f"musicbrainz:{MBID}" in image_identity.cache_identities
    assert "name:synthetic alias" in image_identity.cache_identities
    assert set(resolve_artist_cluster_ids(db.conn, summary.key)) == {
        discogs,
        musicbrainz,
    }

    sections = query_artist_track_sections(db.conn, summary.key)
    assert [int(row["id"]) for row in sections.tracks] == [primary_track]
    assert [int(row["id"]) for row in sections.featured_on] == [featured_track]
    assert [int(row["id"]) for row in sections.collaborations] == [
        collaboration_track
    ]
    assert [int(row["id"]) for row in sections.group_appearances] == [group_track]
    db.close()


def test_same_provider_conflict_stays_separate_and_visibly_disambiguated(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "provider-conflict.sqlite3")
    first_track = _track(db, tmp_path, "first", "Shared Artist")
    second_track = _track(db, tmp_path, "second", "Shared Artist")
    _clear_seeded_graph(db)
    first = _artist(db, "Shared Artist", discogs="5101")
    second = _artist(db, "Shared Artist", discogs="5102")
    _credit(db, first_track, first)
    _credit(db, second_track, second)
    db.conn.commit()

    summaries = [
        summary
        for summary in query_artist_summaries(db.conn)
        if summary.key.normalized_name == "shared artist"
    ]
    assert len(summaries) == 2
    assert len({summary.display_name for summary in summaries}) == 2
    assert all("Discogs" in summary.display_name for summary in summaries)
    assert ArtistConsolidationService(db).plan().merges == ()
    assert {
        conflict.reason for conflict in ArtistConsolidationService(db).plan().conflicts
    } == {"discogs_id_conflict"}
    db.close()


@pytest.mark.parametrize("secondary_role", ("featured", "collaborator", "primary"))
def test_co_credited_distinct_artists_are_never_identity_merge_evidence(
    secondary_role: str, tmp_path: Path
):
    db = MusicVaultDB(tmp_path / f"co-credit-{secondary_role}.sqlite3")
    track_id = _track(db, tmp_path, f"co-credit-{secondary_role}", "Lead Artist")
    _clear_seeded_graph(db)
    lead = _artist(db, "Lead Artist")
    guest = _artist(db, "Guest Artist")
    _credit(db, track_id, lead)
    _credit(db, track_id, guest, role=secondary_role, order=1)
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert plan.merges == ()
    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 2
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_artist_credits"
    ).fetchone()[0] == 2
    db.close()


def test_unqualified_exact_same_name_rows_are_not_destructively_merged(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "same-name-unqualified.sqlite3")
    first_track = _track(db, tmp_path, "same-name-first", "Shared Name")
    second_track = _track(db, tmp_path, "same-name-second", "Shared Name")
    _clear_seeded_graph(db)
    first = _artist(db, "Shared Name")
    second = _artist(db, "Shared Name")
    _credit(db, first_track, first)
    _credit(db, second_track, second)
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert plan.merges == ()
    assert {conflict.reason for conflict in plan.conflicts} == {
        "ambiguous_exact_same_name"
    }
    # The browser may still cluster the two rows into one non-destructive card.
    assert len(
        [
            summary
            for summary in query_artist_summaries(db.conn)
            if summary.key.normalized_name == "shared name"
        ]
    ) == 1
    db.close()


def test_exact_same_name_complementary_provider_rows_merge_safely(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "same-name-complementary.sqlite3")
    first_track = _track(db, tmp_path, "complement-first", "Complementary Artist")
    second_track = _track(db, tmp_path, "complement-second", "Complementary Artist")
    _clear_seeded_graph(db)
    discogs = _artist(db, "Complementary Artist", discogs="6101")
    musicbrainz = _artist(db, "Complementary Artist", musicbrainz=MBID)
    _credit(db, first_track, discogs)
    _credit(db, second_track, musicbrainz)
    db.conn.commit()

    report = ArtistConsolidationService(db).run(dry_run=False)

    assert report.merged_artist_count == 1
    artist = db.conn.execute(
        "SELECT discogs_artist_id,musicbrainz_artist_id FROM artists"
    ).fetchone()
    assert tuple(artist) == ("6101", MBID)
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT artist_id) FROM track_artist_credits"
    ).fetchone()[0] == 1
    db.close()


def test_alias_shared_by_multiple_owners_does_not_bridge_identities(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "ambiguous-alias.sqlite3")
    track_ids = [
        _track(db, tmp_path, f"alias-{index}", name)
        for index, name in enumerate(("First Owner", "Second Owner", "Shared Alias"))
    ]
    _clear_seeded_graph(db)
    owners = [_artist(db, "First Owner"), _artist(db, "Second Owner")]
    candidate = _artist(db, "Shared Alias")
    for track_id, artist_id in zip(track_ids, (*owners, candidate), strict=True):
        _credit(db, track_id, artist_id)
    for owner in owners:
        db.conn.execute(
            """
            INSERT INTO artist_aliases (
                artist_id,alias_name,normalized_alias,alias_kind,provenance,
                confidence,created_at
            ) VALUES (?,'Shared Alias','shared alias','display_variant',
                      'synthetic',90,?)
            """,
            (owner, STAMP),
        )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()

    assert plan.merges == ()
    assert {conflict.reason for conflict in plan.conflicts} == {
        "ambiguous_preserved_alias"
    }
    db.close()


@pytest.mark.parametrize(
    "display",
    (
        "Ampersand Artist & Partner",
        "Comma Artist, Partner",
        "Slash Artist/Partner",
        "Conjunction Artist and Partner",
    ),
)
def test_plain_credit_punctuation_is_never_split(display: str, tmp_path: Path):
    db = MusicVaultDB(tmp_path / f"plain-{len(display)}.sqlite3")
    track_id = _track(db, tmp_path, f"plain-{len(display)}", display)
    db.conn.execute(
        """
        UPDATE track_artist_credits
        SET provenance='youtube_title_parsed',confidence=99
        WHERE track_id=?
        """,
        (track_id,),
    )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()
    assert plan.full_credit_repairs == ()
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_artist_credits WHERE track_id=?", (track_id,)
    ).fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize(
    ("phrase", "expected_role"),
    (("feat.", "featured"), ("featuring", "featured"), ("x", "collaborator")),
)
def test_explicit_source_title_role_phrase_repairs_combined_entity(
    phrase: str,
    expected_role: str,
    tmp_path: Path,
):
    display = f"Primary Unit {phrase} Related Unit"
    db = MusicVaultDB(tmp_path / f"explicit-{expected_role}-{len(phrase)}.sqlite3")
    track_id = _track(db, tmp_path, f"explicit-{len(phrase)}", display)
    malformed_artist_id = int(
        db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?", (track_id,)
        ).fetchone()[0]
    )
    db.conn.execute(
        """
        UPDATE track_artist_credits
        SET provenance='youtube_title_parsed',confidence=96
        WHERE track_id=?
        """,
        (track_id,),
    )
    db.conn.commit()

    service = ArtistConsolidationService(db)
    plan = service.plan()
    assert len(plan.full_credit_repairs) == 1
    service.apply(plan)
    credits = db.conn.execute(
        """
        SELECT artist.display_name,credit.role
        FROM track_artist_credits AS credit
        JOIN artists AS artist ON artist.id=credit.artist_id
        WHERE credit.track_id=? ORDER BY credit.credit_order
        """,
        (track_id,),
    ).fetchall()
    assert [tuple(row) for row in credits] == [
        ("Primary Unit", "primary"),
        ("Related Unit", expected_role),
    ]
    assert db.get_track(track_id)["artist"] == display
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artists WHERE id=?", (malformed_artist_id,)
    ).fetchone()[0] == 0
    db.close()


def test_blank_albums_share_one_virtual_card_without_persisting_virtual_title(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "virtual-album.sqlite3")
    blank_one = _track(db, tmp_path, "blank-one", "First Artist", album=None)
    blank_two = _track(db, tmp_path, "blank-two", "Second Artist", album="")
    literal = _track(
        db,
        tmp_path,
        "literal-unknown",
        "First Artist",
        album="Unknown Album",
    )
    db.conn.commit()

    summaries = query_album_summaries(db.conn)
    virtuals = [
        summary
        for summary in summaries
        if summary.key.virtual_kind == "singles_uncatalogued"
    ]
    assert len(virtuals) == 1
    assert virtuals[0].album_title == "Singles & Uncatalogued"
    assert virtuals[0].track_count == 3
    assert {int(row["id"]) for row in query_album_tracks(db.conn, virtuals[0].key)} == {
        blank_one,
        blank_two,
        literal,
    }
    assert db.conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE album='Singles & Uncatalogued'"
    ).fetchone()[0] == 0
    db.close()


def test_consolidation_apply_is_rollback_safe_inside_outer_transaction(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "savepoint.sqlite3")
    first_track = _track(db, tmp_path, "first-savepoint", "Savepoint Artist")
    second_track = _track(db, tmp_path, "second-savepoint", "Savepoint Artist")
    _clear_seeded_graph(db)
    first = _artist(db, "Savepoint Artist", discogs="6101")
    second = _artist(db, "Savepoint Artist", musicbrainz=MBID)
    _credit(db, first_track, first)
    _credit(db, second_track, second)
    db.conn.commit()
    plan = ArtistConsolidationService(db).plan()
    assert len(plan.merges) == 1

    db.conn.execute("BEGIN")
    report = ArtistConsolidationService(db).apply(plan)
    assert report.merged_artist_count == 1
    assert db.conn.in_transaction
    db.conn.rollback()

    assert db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 2
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT artist_id) FROM track_artist_credits"
    ).fetchone()[0] == 2
    db.close()


def test_title_parsed_version_suffix_uses_base_artist_without_plain_name_splitting(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "version-title.sqlite3")
    track_id = _track(
        db,
        tmp_path,
        "live-title",
        "Session Unit Live at Synthetic Hall",
    )
    _clear_seeded_graph(db)
    base = _artist(db, "Session Unit", discogs="7101")
    malformed = _artist(db, "Session Unit Live at Synthetic Hall")
    _credit(
        db,
        track_id,
        malformed,
        provenance="youtube_title_parsed",
    )
    db.conn.commit()

    plan = ArtistConsolidationService(db).plan()
    repair = next(item for item in plan.version_repairs if item.track_id == track_id)
    assert repair.canonical_artist_id == base
    assert repair.version_type == "live"
    assert repair.version_label == "Live at Synthetic Hall"
    db.close()


def test_same_name_provider_conflicts_leave_structured_legacy_artist_unassigned(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "ambiguous-structured-legacy.sqlite3")
    first_track = _track(db, tmp_path, "provider-one", "Shared Artist")
    legacy_track = _track(db, tmp_path, "legacy-only", "Shared Artist")
    second_track = _track(db, tmp_path, "provider-two", "Shared Artist")
    _clear_seeded_graph(db)
    first = _artist(db, "Shared Artist", discogs="8101")
    legacy = _artist(db, "Shared Artist")
    second = _artist(db, "Shared Artist", discogs="8102")
    _credit(db, first_track, first)
    _credit(db, legacy_track, legacy)
    _credit(db, second_track, second)
    db.conn.commit()

    summaries = [
        summary
        for summary in query_artist_summaries(db.conn)
        if summary.key.normalized_name == "shared artist"
    ]

    assert len(summaries) == 3
    assert {summary.key.cluster_artist_ids for summary in summaries} == {
        (first,),
        (legacy,),
        (second,),
    }
    assert sum("legacy unassigned" in summary.display_name for summary in summaries) == 1
    expected_tracks = {
        first: {first_track},
        legacy: {legacy_track},
        second: {second_track},
    }
    for summary in summaries:
        artist_id = summary.key.cluster_artist_ids[0]
        sections = query_artist_track_sections(db.conn, summary.key)
        assert {int(row["id"]) for row in sections.tracks} == expected_tracks[artist_id]
        assert sections.featured_on == ()
        assert sections.collaborations == ()
        assert sections.group_appearances == ()
    db.close()
