from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from music_vault.core import importer
from music_vault.core.db import MusicVaultDB
from music_vault.core.playlist_membership import PlaylistMembershipService
from music_vault.core.sync_sources import DESTINATION_PLAYLIST, SyncSourceService
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.providers import (
    ProviderArtistCredit,
    ProviderArtworkCandidate,
    ProviderReleaseCandidate,
)
from music_vault.metadata.service import MetadataAction, MetadataService
from music_vault.metadata.tag_writer import (
    MediaBackup,
    MediaFingerprint,
    PreparedTagWrite,
    TagWriteError,
    TagWriteResult,
)


class TokenStore:
    def __init__(self, value: str = "x") -> None:
        self.value = value
        self.read_count = 0

    def read(self) -> str:
        self.read_count += 1
        return self.value


class FakeDiscogs:
    def __init__(self, candidates=(), error: Exception | None = None) -> None:
        self.candidates = tuple(candidates)
        self.error = error
        self.calls = []

    def search(self, query, *, cancel_event=None):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.candidates)


class FakeMusicBrainz:
    def __init__(self, candidates=(), error: Exception | None = None) -> None:
        self.candidates = tuple(candidates)
        self.error = error
        self.calls = []

    def search(self, title, artist=None, *, cancel_event=None):
        self.calls.append((title, artist, cancel_event))
        if self.error is not None:
            raise self.error
        return list(self.candidates)


class FakeTagWriter:
    def __init__(self, root: Path, *, fail_commit: bool = False) -> None:
        self.root = root
        self.fail_commit = fail_commit
        self.arm_database_commit_failure = None
        self.backups = []
        self.prepared_patches = []
        self.commits = []
        self.restores = []
        self.original = MediaFingerprint("original-full", "audio", 100, 1.0, "synthetic")
        self.updated = MediaFingerprint("updated-full", "audio", 110, 1.0, "synthetic")

    def supports(self, path) -> bool:
        return Path(path).suffix.casefold() == ".mp3"

    def create_backup(self, path, backup_directory, *, identity):
        backup = MediaBackup(
            Path(path),
            self.root / f"{identity}.verified-backup",
            self.original,
        )
        self.backups.append((Path(backup_directory), backup))
        return backup

    def prepare(self, path, patch, *, expected_full_sha256, artwork_path=None):
        assert expected_full_sha256 == self.original.full_sha256
        assert artwork_path is None
        assert "artwork" not in patch
        assert "source_upload_date" not in patch
        self.prepared_patches.append(dict(patch))
        return PreparedTagWrite(
            Path(path),
            self.root / "synthetic-write.tmp",
            self.original,
            self.updated,
            dict(patch),
            None,
        )

    def commit(self, prepared, *, backup):
        self.commits.append((prepared, backup))
        if self.fail_commit:
            raise TagWriteError("synthetic_tag_commit_failure")
        if self.arm_database_commit_failure is not None:
            self.arm_database_commit_failure()
        return TagWriteResult(prepared.original_path, self.original, self.updated)

    def restore(
        self,
        original_path,
        backup_path,
        *,
        expected_backup_sha256,
        expected_current_sha256,
    ):
        self.restores.append(
            (
                Path(original_path),
                Path(backup_path),
                expected_backup_sha256,
                expected_current_sha256,
            )
        )
        return self.original


class FakeArtworkStore:
    def __init__(self, record) -> None:
        self.record = record
        self.calls = []

    def fetch_for_gap(self, candidate, **kwargs):
        self.calls.append((candidate, dict(kwargs)))
        return self.record


def config(**overrides) -> dict:
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


def youtube_track(
    db: MusicVaultDB,
    root: Path,
    *,
    index: int = 1,
    title: str = "Random Upload Title",
    artist: str = "Random Archive",
    cover_path: str | None = None,
    source_upload_date: str = "2024-06-12",
) -> int:
    return db.upsert_track(
        root / f"synthetic-{index}.mp3",
        title=title,
        artist=artist,
        cover_path=cover_path,
        source_kind="youtube",
        source_video_id=f"synthetic{index:02d}"[:11],
        source_upload_date=source_upload_date,
        duration_seconds=240.0,
    )


def discogs_candidate(**changes) -> ProviderReleaseCandidate:
    score = float(changes.pop("provider_score", 97.0))
    values = {
        "provider": "discogs",
        "title": "Canonical Signal",
        "artist": "Canonical Ensemble",
        "artist_credits": (
            ProviderArtistCredit(
                "Canonical Ensemble",
                role="primary",
                artist_id="101",
                entity_type="group",
            ),
        ),
        "album": "Canonical Album",
        "album_artist": "Canonical Ensemble",
        "release_date": "1984",
        "original_release_date": "1984",
        "version_type": "studio",
        "duration_seconds": 240.0,
        "provider_score": score,
        "release_id": "202",
        "master_id": "303",
        "track_position": "A1",
        "label": "Catalogue Company",
        "provider_reference": "https://www.discogs.com/release/202",
        "field_scores": {
            "title": score,
            "artist": score,
            "artist_credits": score,
            "album": score,
            "album_artist": score,
            "release_date": score,
            "original_release_date": score,
            "version_type": score,
            "version_label": score,
            "discogs_release_id": score,
            "discogs_master_id": score,
            "discogs_track_position": score,
            "artwork": score,
        },
    }
    values.update(changes)
    return ProviderReleaseCandidate(**values)


def mb_candidate(**changes) -> MetadataCandidate:
    values = {
        "title": "Secondary Signal",
        "artist": "Secondary Ensemble",
        "album": "Secondary Album",
        "release_date": "1986",
        "recording_id": "11111111-1111-4111-8111-111111111111",
        "release_id": "22222222-2222-4222-8222-222222222222",
        "score": 96,
        "duration_seconds": 240.0,
        "album_artist": "Secondary Ensemble",
    }
    values.update(changes)
    return MetadataCandidate(**values)


def enqueue(db: MusicVaultDB, track_id: int) -> None:
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)


def item_row(db: MusicVaultDB, track_id: int):
    return db.conn.execute(
        "SELECT * FROM metadata_intelligence_items WHERE track_id=? ORDER BY id DESC LIMIT 1",
        (int(track_id),),
    ).fetchone()


def test_provider_specific_consent_disables_discogs_but_keeps_secondary_fallback(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    enqueue(db, track_id)
    token = TokenStore()
    discogs = FakeDiscogs((discogs_candidate(),))
    musicbrainz = FakeMusicBrainz((mb_candidate(),))
    service = MetadataIntelligenceService(
        db,
        config(
            metadata_discogs_consent_version=0,
            metadata_musicbrainz_secondary_enabled=True,
        ),
        token_store=token,
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
    )

    result = service.process_automatic_queue()

    assert result.applied == 1
    assert token.read_count == 0
    assert discogs.calls == []
    assert len(musicbrainz.calls) == 1
    track = db.get_track(track_id)
    assert track["title"] == "Secondary Signal"
    assert track["artist"] == "Secondary Ensemble"
    row = item_row(db, track_id)
    assert row["musicbrainz_recording_id"] == mb_candidate().recording_id
    assert row["musicbrainz_release_id"] == mb_candidate().release_id
    db.close()


def test_missing_main_consent_runs_no_provider_writeback_or_artwork(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    enqueue(db, track_id)
    discogs = FakeDiscogs((discogs_candidate(),))
    tags = FakeTagWriter(tmp_path)
    art = FakeArtworkStore(SimpleNamespace(path=tmp_path / "cover.png"))
    source_config = config(
        metadata_intelligence_consent_version=0,
        metadata_writeback_enabled=True,
        metadata_fill_missing_artwork_enabled=True,
    )
    service = MetadataIntelligenceService(
        db,
        source_config,
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        tag_writer=tags,
        artwork_store_factory=lambda _token: art,
    )

    result = service.process_automatic_queue()

    assert result.processed == 0
    assert discogs.calls == []
    assert tags.backups == []
    assert art.calls == []
    assert db.get_track(track_id)["title"] == "Random Upload Title"
    assert MetadataIntelligenceJobStore(db).aggregate_counts()["queued"] == 1
    db.close()


def test_musicbrainz_applies_when_discogs_temporarily_fails(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    enqueue(db, track_id)
    discogs = FakeDiscogs(error=RuntimeError("discogs_temporarily_unavailable"))
    musicbrainz = FakeMusicBrainz((mb_candidate(),))
    service = MetadataIntelligenceService(
        db,
        config(metadata_musicbrainz_secondary_enabled=True),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
    )

    result = service.process_automatic_queue()

    assert result.applied == 1 and result.failed == 0
    assert db.get_track(track_id)["title"] == "Secondary Signal"
    assert item_row(db, track_id)["provider_agreement"] == "musicbrainz_only"
    db.close()


def test_service_field_locks_and_provider_disagreement_use_best_available_independently(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    locked_id = youtube_track(db, tmp_path, index=1)
    MetadataService(db).apply_actions(
        locked_id, {"title": MetadataAction.set("User Locked Title")}
    )
    enqueue(db, locked_id)
    service = MetadataIntelligenceService(
        db,
        config(),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((discogs_candidate(),)),
    )
    assert service.process_automatic_queue().applied == 1
    locked = db.get_track(locked_id)
    assert locked["title"] == "User Locked Title"
    assert locked["album"] == "Canonical Album"

    conflict_id = youtube_track(db, tmp_path, index=2)
    enqueue(db, conflict_id)
    discogs = FakeDiscogs((discogs_candidate(provider_score=94),))
    musicbrainz = FakeMusicBrainz(
        (mb_candidate(title="Conflicting Signal", artist="Conflicting Artist", score=94),)
    )
    conflict_service = MetadataIntelligenceService(
        db,
        config(metadata_musicbrainz_secondary_enabled=True),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
    )
    conflict = conflict_service.process_automatic_queue()
    assert conflict.review == 0
    assert conflict.applied_with_gaps == 1
    assert db.get_track(conflict_id)["title"] == "Canonical Signal"
    row = item_row(db, conflict_id)
    assert row["state"] == "applied_with_gaps"
    assert row["provider_agreement"] == "discogs_only"
    assert row["review_reason"] == "secondary_metadata_gaps"
    db.close()


def test_youtube_exclusive_live_uses_parsed_identity_not_uploader_or_upload_date(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(
        db,
        tmp_path,
        title="Aurora Unit - Midnight Signal [Live]",
        artist="Random Archive",
        source_upload_date="2024-06-12",
    )
    enqueue(db, track_id)
    service = MetadataIntelligenceService(
        db,
        config(
            metadata_discogs_enabled=False,
            metadata_musicbrainz_secondary_enabled=False,
        ),
        token_store=TokenStore(""),
    )

    result = service.process_automatic_queue()

    assert result.review == 0
    assert result.source_fallback == 1
    track = db.get_track(track_id)
    assert track["title"] == "Midnight Signal"
    assert track["artist"] == "Aurora Unit"
    assert track["version_type"] == "live"
    assert track["release_date"] is None
    assert track["year"] is None
    assert track["source_upload_date"] == "2024-06-12"
    assert item_row(db, track_id)["state"] == "source_fallback"
    db.close()


def test_official_live_date_applies_without_merging_separate_studio_track(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    studio_id = youtube_track(
        db,
        tmp_path,
        index=1,
        title="Aurora Unit - Midnight Signal",
        artist="Aurora Unit",
    )
    live_id = youtube_track(
        db,
        tmp_path,
        index=2,
        title="Aurora Unit - Midnight Signal [Live]",
        artist="Random Archive",
    )
    enqueue(db, live_id)
    live_candidate = discogs_candidate(
        title="Midnight Signal",
        artist="Aurora Unit",
        artist_credits=(ProviderArtistCredit("Aurora Unit", artist_id="102"),),
        album="Official Live Album",
        album_artist="Aurora Unit",
        release_date="2001-05-04",
        original_release_date="1984",
        version_type="live",
        is_official=True,
    )
    service = MetadataIntelligenceService(
        db,
        config(),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((live_candidate,)),
    )

    assert service.process_automatic_queue().applied == 1
    live = db.get_track(live_id)
    assert live["release_date"] == "2001-05-04"
    assert live["original_release_date"] == "1984"
    assert live["version_type"] == "live"
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert db.get_track(studio_id)["path"] != live["path"]
    db.close()


def test_enrichment_preserves_all_source_membership_and_runtime_config_state(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    playlist_id = db.create_playlist("Synthetic Managed Destination")
    membership = PlaylistMembershipService(db)
    membership.add_manual_origin(playlist_id, track_id)
    source = SyncSourceService(db, membership).create_source(
        "PLsynthetic123456789",
        destination_kind=DESTINATION_PLAYLIST,
        destination_playlist_id=playlist_id,
    )
    membership.set_source_origins(source.id, playlist_id, [(track_id, 0)])
    enqueue(db, track_id)

    watched_tables = (
        "source_track_identities",
        "sync_sources",
        "playlist_track_origins",
        "playlist_tracks",
    )
    before = {
        table: [tuple(row) for row in db.conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
        for table in watched_tables
    }
    source_config = config()
    source_config.update(
        {
            "shuffle": True,
            "repeat_mode": "all",
            "autoplay": False,
            "manual_queue": [91, 92],
            "party_mode_visual_preset": "aurora",
            "party_mode_lyrics_enabled": True,
            "lyrics_online_enabled": False,
        }
    )
    original_config = dict(source_config)
    service = MetadataIntelligenceService(
        db,
        source_config,
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((discogs_candidate(),)),
    )

    assert service.process_automatic_queue().applied == 1
    after = {
        table: [tuple(row) for row in db.conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
        for table in watched_tables
    }
    assert after == before
    assert source_config == original_config
    db.close()


def test_import_remains_successful_when_intelligence_enqueue_fails(
    tmp_path: Path, monkeypatch
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    path = tmp_path / "new-track.mp3"
    path.write_bytes(b"synthetic-local-file")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: {
            "title": "Local Signal",
            "artist": "Local Artist",
            "album": None,
            "album_artist": None,
            "release_date": None,
            "duration_seconds": 12.0,
        },
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    monkeypatch.setattr(
        MetadataIntelligenceJobStore,
        "enqueue_track",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic queue failure")),
    )

    assert importer.import_file(db, path) is True
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_intelligence_items").fetchone()[0] == 0
    db.close()


def test_failed_job_resume_requeues_only_failed_items_and_cancel_is_persistent(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    first = youtube_track(db, tmp_path, index=1)
    second = youtube_track(db, tmp_path, index=2)
    third = youtube_track(db, tmp_path, index=3)
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([first, second])
    failed = store.claim_next_item(job_id)
    assert failed is not None
    store.mark_item(failed.id, "failed")
    review = store.claim_next_item(job_id)
    assert review is not None
    store.mark_item(review.id, "review")
    service = MetadataIntelligenceService(db, config())

    service.resume_job(job_id)

    states = {
        int(row["track_id"]): str(row["state"])
        for row in db.conn.execute(
            "SELECT track_id,state FROM metadata_intelligence_items WHERE job_id=?",
            (job_id,),
        )
    }
    assert states[failed.track_id] == "queued"
    assert states[review.track_id] == "review"

    cancel_id = store.create_existing_library_job([third])
    service.cancel_job(cancel_id)
    assert store.job_summary(cancel_id).status == "cancelled"
    assert store.aggregate_counts(cancel_id)["cancelled"] == 1
    with pytest.raises(ValueError):
        service.resume_job(cancel_id)
    db.close()


def test_verified_text_writeback_commits_field_transaction_without_artwork_embedding(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    enqueue(db, track_id)
    writer = FakeTagWriter(tmp_path)
    service = MetadataIntelligenceService(
        db,
        config(metadata_writeback_enabled=True),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((discogs_candidate(),)),
        tag_writer=writer,
    )

    result = service.process_automatic_queue()

    assert result.applied == 1
    assert len(writer.backups) == len(writer.commits) == 1
    assert writer.prepared_patches[0]["title"] == "Canonical Signal"
    assert "artwork" not in writer.prepared_patches[0]
    assert item_row(db, track_id)["file_write_result"] == "verified"
    assert db.get_track(track_id)["title"] == "Canonical Signal"
    db.close()


def test_tag_write_failure_rolls_back_fields_ids_release_context_and_credits(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    enqueue(db, track_id)
    writer = FakeTagWriter(tmp_path, fail_commit=True)
    service = MetadataIntelligenceService(
        db,
        config(metadata_writeback_enabled=True),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((discogs_candidate(),)),
        tag_writer=writer,
    )

    result = service.process_automatic_queue()

    assert result.review == 0
    assert result.failed == 1
    track = db.get_track(track_id)
    assert track["title"] == "Random Upload Title"
    assert track["discogs_release_id"] is None
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_release_context WHERE track_id=?", (track_id,)
    ).fetchone()[0] == 0
    credits = db.conn.execute(
        """
        SELECT artist.display_name FROM track_artist_credits credit
        JOIN artists artist ON artist.id=credit.artist_id
        WHERE credit.track_id=?
        """,
        (track_id,),
    ).fetchall()
    assert [row[0] for row in credits] == ["Random Archive"]
    row = item_row(db, track_id)
    assert row["state"] == "failed"
    assert row["file_write_result"] == "restored"
    assert row["review_reason"] == "file_write_rollback_failure"
    db.close()


def test_database_commit_failure_after_tag_commit_restores_media_and_database(
    tmp_path: Path,
):
    path = tmp_path / "library.sqlite3"
    db = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    track_id = youtube_track(db, tmp_path)
    enqueue(db, track_id)
    writer = FakeTagWriter(tmp_path)
    service = MetadataIntelligenceService(
        db,
        config(metadata_writeback_enabled=True),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((discogs_candidate(),)),
        tag_writer=writer,
    )

    def worker_database():
        worker = MusicVaultDB(path, backup_dir=tmp_path / "backups")
        armed = {"value": False}

        def arm():
            armed["value"] = True

        def authorizer(action, argument1, _argument2, _database, _trigger):
            if (
                armed["value"]
                and action == sqlite3.SQLITE_TRANSACTION
                and str(argument1).upper() == "COMMIT"
            ):
                armed["value"] = False
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        worker.conn.set_authorizer(authorizer)
        writer.arm_database_commit_failure = arm
        return worker

    service._worker_database = worker_database  # type: ignore[method-assign]

    result = service.process_automatic_queue()

    assert result.failed == 1
    assert len(writer.commits) == 1
    assert len(writer.restores) == 1
    track = db.get_track(track_id)
    assert track["title"] == "Random Upload Title"
    assert track["discogs_release_id"] is None
    assert item_row(db, track_id)["state"] == "failed"
    db.close()


def test_gap_art_is_applied_but_valid_existing_art_is_preserved(tmp_path: Path):
    existing_path = tmp_path / "existing.png"
    existing = QImage(4, 4, QImage.Format.Format_RGB32)
    existing.fill(QColor("#22aa66"))
    assert existing.save(str(existing_path), "PNG")
    replacement_path = tmp_path / "discogs-replacement.png"
    replacement = QImage(4, 4, QImage.Format.Format_RGB32)
    replacement.fill(QColor("#805cff"))
    assert replacement.save(str(replacement_path), "PNG")

    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    gap_id = youtube_track(db, tmp_path, index=1)
    existing_id = youtube_track(db, tmp_path, index=2, cover_path=str(existing_path))
    enqueue(db, gap_id)
    enqueue(db, existing_id)
    artwork = ProviderArtworkCandidate(
        source_url="https://i.discogs.com/synthetic.png",
        provider_page_url="https://www.discogs.com/release/202",
        release_id="202",
        image_type="front",
        width=4,
        height=4,
    )
    candidate = discogs_candidate(artwork=artwork)
    record = SimpleNamespace(
        path=replacement_path,
        provider_page_url="https://www.discogs.com/release/202",
    )
    stores = []

    def store_factory(_token):
        store = FakeArtworkStore(record)
        stores.append(store)
        return store

    service = MetadataIntelligenceService(
        db,
        config(metadata_fill_missing_artwork_enabled=True),
        token_store=TokenStore(),
        discogs_provider_factory=lambda _token: FakeDiscogs((candidate,)),
        artwork_store_factory=store_factory,
    )

    result = service.process_automatic_queue()

    assert result.applied == 2
    assert Path(db.get_track(gap_id)["cover_path"]) == replacement_path
    assert Path(db.get_track(existing_id)["cover_path"]) == existing_path
    rows = {
        int(row["track_id"]): str(row["artwork_result"])
        for row in db.conn.execute(
            "SELECT track_id,artwork_result FROM metadata_intelligence_items"
        )
    }
    assert rows[gap_id] == "filled"
    assert rows[existing_id] == "preserved_existing"
    assert len(stores) == 2
    assert stores[1].calls[0][1]["current_cover_path"] == str(existing_path)
    db.close()
