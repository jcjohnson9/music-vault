from __future__ import annotations

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.service import MetadataAction, MetadataService


@pytest.fixture
def library(tmp_path):
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3")
    tracks = [db.upsert_track(tmp_path / f"{index}.synthetic", title=f"Fixture {index}", artist="Original") for index in range(2)]
    yield db, tracks, ArtistCreditService(db)
    db.close()


def rows(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {table: list(map(tuple, conn.execute(f'SELECT * FROM "{table}"'))) for table in tables}


def test_credited_as_is_track_specific_and_does_not_rename_canonical_artist(library):
    db, tracks, service = library
    first = service.replace_track_credits(tracks[0], [ArtistCreditInput(
        "Canonical Ensemble", entity_type="group", musicbrainz_artist_id="fixture-mbid", credited_as="The Fixture Alias",
    )], provenance="musicbrainz", confidence=99)[0]
    artist_before = tuple(db.conn.execute("SELECT * FROM artists WHERE id=?", (first.artist.id,)).fetchone())
    second = service.replace_track_credits(tracks[1], [{
        "display_name": "Canonical Ensemble", "entity_type": "group",
        "musicbrainz_artist_id": "fixture-mbid", "credited_as": "Different Credit",
    }], provenance="musicbrainz", confidence=99)[0]
    assert first.artist.id == second.artist.id
    assert tuple(db.conn.execute("SELECT * FROM artists WHERE id=?", (first.artist.id,)).fetchone()) == artist_before
    assert service.track_credits(tracks[0])[0].artist.display_name == "Canonical Ensemble"
    assert service.formatted_credit(service.track_credits(tracks[0])) == "The Fixture Alias"
    assert service.formatted_credit(service.track_credits(tracks[1])) == "Different Credit"
    assert db.get_track(tracks[0])["artist"] == "The Fixture Alias"
    assert db.get_track(tracks[1])["artist"] == "Different Credit"


def test_nullable_legacy_credits_fall_back_to_unsplit_entity_display(library):
    db, tracks, service = library
    credits = service.replace_track_credits(tracks[0], [ArtistCreditInput("Fixture Group & Co")], provenance="embedded", confidence=100)
    assert len(credits) == 1 and credits[0].credited_as is None
    assert service.formatted_credit(credits) == "Fixture Group & Co"


def test_credit_order_prefix_join_and_roles_preserve_credited_names(library):
    db, tracks, service = library
    credits = service.replace_track_credits(tracks[0], [
        ArtistCreditInput("Canonical A", credited_as="Alias A", musicbrainz_artist_id="fixture-a"),
        ArtistCreditInput("Canonical B", role="featured", join_phrase="feat.", credited_as="Alias B", musicbrainz_artist_id="fixture-b"),
    ], provenance="musicbrainz", confidence=99)
    assert service.formatted_credit(credits) == "Alias A feat. Alias B"
    assert [credit.credit_order for credit in credits] == [0, 1]
    assert [credit.artist.display_name for credit in credits] == ["Canonical A", "Canonical B"]
    assert credits[1].role == "featured"


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("update_display", [False, True])
def test_exact_credit_replacement_is_value_and_timestamp_idempotent(library, manual, update_display):
    db, tracks, service = library
    kwargs = dict(provenance="manual" if manual else "musicbrainz", confidence=99, is_manual=manual, update_display=update_display)
    credits = [ArtistCreditInput("Canonical", musicbrainz_artist_id="fixture-mbid", credited_as="Credit Alias")]
    first = service.replace_track_credits(tracks[0], credits, **kwargs)
    before = rows(db.conn)
    changes = db.conn.total_changes
    assert service.replace_track_credits(tracks[0], credits, **kwargs) == first
    assert rows(db.conn) == before
    assert db.conn.total_changes == changes


def test_manual_blank_artist_lock_prevents_automatic_credit_and_alias_creation(library):
    db, tracks, service = library
    MetadataService(db).apply_actions(tracks[0], {"artist": MetadataAction.clear()})
    before = rows(db.conn)
    service.replace_track_credits(tracks[0], [ArtistCreditInput(
        "Must Not Appear", credited_as="Nor This", musicbrainz_artist_id="rejected-id",
    )], provenance="musicbrainz", confidence=100)
    assert rows(db.conn) == before
    state = MetadataService(db).snapshot(tracks[0]).fields["artist"]
    assert state.value is None and state.is_manual and state.is_locked


def test_protected_credit_does_not_change_with_new_credited_name(library):
    db, tracks, service = library
    original = service.replace_track_credits(tracks[0], [ArtistCreditInput(
        "Canonical", credited_as="Chosen Credit", musicbrainz_artist_id="fixture-mbid",
    )], provenance="manual", is_manual=True, is_locked=True)
    before = rows(db.conn)
    current = service.replace_track_credits(tracks[0], [ArtistCreditInput(
        "Canonical", credited_as="Automatic Credit", musicbrainz_artist_id="fixture-mbid",
    )], provenance="musicbrainz", confidence=100)
    assert current == original and rows(db.conn) == before


@pytest.mark.parametrize("provider", [None, "fixture-provider"])
def test_reobserving_identical_artist_is_noop_including_total_changes(library, provider):
    db, _tracks, service = library
    artist = service.upsert_artist("Canonical Artist", entity_type="person", discogs_artist_id=provider)
    before = rows(db.conn)
    changes = db.conn.total_changes
    assert service.upsert_artist("Canonical Artist", discogs_artist_id=provider) == artist
    assert rows(db.conn) == before
    assert db.conn.total_changes == changes


def test_repaired_alias_preserves_canonical_name_but_allows_real_identity_enrichment(library):
    db, _tracks, service = library
    artist = service.upsert_artist("Corrected Canonical", discogs_artist_id="fixture-provider")
    db.conn.execute(
        "INSERT INTO artist_aliases (artist_id,alias_name,normalized_alias,alias_kind,provenance,confidence,created_at) "
        "VALUES (?,'Legacy Alias','legacy alias','display_variant','manual',100,'2000-01-01T00:00:00Z')",
        (artist.id,),
    )
    db.conn.commit()
    before = rows(db.conn)
    changes = db.conn.total_changes
    assert service.upsert_artist("Legacy Alias", discogs_artist_id="fixture-provider") == artist
    assert rows(db.conn) == before and db.conn.total_changes == changes
    upgraded = service.upsert_artist(
        "Legacy Alias", entity_type="person", discogs_artist_id="fixture-provider", musicbrainz_artist_id="fixture-mbid",
    )
    assert upgraded.id == artist.id and upgraded.display_name == "Corrected Canonical"
    assert upgraded.entity_type == "person" and upgraded.musicbrainz_artist_id == "fixture-mbid"
    assert db.conn.total_changes == changes + 1
    stable = rows(db.conn)
    service.upsert_artist("Legacy Alias", entity_type="person", discogs_artist_id="fixture-provider", musicbrainz_artist_id="fixture-mbid")
    assert rows(db.conn) == stable and db.conn.total_changes == changes + 1


def test_legacy_raw_schema_plain_credit_service_does_not_require_new_column(library):
    db, tracks, service = library
    db.conn.execute("ALTER TABLE track_artist_credits DROP COLUMN credited_as")
    db.conn.execute("PRAGMA user_version=7")
    db.conn.commit()
    credits = service.replace_track_credits(
        tracks[0], [ArtistCreditInput("Legacy Group & Co")], provenance="embedded", update_display=False,
    )
    assert len(credits) == 1 and credits[0].credited_as is None
    assert service.formatted_credit(credits) == "Legacy Group & Co"
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert "credited_as" not in {row[1] for row in db.conn.execute("PRAGMA table_info(track_artist_credits)")}


def test_legacy_raw_schema_alias_write_fails_before_any_change(library):
    db, tracks, service = library
    db.conn.execute("ALTER TABLE track_artist_credits DROP COLUMN credited_as")
    db.conn.commit()
    before = rows(db.conn)
    changes = db.conn.total_changes
    with pytest.raises(ValueError, match="require schema 10"):
        service.replace_track_credits(
            tracks[0], [ArtistCreditInput("Canonical", credited_as="Unavailable Credit")], provenance="embedded",
        )
    assert rows(db.conn) == before and db.conn.total_changes == changes
