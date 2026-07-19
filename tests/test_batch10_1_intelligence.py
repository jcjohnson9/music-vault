from __future__ import annotations

import json
from pathlib import Path

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.providers import ProviderArtistCredit, ProviderReleaseCandidate
from music_vault.metadata.schema import EDITABLE_METADATA_FIELDS
from music_vault.metadata.service import MetadataService


class _TokenStore:
    def read(self) -> str:
        return "synthetic-token"


class _Discogs:
    def __init__(self, candidates=(), error: Exception | None = None) -> None:
        self.candidates = tuple(candidates)
        self.error = error
        self.calls = []

    def search(self, query, *, cancel_event=None):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.candidates)


def _config(**overrides) -> dict:
    values = {
        "metadata_intelligence_enabled": True,
        "metadata_discogs_enabled": True,
        "metadata_musicbrainz_secondary_enabled": False,
        "metadata_writeback_enabled": False,
        "metadata_fill_missing_artwork_enabled": False,
        "metadata_scan_existing_after_setup": False,
        "metadata_intelligence_consent_version": 1,
        "metadata_discogs_consent_version": 1,
    }
    values.update(overrides)
    return values


def _track(db: MusicVaultDB, root: Path, *, title="Uploader Title", artist="Random Archive") -> int:
    return db.upsert_track(
        root / "synthetic.mp3",
        title=title,
        artist=artist,
        source_kind="youtube",
        source_video_id="abcdefghijk",
        duration_seconds=240.0,
    )


def _candidate(*, title="Canonical Song", artist="Canonical Group", score=98.0):
    return ProviderReleaseCandidate(
        provider="discogs",
        title=title,
        artist=artist,
        artist_credits=(
            ProviderArtistCredit(
                artist,
                role="primary",
                artist_id="101",
                entity_type="group",
            ),
        ),
        album="Canonical Album",
        album_artist=artist,
        release_date="1978",
        original_release_date="1978",
        version_type="studio",
        duration_seconds=240.0,
        provider_score=score,
        release_id="202",
        master_id="303",
        track_position="A1",
        label="Synthetic Records",
        provider_reference="https://www.discogs.com/release/202",
        field_scores={
            "title": score,
            "artist": score,
            "artist_credits": score,
            "album": score,
            "album_artist": score,
            "release_date": score,
            "original_release_date": score,
            "version_type": score,
            "discogs_release_id": score,
            "discogs_master_id": score,
            "discogs_track_position": score,
        },
    )


def test_new_import_job_applies_discogs_fields_and_structured_credits(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    provider = _Discogs((_candidate(),))
    service = MetadataIntelligenceService(
        db,
        _config(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: provider,
    )

    result = service.process_automatic_queue()

    assert result.processed == 1
    assert result.applied == 1
    track = db.get_track(track_id)
    assert track["title"] == "Canonical Song"
    assert track["artist"] == "Canonical Group"
    assert track["album"] == "Canonical Album"
    assert track["release_date"] == "1978"
    assert track["original_release_date"] == "1978"
    assert track["discogs_release_id"] == "202"
    assert track["discogs_master_id"] == "303"
    assert str(track["recording_group_key"]).startswith("rg1_")
    credits = db.conn.execute(
        """
        SELECT artist.display_name, artist.entity_type, credit.role
        FROM track_artist_credits credit
        JOIN artists artist ON artist.id=credit.artist_id
        WHERE credit.track_id=?
        """,
        (track_id,),
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in credits] == [
        ("Canonical Group", "group", "primary")
    ]
    stored_item = db.conn.execute(
        "SELECT state,field_proposal FROM metadata_intelligence_items WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert stored_item["state"] in {"applied", "applied_with_gaps"}
    stored_proposal = json.loads(stored_item["field_proposal"])
    assert stored_proposal["_discogs"]["artist_credits"] == [
        {
            "name": "Canonical Group",
            "role": "primary",
            "join_phrase": "",
            "entity_type": "group",
            "artist_id": "101",
        }
    ]
    assert provider.calls and provider.calls[0].title == "Uploader Title"
    db.close()


def test_import_intelligence_disabled_leaves_queue_and_metadata_unchanged(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    provider = _Discogs((_candidate(),))
    service = MetadataIntelligenceService(
        db,
        _config(metadata_intelligence_enabled=False),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: provider,
    )

    result = service.process_automatic_queue()

    assert result.processed == 0
    assert db.get_track(track_id)["title"] == "Uploader Title"
    assert provider.calls == []
    assert MetadataIntelligenceJobStore(db).aggregate_counts()["queued"] == 1
    db.close()


def test_provider_failure_is_private_retryable_job_state(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path, title="No Parse Pattern")
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    service = MetadataIntelligenceService(
        db,
        _config(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _Discogs(error=RuntimeError("temporary")),
    )

    first = service.process_automatic_queue()
    assert first.failed == 1
    row = db.conn.execute(
        "SELECT state,last_error FROM metadata_intelligence_items WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert row["state"] == "failed"
    assert "synthetic-token" not in str(row["last_error"])

    # Re-enqueueing an automatic item is the bounded retry path; completed or
    # review items are not repeated.
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    assert MetadataIntelligenceJobStore(db).aggregate_counts()["queued"] == 1
    db.close()


def test_existing_library_job_uses_each_track_once_despite_source_memberships(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    playlist_a = db.create_playlist("Source A")
    playlist_b = db.create_playlist("Source B")
    db.add_track_to_playlist(playlist_a, track_id)
    db.add_track_to_playlist(playlist_b, track_id)

    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job()
    counts = store.aggregate_counts(job_id)

    assert counts["total"] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM metadata_intelligence_items WHERE job_id=? AND track_id=?",
        (job_id, track_id),
    ).fetchone()[0] == 1
    db.close()


def test_manual_complete_track_skips_provider_and_completed_scan_is_not_repeated(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    MetadataService(db).lock_fields(
        track_id,
        [name for name in EDITABLE_METADATA_FIELDS if name != "artwork"],
    )
    provider = _Discogs((_candidate(),))
    service = MetadataIntelligenceService(
        db,
        _config(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: provider,
    )

    first = service.analyze_existing_library()
    second = service.analyze_existing_library()

    assert first.processed == 1
    assert second.processed == 0
    assert provider.calls == []
    row = db.conn.execute(
        "SELECT state,review_reason FROM metadata_intelligence_items WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert tuple(row) == ("skipped", "manual_or_confirmed_complete")
    assert db.conn.execute(
        "SELECT COUNT(*) FROM metadata_intelligence_jobs WHERE job_kind='existing_library'"
    ).fetchone()[0] == 1
    db.close()
