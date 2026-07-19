from __future__ import annotations

from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.canonical_albums import (
    CanonicalAlbumIdentityConflict,
    classify_album_kind,
    seed_existing_canonical_albums,
    upsert_track_canonical_album,
)
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.providers import ProviderArtistCredit, ProviderReleaseCandidate
from music_vault.metadata.service import MetadataService


class _TokenStore:
    def read(self) -> str:
        return "synthetic-token"


class _StaticDiscogs:
    def __init__(self, candidate: ProviderReleaseCandidate) -> None:
        self.candidate = candidate

    def search(self, _query, *, cancel_event=None):
        return (self.candidate,)


class _StaticMusicBrainz:
    def __init__(self, candidate: MetadataCandidate) -> None:
        self.candidate = candidate

    def search(self, _title: str, _artist: str | None = None, *, cancel_event=None):
        return (self.candidate,)


def _settings(*, musicbrainz: bool) -> dict[str, object]:
    return {
        "metadata_intelligence_enabled": True,
        "metadata_discogs_enabled": True,
        "metadata_musicbrainz_secondary_enabled": musicbrainz,
        "metadata_writeback_enabled": False,
        "metadata_fill_missing_artwork_enabled": False,
        "metadata_scan_existing_after_setup": False,
        "metadata_intelligence_consent_version": 1,
        "metadata_discogs_consent_version": 1,
    }


def _accepted_discogs(*, family_id: str) -> ProviderReleaseCandidate:
    score = 98.0
    return ProviderReleaseCandidate(
        provider="discogs",
        title="Accepted Song",
        artist="Synthetic Artist",
        album="Accepted Album",
        album_artist="Synthetic Artist",
        release_date="2001",
        original_release_date="2001",
        version_type="studio",
        duration_seconds=180.0,
        provider_score=score,
        release_id="synthetic-release-1",
        release_family_id=family_id,
        provider_reference="https://www.discogs.com/release/1",
        field_scores={
            name: score
            for name in (
                "title",
                "artist",
                "album",
                "album_artist",
                "release_date",
                "original_release_date",
                "version_type",
                "discogs_release_id",
                "provider_release_family_id",
            )
        },
    )


def _queued_track(db: MusicVaultDB, root: Path, name: str = "track") -> int:
    track_id = db.upsert_track(
        root / f"{name}.flac",
        title="Source Song",
        artist="Synthetic Artist",
        album="Source Album",
        album_artist="Synthetic Artist",
        duration_seconds=180.0,
    )
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    return track_id


def test_accepted_provider_family_is_persisted_and_drives_real_upsert_and_backfill(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "family.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _queued_track(db, tmp_path)
    family_id = "synthetic-catalogue:family-1"
    candidate = _accepted_discogs(family_id=family_id)
    service = MetadataIntelligenceService(
        db,
        _settings(musicbrainz=False),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _StaticDiscogs(candidate),
    )

    assert service.process_automatic_queue().applied == 1
    context = db.conn.execute(
        "SELECT provider_release_family_id FROM track_release_context WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert context[0] == family_id
    album = db.conn.execute(
        """
        SELECT album.canonical_key,album.provider_release_family_id
        FROM canonical_albums album
        JOIN track_album_memberships membership
          ON membership.canonical_album_id=album.id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert tuple(album) == (
        f"provider-release-family:{family_id}",
        family_id,
    )

    # Exercise the migration/backfill reader against identity written by the
    # real accepted-candidate path, not a direct identity-function fixture.
    db.conn.execute("DELETE FROM track_album_memberships")
    db.conn.execute("DELETE FROM canonical_albums")
    seed_existing_canonical_albums(db.conn)
    rebuilt = db.conn.execute(
        """
        SELECT album.canonical_key
        FROM canonical_albums album
        JOIN track_album_memberships membership
          ON membership.canonical_album_id=album.id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()[0]
    assert rebuilt == f"provider-release-family:{family_id}"
    before = db.conn.total_changes
    seed_existing_canonical_albums(db.conn)
    assert db.conn.total_changes == before
    db.close()


def test_accepted_edition_and_original_dates_reach_context_and_membership(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "edition-dates.sqlite3")
    track_id = _queued_track(db, tmp_path, "edition-dates")
    score = 98.0
    candidate = ProviderReleaseCandidate(
        provider="discogs",
        title="Accepted Song",
        artist="Synthetic Artist",
        artist_credits=(ProviderArtistCredit("Synthetic Artist"),),
        album="Accepted Album (2022 Reissue)",
        album_artist="Synthetic Artist",
        release_date="2022-04-01",
        original_release_date="1984",
        version_type="studio",
        duration_seconds=180.0,
        provider_score=score,
        release_id="synthetic-release-2022",
        master_id="synthetic-master-1984",
        track_position="A1",
        provider_reference="https://www.discogs.com/release/2022",
        field_scores={
            name: score
            for name in (
                "title",
                "artist",
                "artist_credits",
                "album",
                "album_artist",
                "release_date",
                "original_release_date",
                "version_type",
                "discogs_release_id",
                "discogs_master_id",
                "discogs_track_position",
            )
        },
    )
    service = MetadataIntelligenceService(
        db,
        _settings(musicbrainz=False),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _StaticDiscogs(candidate),
    )

    assert service.process_automatic_queue().applied == 1

    track = db.get_track(track_id)
    assert track["release_date"] == "2022-04-01"
    assert track["original_release_date"] == "1984"
    context = db.conn.execute(
        """
        SELECT release_date,original_release_date,discogs_release_id,
               discogs_master_id
        FROM track_release_context WHERE track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert tuple(context) == (
        "2022-04-01",
        "1984",
        "synthetic-release-2022",
        "synthetic-master-1984",
    )
    album = db.conn.execute(
        """
        SELECT album.title,album.original_release_date,
               membership.edition_label,membership.edition_release_date,
               membership.discogs_release_id
        FROM canonical_albums AS album
        JOIN track_album_memberships AS membership
          ON membership.canonical_album_id=album.id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert tuple(album) == (
        "Accepted Album",
        "1984",
        "2022 Reissue",
        "2022-04-01",
        "synthetic-release-2022",
    )
    db.close()


def test_accepted_structured_credits_drive_fallback_album_after_credit_replacement(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "fallback-after-credits.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "fallback.flac",
        title="Accepted Song",
        artist="Legacy Primary & Guest",
        album="Fallback Album",
        album_artist="Legacy Primary & Guest",
        duration_seconds=180.0,
    )
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    score = 98.0
    candidate = ProviderReleaseCandidate(
        provider="discogs",
        title="Accepted Song",
        artist="Structured Primary feat. Featured Guest",
        artist_credits=(
            ProviderArtistCredit(
                "Structured Primary",
                role="primary",
                artist_id="synthetic-primary",
                join_phrase=" feat. ",
            ),
            ProviderArtistCredit(
                "Featured Guest",
                role="featured",
                artist_id="synthetic-featured",
            ),
        ),
        album="Fallback Album",
        album_artist=None,
        duration_seconds=180.0,
        provider_score=score,
        release_id="edition-only-release",
        provider_reference="https://www.discogs.com/release/2",
        field_scores={
            name: score
            for name in (
                "title",
                "artist",
                "artist_credits",
                "album",
                "discogs_release_id",
            )
        },
    )
    service = MetadataIntelligenceService(
        db,
        _settings(musicbrainz=False),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _StaticDiscogs(candidate),
    )

    assert service.process_automatic_queue().applied == 1
    credits = db.conn.execute(
        """
        SELECT artist.display_name,credit.role
        FROM track_artist_credits credit
        JOIN artists artist ON artist.id=credit.artist_id
        WHERE credit.track_id=?
        ORDER BY credit.credit_order,credit.id
        """,
        (track_id,),
    ).fetchall()
    assert [tuple(row) for row in credits] == [
        ("Structured Primary", "primary"),
        ("Featured Guest", "featured"),
    ]
    canonical_key = db.conn.execute(
        """
        SELECT album.canonical_key
        FROM canonical_albums album
        JOIN track_album_memberships membership
          ON membership.canonical_album_id=album.id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()[0]
    assert canonical_key == "fallback:album:structured primary:fallback album"
    db.close()


def test_musicbrainz_release_group_persists_and_outranks_provider_family(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "release-group.sqlite3")
    track_id = _queued_track(db, tmp_path)
    family_id = "synthetic-catalogue:family-2"
    release_group_id = "00000000-0000-0000-0000-000000000207"
    discogs = _accepted_discogs(family_id=family_id)
    musicbrainz = MetadataCandidate(
        title=discogs.title,
        artist=discogs.artist,
        album=discogs.album,
        release_date=discogs.release_date,
        recording_id="00000000-0000-0000-0000-000000000201",
        release_id="00000000-0000-0000-0000-000000000202",
        release_group_id=release_group_id,
        score=98,
        duration_seconds=discogs.duration_seconds,
        album_artist=discogs.album_artist,
    )
    service = MetadataIntelligenceService(
        db,
        _settings(musicbrainz=True),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _StaticDiscogs(discogs),
        musicbrainz_provider_factory=lambda: _StaticMusicBrainz(musicbrainz),
    )

    assert service.process_automatic_queue().applied == 1
    context = db.conn.execute(
        """
        SELECT musicbrainz_release_group_id,provider_release_family_id
        FROM track_release_context WHERE track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert tuple(context) == (release_group_id, family_id)
    album = db.conn.execute(
        """
        SELECT album.canonical_key,album.musicbrainz_release_group_id,
               album.provider_release_family_id
        FROM canonical_albums album
        JOIN track_album_memberships membership
          ON membership.canonical_album_id=album.id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()
    assert tuple(album) == (
        f"musicbrainz-release-group:{release_group_id}",
        release_group_id,
        family_id,
    )

    # A later accepted track that knows only the lower-priority family still
    # resolves to the release-group card instead of colliding or duplicating.
    second_track_id = _queued_track(db, tmp_path, "family-only-later")
    family_only = MetadataIntelligenceService(
        db,
        _settings(musicbrainz=False),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _StaticDiscogs(discogs),
    )
    assert family_only.process_automatic_queue().processed == 1
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT canonical_album_id) FROM track_album_memberships"
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
        (second_track_id,),
    ).fetchone()[0] == db.conn.execute(
        "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
        (track_id,),
    ).fetchone()[0]
    db.close()


def test_user_confirmed_musicbrainz_save_promotes_fallback_membership(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "confirmed-release-group.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "confirmed.flac",
        title="Source Song",
        artist="Synthetic Artist",
        album="Source Album",
        album_artist="Synthetic Artist",
    )
    release_group_id = "00000000-0000-0000-0000-000000000307"

    MetadataService(db).apply_confirmed_candidate(
        track_id,
        {"album": "User Confirmed Album", "album_artist": "Synthetic Artist"},
        recording_id="00000000-0000-0000-0000-000000000301",
        release_id="00000000-0000-0000-0000-000000000302",
        release_group_id=release_group_id,
        confidence=99,
    )

    context = db.conn.execute(
        "SELECT musicbrainz_release_group_id FROM track_release_context WHERE track_id=?",
        (track_id,),
    ).fetchone()[0]
    key = db.conn.execute(
        """
        SELECT album.canonical_key
        FROM track_album_memberships membership
        JOIN canonical_albums album ON album.id=membership.canonical_album_id
        WHERE membership.track_id=?
        """,
        (track_id,),
    ).fetchone()[0]
    assert context == release_group_id
    assert key == f"musicbrainz-release-group:{release_group_id}"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM canonical_albums WHERE normalized_title='user confirmed album'"
    ).fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("provider_track_first", (True, False))
def test_partial_provider_coverage_converges_on_one_album_card(
    tmp_path: Path,
    provider_track_first: bool,
):
    db = MusicVaultDB(tmp_path / f"partial-{provider_track_first}.sqlite3")
    names = ("provider", "fallback") if provider_track_first else ("fallback", "provider")
    track_ids: dict[str, int] = {}
    for name in names:
        track_ids[name] = db.upsert_track(
            tmp_path / f"{name}.flac",
            title=f"{name.title()} Track",
            artist="Shared Artist",
            album="Shared Record",
            album_artist="Shared Artist",
        )
        if name == "provider":
            db.conn.execute(
                "UPDATE tracks SET discogs_master_id='shared-master' WHERE id=?",
                (track_ids[name],),
            )
            upsert_track_canonical_album(db.conn, track_ids[name])

    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT canonical_album_id) FROM track_album_memberships"
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT canonical_key FROM canonical_albums"
    ).fetchone()[0] == "discogs-master:shared-master"

    # Repeat through the full backfill reader with the fallback track ordered
    # before the provider-covered track; result must be order-independent.
    db.conn.execute("DELETE FROM track_album_memberships")
    db.conn.execute("DELETE FROM canonical_albums")
    seed_existing_canonical_albums(db.conn)
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT canonical_album_id) FROM track_album_memberships"
    ).fetchone()[0] == 1
    db.close()


def test_conflicting_strong_release_identities_fail_closed(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "conflicting-release-identities.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "conflict.flac",
        title="Conflict Track",
        artist="Conflict Artist",
        album="Conflict Album",
        album_artist="Conflict Artist",
    )
    timestamp = "2026-01-01T00:00:00Z"
    db.conn.execute(
        """
        INSERT INTO canonical_albums(
            canonical_key,title,normalized_title,album_artist_display,
            normalized_album_artist,album_kind,discogs_master_id,
            created_at,updated_at
        ) VALUES('discogs-master:conflict-master','Conflict Album',
                 'conflict album','Conflict Artist','conflict artist','album',?,?,?)
        """,
        ("conflict-master", timestamp, timestamp),
    )
    db.conn.execute(
        """
        INSERT INTO canonical_albums(
            canonical_key,title,normalized_title,album_artist_display,
            normalized_album_artist,album_kind,musicbrainz_release_group_id,
            created_at,updated_at
        ) VALUES('musicbrainz-release-group:conflict-group','Conflict Album',
                 'conflict album','Conflict Artist','conflict artist','album',?,?,?)
        """,
        ("conflict-group", timestamp, timestamp),
    )
    db.conn.execute(
        "UPDATE tracks SET discogs_master_id='conflict-master' WHERE id=?",
        (track_id,),
    )
    db.conn.execute(
        """
        INSERT INTO track_release_context(
            track_id,musicbrainz_release_group_id,updated_at
        ) VALUES(?,'conflict-group',CURRENT_TIMESTAMP)
        ON CONFLICT(track_id) DO UPDATE SET
            musicbrainz_release_group_id=excluded.musicbrainz_release_group_id
        """,
        (track_id,),
    )
    before_membership = db.conn.execute(
        "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
        (track_id,),
    ).fetchone()[0]

    with pytest.raises(CanonicalAlbumIdentityConflict):
        upsert_track_canonical_album(db.conn, track_id)

    assert db.conn.execute(
        "SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?",
        (track_id,),
    ).fetchone()[0] == before_membership
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 3
    db.close()


def test_album_clear_retires_active_membership_idempotently(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "clear.sqlite3")
    track_id = db.upsert_track(
        tmp_path / "clear.flac",
        title="Track",
        artist="Artist",
        album="Album To Clear",
        album_artist="Artist",
    )
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_album_memberships WHERE track_id=?", (track_id,)
    ).fetchone()[0] == 1

    MetadataService(db).apply_manual_patch(track_id, {"album": None})
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_album_memberships WHERE track_id=?", (track_id,)
    ).fetchone()[0] == 0
    before = db.conn.total_changes
    assert upsert_track_canonical_album(db.conn, track_id) is None
    assert db.conn.total_changes == before
    db.close()


def test_manual_same_provider_identity_refreshes_shared_card_but_automatic_does_not(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "presentation.sqlite3")
    track_ids = [
        db.upsert_track(
            tmp_path / f"shared-{index}.flac",
            title=f"Track {index}",
            artist="Primary Artist",
            album="Shared Album",
            album_artist="Primary Artist",
        )
        for index in (1, 2)
    ]
    for track_id in track_ids:
        db.conn.execute(
            "UPDATE tracks SET discogs_master_id='shared-master' WHERE id=?",
            (track_id,),
        )
        upsert_track_canonical_album(db.conn, track_id)

    MetadataService(db).record_source_observations(
        track_ids[0],
        provider="embedded",
        values={"album": "Incidental Automatic Name"},
    )
    unchanged = db.conn.execute(
        "SELECT title,album_artist_display,album_kind FROM canonical_albums "
        "WHERE canonical_key='discogs-master:shared-master'"
    ).fetchone()
    assert tuple(unchanged) == ("Shared Album", "Primary Artist", "album")

    MetadataService(db).apply_manual_patch(
        track_ids[0],
        {
            "album": "Corrected Live at Synthetic Hall",
            "album_artist": "Corrected Album Artist",
        },
    )
    corrected = db.conn.execute(
        "SELECT title,normalized_title,album_artist_display,album_kind "
        "FROM canonical_albums WHERE canonical_key='discogs-master:shared-master'"
    ).fetchone()
    assert tuple(corrected) == (
        "Corrected Live at Synthetic Hall",
        "corrected live at synthetic hall",
        "Corrected Album Artist",
        "live_album",
    )
    # The correction is browser identity only and never rewrites sibling rows.
    assert db.conn.execute(
        "SELECT album FROM tracks WHERE id=?", (track_ids[1],)
    ).fetchone()[0] == "Shared Album"
    db.close()


def test_live_word_in_studio_album_title_is_not_a_live_release():
    assert classify_album_kind("Live Through This") == "album"
    assert classify_album_kind("A Record Live") == "live_album"
    assert classify_album_kind("A Record", release_format="Album, Live") == "live_album"


def test_prerelease_schema7_adds_release_identity_columns_idempotently(tmp_path: Path):
    path = tmp_path / "schema7-additive.sqlite3"
    db = MusicVaultDB(path)
    track_id = db.upsert_track(
        tmp_path / "legacy.flac",
        title="Legacy Track",
        artist="Legacy Artist",
        album="Legacy Album",
        album_artist="Legacy Artist",
    )
    db.conn.execute(
        """
        INSERT INTO track_release_context (
            track_id,discogs_release_id,release_title,confidence,updated_at
        ) VALUES (?,'legacy-release','Legacy Album',90,CURRENT_TIMESTAMP)
        """,
        (track_id,),
    )
    for index in (
        "idx_release_context_mb_release_group",
        "idx_release_context_provider_family",
        "idx_canonical_albums_provider_family",
    ):
        db.conn.execute(f"DROP INDEX {index}")
    db.conn.execute(
        "ALTER TABLE track_release_context DROP COLUMN musicbrainz_release_group_id"
    )
    db.conn.execute(
        "ALTER TABLE track_release_context DROP COLUMN provider_release_family_id"
    )
    db.conn.execute(
        "ALTER TABLE canonical_albums DROP COLUMN provider_release_family_id"
    )
    counts = {
        table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "tracks",
            "track_release_context",
            "canonical_albums",
            "track_album_memberships",
        )
    }
    db.conn.commit()
    db.close()

    reopened = MusicVaultDB(path)
    assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert {
        "musicbrainz_release_group_id",
        "provider_release_family_id",
    } <= {
        str(row[1])
        for row in reopened.conn.execute("PRAGMA table_info(track_release_context)")
    }
    assert "provider_release_family_id" in {
        str(row[1])
        for row in reopened.conn.execute("PRAGMA table_info(canonical_albums)")
    }
    assert counts == {
        table: reopened.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in counts
    }
    before = reopened.conn.total_changes
    reopened.migrate()
    assert reopened.conn.total_changes == before
    assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert reopened.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()
