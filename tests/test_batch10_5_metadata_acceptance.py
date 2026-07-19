from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.canonical_albums import (
    SINGLES_UNCATALOGUED_TITLE,
    canonical_album_identity,
    uncatalogued_track_ids,
)
from music_vault.metadata.ensemble import FieldAction, build_metadata_ensemble
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.providers import ProviderReleaseCandidate
from music_vault.metadata.review_policy import ReviewOutcome
from music_vault.metadata.review_reclassification import best_available_reclassify
from music_vault.metadata.service import AutomaticMetadataField, MetadataService
from music_vault.metadata.soundtrack import (
    SoundtrackKind,
    classify_soundtrack,
    soundtrack_search_variants,
)
from music_vault.metadata.title_parser import (
    parse_youtube_title,
    title_orientation_hypotheses,
)


def _track(
    db: MusicVaultDB,
    root: Path,
    *,
    title: str,
    artist: str,
    album: str | None = None,
) -> int:
    path = root / f"{title.replace(' ', '-')}.mp3"
    path.write_bytes(b"synthetic metadata acceptance fixture")
    return db.upsert_track(
        path,
        title=title,
        artist=artist,
        album=album,
        source_kind="youtube",
        source_video_id="synthetic10_5",
    )


def test_dash_title_keeps_raw_observation_and_both_orientation_hypotheses():
    raw = "Song Name - Band Name (1978)"
    parsed = parse_youtube_title(raw)

    assert parsed.raw_title == raw
    assert [(item.artist, item.title) for item in title_orientation_hypotheses(parsed)] == [
        ("Song Name", "Band Name"),
        ("Band Name", "Song Name"),
    ]


def test_reverse_provider_query_uses_artist_without_live_version_suffix():
    parsed = parse_youtube_title("Song Name - Band Name (Live)")
    snapshot = SimpleNamespace(
        path="synthetic.mp3",
        value=lambda name: {
            "title": "Song Name - Band Name (Live)",
            "artist": "Uploader",
            "album": None,
        }.get(name),
    )

    queries = MetadataIntelligenceService._query_variants(
        snapshot,
        {"duration_seconds": 240},
        parsed,
    )

    assert len(queries) == 2
    assert (queries[1].title, queries[1].artist) == ("Song Name", "Band Name")
    assert queries[1].version_type == "live"


@pytest.mark.parametrize(
    "raw",
    (
        "Song Name - Band Name - Live",
        "Song Name - Band Name - Live at Synthetic Venue",
    ),
)
def test_reverse_provider_query_strips_delimited_live_suffix(raw: str):
    parsed = parse_youtube_title(raw)

    reverse = title_orientation_hypotheses(parsed)[1]

    assert (reverse.title, reverse.artist) == ("Song Name", "Band Name")
    assert parsed.version_type == "live"


def test_discogs_overrides_backwards_parser_and_musicbrainz_disagreement():
    parsed = parse_youtube_title("Song Name - Band Name (1978)")
    discogs = ProviderReleaseCandidate(
        provider="discogs",
        title="Song Name",
        artist="Band Name",
        album="Catalogue Record",
        original_release_date="1978",
        provider_score=78,
    )
    musicbrainz = SimpleNamespace(
        title="Different Name",
        artist="Different Band",
        album=None,
        provider_score=96,
        provider_reference=None,
    )

    ensemble = build_metadata_ensemble(
        current={"title": "Band Name", "artist": "Song Name"},
        discogs_candidates=(discogs,),
        musicbrainz_candidates=(musicbrainz,),
        parsed_title=parsed,
    )

    assert ensemble.field("title").value == "Song Name"
    assert ensemble.field("artist").value == "Band Name"
    assert ensemble.field("title").source == "discogs"
    assert ensemble.field("title").action is FieldAction.APPLY
    assert not ensemble.field("title").conflict


def test_musicbrainz_fills_field_when_discogs_field_is_not_useful():
    discogs = ProviderReleaseCandidate(
        provider="discogs",
        title="Synthetic Title",
        artist="Synthetic Artist",
        album="Low Confidence Discogs Album",
        provider_score=92,
        field_scores={"title": 92, "artist": 92, "album": 20},
    )
    musicbrainz = SimpleNamespace(
        title="Synthetic Title",
        artist="Synthetic Artist",
        album="Useful MusicBrainz Album",
        provider_score=82,
        provider_reference="synthetic:musicbrainz",
    )

    ensemble = build_metadata_ensemble(
        current={},
        discogs_candidates=(discogs,),
        musicbrainz_candidates=(musicbrainz,),
    )

    album = ensemble.field("album")
    assert album.source == "musicbrainz"
    assert album.value == "Useful MusicBrainz Album"
    assert album.action is FieldAction.APPLY


def test_offline_reclassification_plans_applies_and_rolls_back_crosswise_repair(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "repair.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        title="Band Name",
        artist="Song Name",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "raw_title": "Song Name - Band Name (1978)",
            "title": "Band Name",
            "artist": "Song Name",
            "pattern": "artist_dash_title",
            "year": 1978,
        },
        field_proposal={
            "_current": {"title": "Band Name", "artist": "Song Name"},
            "_discogs": {
                "title": "Song Name",
                "artist": "Band Name",
                "album": "Catalogue Record",
                "original_release_date": "1978",
                "score": 78,
                "provider_reference": "https://www.discogs.com/release/1",
            },
            "_musicbrainz": {},
            "_sources": {},
            "_reasons": {"title": ["provider_value_conflict"]},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={
            "title": 78,
            "artist": 78,
            "album": 78,
            "original_release_date": 78,
        },
        provider_agreement="discogs_only",
        review_reason="title_ambiguity",
    )

    plan = best_available_reclassify(db, apply=False)
    assert plan.reversed_orientation_repairs == 1
    assert plan.album_fields_applied == 1
    assert plan.terminalized_review_items == 1
    assert plan.needs_review == 0
    assert tuple(db.get_track(track_id)[name] for name in ("title", "artist", "album")) == (
        "Band Name",
        "Song Name",
        None,
    )

    db.conn.execute("BEGIN")
    applied = best_available_reclassify(db, apply=True)
    assert applied.changed == 1
    assert applied.safe_fields_applied >= 3
    assert tuple(db.get_track(track_id)[name] for name in ("title", "artist", "album")) == (
        "Song Name",
        "Band Name",
        "Catalogue Record",
    )
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=?", (track_id,)
    ).fetchone()[0] >= 3
    db.conn.rollback()

    assert tuple(db.get_track(track_id)[name] for name in ("title", "artist", "album")) == (
        "Band Name",
        "Song Name",
        None,
    )
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == "review"
    db.close()


def test_corrupt_stored_evidence_is_failed_not_left_in_review(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "corrupt.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path, title="Title", artist="Artist")
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(item.id, "review", review_reason="provider_disagreement")
    db.conn.execute(
        "UPDATE metadata_intelligence_items SET field_proposal='{bad-json' WHERE id=?",
        (item.id,),
    )

    report = best_available_reclassify(db, apply=True)

    assert report.operational_failures == 1
    assert report.needs_review == 0
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == "failed"
    db.close()


def test_legacy_no_match_with_source_identity_becomes_accepted_fallback(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "no-match.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path, title="Source Title", artist="Source Artist")
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "no_match",
        parsed_hints={
            "raw_title": "Source Artist - Source Title",
            "title": "Source Title",
            "artist": "Source Artist",
            "pattern": "artist_dash_title",
        },
            field_proposal={
                "_current": {"title": "Source Title", "artist": "Source Artist"},
                "_discogs": {},
                "_musicbrainz": {},
                "_artwork": {"candidate_available": False},
                "_orientation": {
                    "evaluated_count": 2,
                    "selected": "left_is_artist",
                    "provider_confirmed": False,
                    "requires_provider_adjudication": True,
                    "reasons": ["provisional_conventional_orientation"],
                },
            },
        field_confidence={},
        provider_agreement="none",
        review_reason="no_provider_match",
    )

    report = best_available_reclassify(db, apply=True)

    assert report.scanned == 1
    assert report.source_fallback == 1
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == "source_fallback"
    db.close()


def test_hard_duration_mismatch_preserves_identity_and_applies_no_provider_history(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "duration-mismatch.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        title="Current Title",
        artist="Current Artist",
        album="Current Album",
    )
    db.conn.execute(
        "UPDATE tracks SET duration_seconds=200 WHERE id=?", (track_id,)
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "raw_title": "Current Artist - Current Title",
            "title": "Current Title",
            "artist": "Current Artist",
            "pattern": "artist_dash_title",
        },
        field_proposal={
            "_current": {
                "title": "Current Title",
                "artist": "Current Artist",
                "album": "Current Album",
                "duration_seconds": 200,
            },
            "_discogs": {
                "title": "Wrong Recording",
                "artist": "Wrong Artist",
                "album": "Wrong Album",
                "duration_seconds": 900,
                "score": 99,
                "provider_reference": "synthetic:duration-mismatch",
            },
            "_musicbrainz": {},
            "_reasons": {"duration": ["duration_mismatch"]},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={"title": 99, "artist": 99, "album": 99},
        provider_agreement="discogs_only",
        review_reason="incompatible_duration",
    )

    report = best_available_reclassify(db, apply=True)

    assert report.safe_fields_applied == 0
    assert tuple(db.get_track(track_id)[name] for name in ("title", "artist", "album")) == (
        "Current Title",
        "Current Artist",
        "Current Album",
    )
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == ReviewOutcome.APPLIED_WITH_GAPS.value
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history "
        "WHERE track_id=? AND actor='metadata_review_reclassification'",
        (track_id,),
    ).fetchone()[0] == 0
    db.close()


def test_saved_duration_mismatch_blocks_only_that_provider_catalogue_evidence(
    tmp_path: Path,
):
    db = MusicVaultDB(
        tmp_path / "provider-duration.sqlite3", backup_dir=tmp_path / "backups"
    )
    track_id = _track(
        db,
        tmp_path,
        title="Current Title",
        artist="Current Artist",
    )
    db.conn.execute(
        "UPDATE tracks SET duration_seconds=200 WHERE id=?", (track_id,)
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "raw_title": "Current Artist - Current Title",
            "title": "Current Title",
            "artist": "Current Artist",
            "pattern": "artist_dash_title",
        },
        discogs_release_id="wrong-discogs-release",
        discogs_master_id="wrong-discogs-master",
        musicbrainz_recording_id="matching-mb-recording",
        musicbrainz_release_id="matching-mb-release",
        field_proposal={
            "_current": {
                "title": "Current Title",
                "artist": "Current Artist",
                "duration_seconds": 200,
            },
            "_sources": {"album": "discogs"},
            "_discogs": {
                "title": "Wrong Recording",
                "artist": "Wrong Artist",
                "album": "Wrong Album",
                "duration_seconds": 900,
                "release_id": "wrong-discogs-release",
                "master_id": "wrong-discogs-master",
                "artist_credits": [
                    {
                        "name": "Wrong Artist",
                        "role": "primary",
                        "artist_id": "wrong-discogs-artist",
                    }
                ],
                "score": 99,
                "field_scores": {
                    "album": 99,
                    "discogs_release_id": 99,
                    "discogs_master_id": 99,
                    "artist_credits": 99,
                },
            },
            "_musicbrainz": {
                "title": "Current Title",
                "artist": "Current Artist",
                "duration_seconds": 200,
                "recording_id": "matching-mb-recording",
                "release_id": "matching-mb-release",
                "score": 90,
                "field_scores": {
                    "musicbrainz_recording_id": 90,
                    "musicbrainz_release_id": 90,
                },
            },
            "_artwork": {"candidate_available": False},
        },
        field_confidence={"album": 99},
        provider_agreement="agreed",
        review_reason="album_ambiguity",
    )

    report = best_available_reclassify(db, apply=True)
    track = db.get_track(track_id)

    assert report.safe_fields_applied == 0
    assert track["album"] is None
    assert track["discogs_release_id"] is None
    assert track["discogs_master_id"] is None
    assert track["musicbrainz_recording_id"] == "matching-mb-recording"
    assert track["musicbrainz_release_id"] == "matching-mb-release"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_release_context "
        "WHERE track_id=? AND (discogs_release_id IS NOT NULL "
        "OR discogs_master_id IS NOT NULL)",
        (track_id,),
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artists WHERE discogs_artist_id=?",
        ("wrong-discogs-artist",),
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == ReviewOutcome.APPLIED_WITH_GAPS.value
    db.close()


def test_low_score_raw_provider_cannot_borrow_embedded_field_confidence(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "score-provenance.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        title="Trusted Title",
        artist="Trusted Artist",
        album="Trusted Album",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "raw_title": "Trusted Artist - Trusted Title",
            "title": "Trusted Title",
            "artist": "Trusted Artist",
            "pattern": "artist_dash_title",
        },
        field_proposal={
            "title": "Trusted Title",
            "artist": "Trusted Artist",
            "album": "Trusted Album",
            "_current": {
                "title": "Trusted Title",
                "artist": "Trusted Artist",
                "album": "Trusted Album",
            },
            "_sources": {
                "title": "embedded_or_existing",
                "artist": "embedded_or_existing",
                "album": "embedded_or_existing",
            },
            "_discogs": {
                "title": "Low Score Title",
                "artist": "Low Score Artist",
                "album": "Low Score Album",
                "score": 20,
                "provider_reference": "synthetic:low-score",
            },
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={"title": 82, "artist": 82, "album": 82},
        provider_agreement="discogs_only",
        review_reason="album_ambiguity",
    )

    report = best_available_reclassify(db, apply=True)

    assert report.safe_fields_applied == 0
    assert tuple(db.get_track(track_id)[name] for name in ("title", "artist", "album")) == (
        "Trusted Title",
        "Trusted Artist",
        "Trusted Album",
    )
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history "
        "WHERE track_id=? AND actor='metadata_review_reclassification'",
        (track_id,),
    ).fetchone()[0] == 0
    db.close()


def test_candidate_wide_score_cannot_override_low_provider_field_confidence(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "field-score.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        title="Trusted Title",
        artist="Trusted Artist",
        album="Trusted Album",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "title": "Trusted Title",
            "artist": "Trusted Artist",
            "pattern": "artist_dash_title",
        },
        field_proposal={
            "album": "Ambiguous Provider Album",
            "_current": {
                "title": "Trusted Title",
                "artist": "Trusted Artist",
                "album": "Trusted Album",
            },
            "_sources": {"album": "discogs"},
            "_discogs": {
                "album": "Ambiguous Provider Album",
                "score": 90,
                "provider_reference": "synthetic:field-score",
            },
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={"album": 20},
        provider_agreement="discogs_only",
        review_reason="album_ambiguity",
    )

    report = best_available_reclassify(db, apply=True)

    assert report.safe_fields_applied == 0
    assert db.get_track(track_id)["album"] == "Trusted Album"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history "
        "WHERE track_id=? AND actor='metadata_review_reclassification'",
        (track_id,),
    ).fetchone()[0] == 0
    db.close()


def test_failed_malformed_nested_evidence_cannot_apply_populated_proposal(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "malformed-populated.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path, title="Trusted Title", artist="Trusted Artist")
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "title": "Trusted Title",
            "artist": "Trusted Artist",
            "pattern": "artist_dash_title",
        },
        field_proposal={
            "title": "Must Not Apply",
            "_current": {"title": "Trusted Title", "artist": "Trusted Artist"},
            "_sources": {"title": "discogs"},
            "_discogs": ["malformed-nested-evidence"],
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={"title": 99},
        provider_agreement="discogs_only",
        review_reason="provider_or_apply_failure",
    )

    report = best_available_reclassify(db, apply=True)

    assert report.operational_failures == 1
    assert report.safe_fields_applied == 0
    assert db.get_track(track_id)["title"] == "Trusted Title"
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == ReviewOutcome.FAILED.value
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history "
        "WHERE track_id=? AND actor='metadata_review_reclassification'",
        (track_id,),
    ).fetchone()[0] == 0
    db.close()


def test_medium_confidence_database_change_is_not_offered_to_tag_writer(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "medium.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path, title="Old Title", artist="Artist")
    result = MetadataService(db).apply_automatic_fields(
        track_id,
        {
            "title": AutomaticMetadataField(
                "Accepted Database Title", 72, provider="adjudicated_source_title"
            )
        },
        minimum_confidence=60,
    )

    class NoWriteTagWriter:
        @staticmethod
        def supports(_path):
            return True

        @staticmethod
        def create_backup(*_args, **_kwargs):
            raise AssertionError("medium-confidence metadata reached tag writeback")

    service = MetadataIntelligenceService(
        db,
        {},
        tag_writer=NoWriteTagWriter(),
    )
    status, committed = service._write_tags(
        db,
        SimpleNamespace(track_id=track_id, job_id="synthetic"),
        result,
        high_confidence_fields=frozenset(),
    )
    assert (status, committed) == ("not_needed", None)
    db.close()


def test_blank_album_uses_one_virtual_collection_without_persisting_metadata(
    tmp_path: Path,
):
    db = MusicVaultDB(tmp_path / "uncatalogued.sqlite3", backup_dir=tmp_path / "backups")
    first = _track(db, tmp_path, title="Loose One", artist="Artist One")
    second = _track(db, tmp_path, title="Loose Two", artist="Artist Two")
    literal = _track(
        db,
        tmp_path,
        title="Loose Placeholder",
        artist="Artist Three",
        album="Unknown Album",
    )

    assert uncatalogued_track_ids(db.conn) == (first, second, literal)
    assert db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0] == 0
    assert db.get_track(literal)["album"] == "Unknown Album"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_album_memberships"
    ).fetchone()[0] == 0
    assert SINGLES_UNCATALOGUED_TITLE == "Singles & Uncatalogued"
    with pytest.raises(ValueError, match="virtual"):
        canonical_album_identity("", "Artist")
    with pytest.raises(ValueError, match="virtual"):
        canonical_album_identity("Unknown Album", "Artist")
    db.close()


def test_soundtrack_context_is_nonblocking_and_queries_are_bounded():
    classification = classify_soundtrack(
        title="Synthetic Theme",
        album="Synthetic Game Original Soundtrack",
        release_format="CD, ambiguous regional pressing",
        album_artist="Various Artists",
    )
    variants = soundtrack_search_variants(
        track_title="Synthetic Theme",
        artist="Synthetic Composer",
        work_title="Synthetic Game",
        composer="Synthetic Composer",
        limit=4,
    )

    assert classification.kind is SoundtrackKind.GAME_SOUNDTRACK
    assert classification.various_artists_release_context
    assert len(variants) == 4
    assert len(set(variants)) == len(variants)


def test_production_query_variants_include_bounded_soundtrack_work_context():
    values = {
        "title": "Synthetic Composer - Synthetic Theme [Soundtrack]",
        "artist": "Synthetic Composer",
        "album": "Synthetic Game Original Soundtrack",
        "album_artist": "Various Artists",
        "release_format": "Compilation",
    }
    snapshot = SimpleNamespace(
        path="synthetic.mp3", value=lambda name: values.get(name)
    )
    parsed = parse_youtube_title(values["title"])

    queries = MetadataIntelligenceService._query_variants(
        snapshot, {"duration_seconds": 180}, parsed
    )

    identities = {(query.title.casefold(), (query.artist or "").casefold()) for query in queries}
    assert len(queries) <= 2
    assert len(identities) == len(queries)
    assert all("synthetic game" in (query.album or "").casefold() for query in queries)
    assert all(query.version_type == "soundtrack" for query in queries)


class _TokenStore:
    @staticmethod
    def read() -> str:
        return "synthetic-token-not-a-secret"


class _MediumDiscogsProvider:
    def search(self, _query, *, cancel_event=None):
        return (
            ProviderReleaseCandidate(
                provider="discogs",
                title="Song Name",
                artist="Band Name",
                album="Catalogue Record",
                original_release_date="1978",
                provider_score=78,
                release_id="synthetic-release",
                master_id="synthetic-master",
            ),
        )


def test_new_intelligence_item_never_enters_ordinary_review(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "intelligence.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path, title="Song Name - Band Name (1978)", artist="Uploader")
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    service = MetadataIntelligenceService(
        db,
        {
            "metadata_intelligence_enabled": True,
            "metadata_discogs_enabled": True,
            "metadata_musicbrainz_secondary_enabled": False,
            "metadata_writeback_enabled": False,
            "metadata_fill_missing_artwork_enabled": False,
            "metadata_intelligence_consent_version": 1,
            "metadata_discogs_consent_version": 1,
        },
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: _MediumDiscogsProvider(),
    )

    result = service.process_automatic_queue()
    state = db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE track_id=?", (track_id,)
    ).fetchone()[0]

    assert result.review == 0
    assert state in {ReviewOutcome.APPLIED.value, ReviewOutcome.APPLIED_WITH_GAPS.value}
    assert state != ReviewOutcome.NEEDS_REVIEW.value
    db.close()
