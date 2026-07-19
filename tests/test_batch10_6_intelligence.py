from __future__ import annotations

import json
from pathlib import Path

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.providers import ProviderArtistCredit, ProviderReleaseCandidate
from music_vault.metadata.review_policy import (
    ReviewOutcome,
    classify_stored_review_evidence,
)
from music_vault.metadata.service import MetadataAction, MetadataService


class _TokenStore:
    def __init__(self) -> None:
        self.read_count = 0

    def read(self) -> str:
        self.read_count += 1
        return "synthetic-token"


class _SequencedDiscogs:
    def __init__(self, *groups) -> None:
        self.groups = tuple(tuple(group) for group in groups)
        self.calls = []

    def search(self, query, *, cancel_event=None):
        self.calls.append(query)
        index = min(len(self.calls) - 1, len(self.groups) - 1)
        return self.groups[index] if self.groups else ()


class _MusicBrainz:
    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)
        self.calls = []

    def search(self, title, artist=None, *, cancel_event=None):
        self.calls.append((title, artist))
        return self.candidates


def _settings(**changes) -> dict[str, object]:
    values: dict[str, object] = {
        "metadata_intelligence_enabled": True,
        "metadata_discogs_enabled": True,
        "metadata_musicbrainz_secondary_enabled": False,
        "metadata_writeback_enabled": False,
        "metadata_fill_missing_artwork_enabled": False,
        "metadata_intelligence_consent_version": 1,
        "metadata_discogs_consent_version": 1,
    }
    values.update(changes)
    return values


def _track(
    db: MusicVaultDB,
    root: Path,
    *,
    raw_title: str = "Anthem of the Republic - The Cosmic Assembly (1978)",
    current_title: str | None = None,
    current_artist: str | None = None,
    index: int = 1,
) -> int:
    path = root / f"orientation-{index}.mp3"
    path.write_bytes(b"synthetic orientation media; never written by metadata tests")
    track_id = db.upsert_track(
        path,
        title=raw_title,
        artist="Synthetic Archive Channel",
        source_kind="youtube",
        source_video_id=f"orientation{index:02d}"[:11],
        duration_seconds=240,
    )
    if current_title is not None or current_artist is not None:
        with db.conn:
            if current_title is not None:
                db.conn.execute(
                    "UPDATE tracks SET title=? WHERE id=?", (current_title, track_id)
                )
                db.conn.execute(
                    "UPDATE track_metadata_fields SET value=? "
                    "WHERE track_id=? AND field_name='title'",
                    (current_title, track_id),
                )
            if current_artist is not None:
                db.conn.execute(
                    "UPDATE tracks SET artist=? WHERE id=?", (current_artist, track_id)
                )
                db.conn.execute(
                    "UPDATE track_metadata_fields SET value=? "
                    "WHERE track_id=? AND field_name='artist'",
                    (current_artist, track_id),
                )
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    return track_id


def _discogs(*, reverse: bool, score: float = 96, duration: float = 240):
    title = "Anthem of the Republic" if reverse else "The Cosmic Assembly"
    artist = "The Cosmic Assembly" if reverse else "Anthem of the Republic"
    return ProviderReleaseCandidate(
        provider="discogs",
        title=title,
        artist=artist,
        artist_credits=(
            ProviderArtistCredit(
                artist,
                artist_id="synthetic-artist",
                entity_type="group",
            ),
        ),
        album="Synthetic Catalogue Album",
        album_artist=artist,
        original_release_date="1978",
        version_type="studio",
        duration_seconds=duration,
        provider_score=score,
        release_id="synthetic-release",
        master_id="synthetic-master",
        reasons=("exact_tracklist_title", "exact_artist_credit"),
        field_scores={
            "title": score,
            "artist": score,
            "artist_credits": score,
            "album": score,
            "album_artist": score,
            "original_release_date": score,
            "version_type": score,
        },
    )


def _item(db: MusicVaultDB, track_id: int):
    return db.conn.execute(
        "SELECT * FROM metadata_intelligence_items WHERE track_id=?",
        (track_id,),
    ).fetchone()


def test_incomplete_orientation_evidence_cannot_terminalize_dash_source_fallback():
    decision = classify_stored_review_evidence(
        parsed_hints={
            "title": "Synthetic Anthem",
            "artist": "Synthetic Ensemble",
            "pattern": "artist_dash_title",
            "orientation": {
                "evaluated_count": 2,
                "provider_confirmed": False,
                "requires_provider_adjudication": True,
            },
        },
        field_proposal={
            "_current": {
                "title": "Synthetic Anthem",
                "artist": "Synthetic Ensemble",
            },
            "_discogs": {},
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={},
        provider_agreement="none",
        review_reason="youtube_exclusive",
    )

    assert decision.outcome is not ReviewOutcome.SOURCE_FALLBACK


def test_conclusive_first_discogs_orientation_short_circuits_second_search(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        raw_title="The Cosmic Assembly - Anthem of the Republic (1978)",
    )
    discogs = _SequencedDiscogs((_discogs(reverse=True),), ())
    service = MetadataIntelligenceService(
        db,
        _settings(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
    )

    result = service.process_automatic_queue()
    evidence = json.loads(_item(db, track_id)["field_proposal"])["_orientation"]

    assert result.applied == 1
    assert len(discogs.calls) == 1
    assert evidence["selected"] == "left_is_artist"
    assert evidence["discogs_queries"] == 1
    assert evidence["provider_confirmed"] is True
    db.close()


def test_orientation_selected_candidate_wins_over_higher_score_identity_mismatch(
    tmp_path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        raw_title="The Cosmic Assembly - Anthem of the Republic (1978)",
    )
    mismatch = _discogs(reverse=False, score=99)
    coherent = _discogs(reverse=True, score=92)
    discogs = _SequencedDiscogs((mismatch, coherent), ())
    service = MetadataIntelligenceService(
        db,
        _settings(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
    )

    service.process_automatic_queue()
    track = db.get_track(track_id)

    assert len(discogs.calls) == 1
    assert track["title"] == coherent.title
    assert track["artist"] == coherent.artist
    db.close()


def test_weak_first_discogs_orientation_triggers_reverse_and_provider_overrides_parser(
    tmp_path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    weak = _discogs(reverse=False, score=58)
    reverse = _discogs(reverse=True)
    discogs = _SequencedDiscogs((weak,), (reverse,))
    service = MetadataIntelligenceService(
        db,
        _settings(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
    )

    result = service.process_automatic_queue()
    track = db.get_track(track_id)
    row = _item(db, track_id)
    parsed = json.loads(row["parsed_hints"])
    evidence = json.loads(row["field_proposal"])["_orientation"]

    assert result.applied == 1
    assert len(discogs.calls) == 2
    assert track["title"] == "Anthem of the Republic"
    assert track["artist"] == "The Cosmic Assembly"
    assert track["original_release_date"] == "1978"
    assert parsed["raw_title"] == "Anthem of the Republic - The Cosmic Assembly (1978)"
    assert evidence["selected"] == "right_is_artist"
    assert evidence["evaluated_count"] == 2
    assert evidence["discogs_queries"] == 2
    assert row["state"] in {"applied", "applied_with_gaps"}
    db.close()


def test_duration_conflict_rejects_first_orientation_and_keeps_requests_bounded(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    _track(db, tmp_path)
    discogs = _SequencedDiscogs(
        (_discogs(reverse=False, duration=900),),
        (_discogs(reverse=True),),
    )
    service = MetadataIntelligenceService(
        db,
        _settings(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
    )

    result = service.process_automatic_queue()

    assert result.applied == 1
    assert len(discogs.calls) == 2
    db.close()


def test_discogs_remains_primary_over_conflicting_musicbrainz(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        raw_title="The Cosmic Assembly - Anthem of the Republic (1978)",
    )
    discogs = _SequencedDiscogs((_discogs(reverse=True),))
    musicbrainz = _MusicBrainz((_discogs(reverse=False),))
    service = MetadataIntelligenceService(
        db,
        _settings(metadata_musicbrainz_secondary_enabled=True),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
    )

    service.process_automatic_queue()
    track = db.get_track(track_id)
    evidence = json.loads(_item(db, track_id)["field_proposal"])["_orientation"]

    assert len(discogs.calls) == 1
    assert len(musicbrainz.calls) == 1
    assert track["title"] == "Anthem of the Republic"
    assert track["artist"] == "The Cosmic Assembly"
    assert evidence["selected"] == "left_is_artist"
    assert evidence["musicbrainz_queries"] == 1
    db.close()


def test_single_musicbrainz_fallback_can_confirm_reverse_after_discogs_no_match(
    tmp_path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    discogs = _SequencedDiscogs((), ())
    musicbrainz = _MusicBrainz((_discogs(reverse=True, score=97),))
    service = MetadataIntelligenceService(
        db,
        _settings(metadata_musicbrainz_secondary_enabled=True),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
    )

    service.process_automatic_queue()
    track = db.get_track(track_id)
    evidence = json.loads(_item(db, track_id)["field_proposal"])["_orientation"]

    assert len(discogs.calls) == 2
    assert len(musicbrainz.calls) == 1
    assert track["title"] == "Anthem of the Republic"
    assert track["artist"] == "The Cosmic Assembly"
    assert evidence["selected"] == "right_is_artist"
    assert "musicbrainz_fallback_orientation" in evidence["reasons"]
    db.close()


def test_no_provider_fallback_records_both_local_orientations_and_remains_eligible(
    tmp_path,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(
        db,
        tmp_path,
        current_title="The Cosmic Assembly",
        current_artist="Anthem of the Republic",
    )
    service = MetadataIntelligenceService(
        db,
        _settings(
            metadata_discogs_enabled=False,
            metadata_discogs_consent_version=0,
        ),
    )

    result = service.process_automatic_queue()
    row = _item(db, track_id)
    evidence = json.loads(row["field_proposal"])["_orientation"]

    assert result.source_fallback == 1
    assert result.review == 0
    assert row["state"] == "source_fallback"
    assert evidence["evaluated_count"] == 2
    assert evidence["selected"] == "left_is_artist"
    assert evidence["requires_provider_adjudication"] is True
    assert "preserved_current_orientation" in evidence["reasons"]
    db.close()


def test_unique_independently_supported_local_artist_selects_reverse_offline(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    supporting_path = tmp_path / "supporting.mp3"
    supporting_path.write_bytes(b"synthetic supporting artist identity")
    db.upsert_track(
        supporting_path,
        title="Different Synthetic Song",
        artist="The Cosmic Assembly",
        source_kind="embedded",
    )
    track_id = _track(
        db,
        tmp_path,
        current_title="The Cosmic Assembly",
        current_artist="Anthem of the Republic",
        index=2,
    )
    service = MetadataIntelligenceService(
        db,
        _settings(
            metadata_discogs_enabled=False,
            metadata_discogs_consent_version=0,
        ),
    )

    result = service.process_automatic_queue()
    track = db.get_track(track_id)
    evidence = json.loads(_item(db, track_id)["field_proposal"])["_orientation"]

    assert result.source_fallback == 1
    assert track["title"] == "Anthem of the Republic"
    assert track["artist"] == "The Cosmic Assembly"
    assert evidence["selected"] == "right_is_artist"
    assert "unique_local_artist_identity" in evidence["reasons"]
    assert evidence["requires_provider_adjudication"] is False
    db.close()


def test_both_locally_supported_sides_do_not_auto_swap_without_provider(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    for index, artist in enumerate(
        ("Anthem of the Republic", "The Cosmic Assembly"), start=20
    ):
        path = tmp_path / f"supporting-{index}.mp3"
        path.write_bytes(b"synthetic ambiguous local artist identity")
        db.upsert_track(
            path,
            title=f"Different Synthetic Song {index}",
            artist=artist,
            source_kind="embedded",
        )
    track_id = _track(
        db,
        tmp_path,
        current_title="The Cosmic Assembly",
        current_artist="Anthem of the Republic",
        index=3,
    )
    service = MetadataIntelligenceService(
        db,
        _settings(
            metadata_discogs_enabled=False,
            metadata_discogs_consent_version=0,
        ),
    )

    service.process_automatic_queue()
    track = db.get_track(track_id)
    evidence = json.loads(_item(db, track_id)["field_proposal"])["_orientation"]

    assert track["title"] == "The Cosmic Assembly"
    assert track["artist"] == "Anthem of the Republic"
    assert evidence["selected"] == "left_is_artist"
    assert evidence["requires_provider_adjudication"] is True
    assert "unique_local_artist_identity" not in evidence["reasons"]
    db.close()


def test_manual_title_and_artist_locks_block_provider_identity_changes(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    MetadataService(db).apply_actions(
        track_id,
        {
            "title": MetadataAction.set("User Title"),
            "artist": MetadataAction.set("User Artist"),
        },
    )
    credits_before = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT artist_id,role,credit_order,join_phrase,provenance,is_manual,is_locked "
            "FROM track_artist_credits WHERE track_id=? ORDER BY credit_order,id",
            (track_id,),
        )
    ]
    discogs = _SequencedDiscogs((), (_discogs(reverse=True),))
    service = MetadataIntelligenceService(
        db,
        _settings(),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
    )

    service.process_automatic_queue()
    track = db.get_track(track_id)

    assert track["title"] == "User Title"
    assert track["artist"] == "User Artist"
    credits_after = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT artist_id,role,credit_order,join_phrase,provenance,is_manual,is_locked "
            "FROM track_artist_credits WHERE track_id=? ORDER BY credit_order,id",
            (track_id,),
        )
    ]
    assert credits_after == credits_before
    db.close()


def test_provider_request_counts_never_exceed_two_discogs_and_one_musicbrainz(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _track(db, tmp_path)
    discogs = _SequencedDiscogs((), ())
    musicbrainz = _MusicBrainz(())
    service = MetadataIntelligenceService(
        db,
        _settings(metadata_musicbrainz_secondary_enabled=True),
        token_store=_TokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
    )

    service.process_automatic_queue()
    evidence = json.loads(_item(db, track_id)["field_proposal"])["_orientation"]

    assert len(discogs.calls) == 2
    assert len(musicbrainz.calls) == 1
    assert evidence["discogs_queries"] == 2
    assert evidence["musicbrainz_queries"] == 1
    db.close()
