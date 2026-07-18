from __future__ import annotations

from pathlib import Path

from music_vault.core.db import MusicVaultDB
from music_vault.core.importer import ImportSourceContext, import_file
from music_vault.core.library_browser import (
    ArtistKey,
    artist_credit_is_browser_visible,
    query_artist_summaries,
    query_artist_track_sections,
)
from music_vault.metadata.artist_credits import (
    ArtistCreditInput,
    ArtistCreditService,
)
from music_vault.metadata.service import MetadataService


def _import_youtube_track(
    db: MusicVaultDB,
    root: Path,
    monkeypatch,
    *,
    filename: str,
    artist: str,
    video_id: str,
) -> int:
    from music_vault.core import importer

    media = root / filename
    media.write_bytes(b"synthetic-test-audio")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: {
            "title": f"Upload {filename}",
            "artist": artist,
            "album": None,
            "album_artist": None,
            "release_date": "20260717",
            "year": "20260717",
            "duration_seconds": 60.0,
            "title_provenance": "embedded",
        },
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    assert import_file(
        db,
        media,
        ImportSourceContext("youtube", video_id, "20260717"),
    )
    return int(
        db.conn.execute(
            "SELECT id FROM tracks WHERE path=?",
            (str(media.resolve()),),
        ).fetchone()[0]
    )


def _summary_names(db: MusicVaultDB) -> set[str]:
    return {summary.display_name for summary in query_artist_summaries(db.conn)}


def _credit_provenance(db: MusicVaultDB, track_id: int) -> str:
    row = db.conn.execute(
        "SELECT provenance FROM track_artist_credits WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_real_youtube_import_hides_uncorrected_channel_and_label_artists(
    tmp_path: Path,
    monkeypatch,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    try:
        channel_id = _import_youtube_track(
            db,
            tmp_path,
            monkeypatch,
            filename="channel.mp3",
            artist="Synthetic Upload Channel",
            video_id="channel00001",
        )
        label_id = _import_youtube_track(
            db,
            tmp_path,
            monkeypatch,
            filename="label.mp3",
            artist="Synthetic Catalogue Label",
            video_id="label000001",
        )
        blank_id = db.upsert_track(
            tmp_path / "blank.synthetic-audio",
            title="Blank Artist",
            artist=None,
        )

        assert _credit_provenance(db, channel_id) == "youtube"
        assert _credit_provenance(db, label_id) == "youtube"
        assert db.conn.execute(
            "SELECT provenance FROM track_metadata_fields "
            "WHERE track_id=? AND field_name='artist'",
            (channel_id,),
        ).fetchone()[0] == "youtube"

        names = _summary_names(db)
        assert "Synthetic Upload Channel" not in names
        assert "Synthetic Catalogue Label" not in names
        assert "Unknown Artist" in names
        unknown = next(
            summary
            for summary in query_artist_summaries(db.conn)
            if summary.key.normalized_name == ""
        )
        assert unknown.track_count == 1
        assert [
            int(row["id"])
            for row in query_artist_track_sections(db.conn, ArtistKey("")).tracks
        ] == [blank_id]

        assert query_artist_track_sections(
            db.conn,
            ArtistKey("synthetic upload channel"),
        ).tracks == ()
    finally:
        db.close()


def test_accepted_parsed_provider_and_manual_credits_become_visible(
    tmp_path: Path,
    monkeypatch,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    try:
        parsed_id = _import_youtube_track(
            db,
            tmp_path,
            monkeypatch,
            filename="parsed.mp3",
            artist="Synthetic Source Channel",
            video_id="parsed00001",
        )
        provider_id = _import_youtube_track(
            db,
            tmp_path,
            monkeypatch,
            filename="provider.mp3",
            artist="Synthetic Release Label",
            video_id="provider001",
        )
        manual_id = _import_youtube_track(
            db,
            tmp_path,
            monkeypatch,
            filename="manual.mp3",
            artist="Synthetic Manual Channel",
            video_id="manual00001",
        )

        # The accepted source-fallback path applies parsed title identity as a
        # normal source observation.  MetadataService must replace the hidden
        # importer credit with the accepted parsed provenance.
        result = MetadataService(db).record_source_observations(
            parsed_id,
            provider="youtube_title_parsed",
            values={"artist": "Parsed Performer"},
            confidence=86.0,
            reason="synthetic_source_fallback",
        )
        assert result.changed_fields == frozenset({"artist"})
        assert _credit_provenance(db, parsed_id) == "youtube_title_parsed"

        ArtistCreditService(db).replace_track_credits(
            provider_id,
            (
                ArtistCreditInput(
                    "Provider Performer",
                    discogs_artist_id="synthetic-artist-101",
                ),
            ),
            provenance="discogs_high_confidence",
            provider_reference="synthetic-release-202",
            confidence=98.0,
        )
        ArtistCreditService(db).replace_track_credits(
            manual_id,
            (ArtistCreditInput("Manual Performer"),),
            provenance="manual",
            confidence=100.0,
            is_manual=True,
            is_locked=True,
            actor="user",
            reason="synthetic_manual_artist_correction",
        )

        summaries = {
            summary.display_name: summary
            for summary in query_artist_summaries(db.conn)
        }
        assert summaries["Parsed Performer"].track_count == 1
        assert summaries["Provider Performer"].track_count == 1
        assert summaries["Manual Performer"].track_count == 1
        assert "Synthetic Source Channel" not in summaries
        assert "Synthetic Release Label" not in summaries
        assert "Synthetic Manual Channel" not in summaries
        assert [
            int(row["id"])
            for row in query_artist_track_sections(
                db.conn,
                summaries["Parsed Performer"].key,
            ).tracks
        ] == [parsed_id]
    finally:
        db.close()


def test_artist_credit_visibility_is_explicit_and_manual_authority_wins():
    assert not artist_credit_is_browser_visible("youtube")
    assert not artist_credit_is_browser_visible("youtube_uploader_fallback")
    assert not artist_credit_is_browser_visible("youtube_release_company")
    assert artist_credit_is_browser_visible("youtube_title_parsed")
    assert artist_credit_is_browser_visible("discogs_high_confidence")
    assert artist_credit_is_browser_visible("manual")
    assert artist_credit_is_browser_visible("youtube", is_manual=True)
    assert artist_credit_is_browser_visible("youtube", is_locked=True)
