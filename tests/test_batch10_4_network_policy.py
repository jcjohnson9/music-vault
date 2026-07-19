from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core import app_status
from music_vault.core.runtime_policy import (
    NO_NETWORK_ENVIRONMENT,
    NO_SECRETS_ENVIRONMENT,
)
from music_vault.core.youtube_sync import (
    AuthorizedYouTubePlaylistSyncer,
    YouTubeSyncConfig,
)
from music_vault.lyrics.models import LyricsQuery, LyricsStatus, TrackLyricsIdentity
from music_vault.lyrics.providers import lrclib
from music_vault.metadata import artwork, discogs_artwork, musicbrainz_enricher
from music_vault.metadata import artist_images
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_settings import DiscogsTokenStore
from music_vault.metadata.providers import ProviderArtworkCandidate, discogs


def _deny_session(*_args, **_kwargs):
    raise AssertionError("network transport factory must not be invoked")


def _deny_resolver(*_args, **_kwargs):
    raise AssertionError("DNS resolver must not be invoked")


def test_no_secret_mode_never_opens_discogs_token(monkeypatch, tmp_path: Path) -> None:
    token = tmp_path / "credential.txt"
    token.write_text("synthetic-test-value\n", encoding="utf-8")
    monkeypatch.setenv(NO_SECRETS_ENVIRONMENT, "1")

    def deny_read(*_args, **_kwargs):
        raise AssertionError("credential file content must not be read")

    monkeypatch.setattr(Path, "read_text", deny_read)
    assert DiscogsTokenStore(token).read() == ""
    assert DiscogsTokenStore(token).configured() is False


def test_no_secret_status_readiness_never_opens_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(NO_SECRETS_ENVIRONMENT, "1")
    monkeypatch.setattr(app_status, "youtube_api_key_path", lambda: tmp_path / "api.txt")
    monkeypatch.setattr(app_status, "discogs_token_path", lambda: tmp_path / "token.txt")

    def deny_read(*_args, **_kwargs):
        raise AssertionError("credential file content must not be read")

    monkeypatch.setattr(Path, "read_text", deny_read)
    assert app_status._api_ready() is False
    assert app_status._discogs_ready() is False


def test_no_network_defers_every_transport_factory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(NO_NETWORK_ENVIRONMENT, "1")
    monkeypatch.setattr(musicbrainz_enricher.requests, "Session", _deny_session)
    monkeypatch.setattr(discogs.requests, "Session", _deny_session)
    monkeypatch.setattr(lrclib.requests, "Session", _deny_session)
    monkeypatch.setattr(artwork.requests, "Session", _deny_session)
    monkeypatch.setattr(discogs_artwork.requests, "Session", _deny_session)
    monkeypatch.setattr(artist_images.requests, "Session", _deny_session)

    musicbrainz = musicbrainz_enricher.MusicBrainzProvider(resolver=_deny_resolver)
    discogs_provider = discogs.DiscogsProvider(
        token="synthetic-token", resolver=_deny_resolver
    )
    lyrics = lrclib.LRCLIBProvider(
        lrclib.SafeLyricsTransport(resolver=_deny_resolver)
    )
    cover_provider = artwork.CoverArtArchiveProvider(resolver=_deny_resolver)
    private_cover_cache = discogs_artwork.DiscogsArtworkCache(
        tmp_path / "covers", resolver=_deny_resolver
    )
    portrait_transport = artist_images.SafeArtistImageTransport(
        resolver=_deny_resolver
    )

    with pytest.raises(musicbrainz_enricher.MetadataProviderError, match="deferred"):
        musicbrainz.search("Synthetic Track", "Synthetic Artist")
    with pytest.raises(discogs.DiscogsProviderError, match="deferred"):
        discogs_provider.test_connection()
    result = lyrics.lookup(
        LyricsQuery(TrackLyricsIdentity(1, "Synthetic Track", "Synthetic Artist"))
    )
    assert result.status is LyricsStatus.TEMPORARY_ERROR
    with pytest.raises(artwork.ArtworkError, match="deferred"):
        cover_provider.fetch("00000000-0000-4000-8000-000000000001")
    with pytest.raises(discogs_artwork.DiscogsArtworkError, match="deferred"):
        private_cover_cache.fetch_for_gap(
            ProviderArtworkCandidate(
                source_url="https://i.discogs.com/synthetic.jpg",
                provider_page_url="https://www.discogs.com/release/1",
                release_id="1",
            ),
            accepted_release_id="1",
            provider_score=100.0,
            current_cover_path=None,
        )
    with pytest.raises(artist_images.ArtistImageUnavailableError, match="deferred"):
        portrait_transport.get_json("https://musicbrainz.org/ws/2/artist")
    assert portrait_transport.session is None


@pytest.mark.parametrize(
    "blocked_environment",
    (NO_NETWORK_ENVIRONMENT, NO_SECRETS_ENVIRONMENT),
)
def test_youtube_sync_stops_before_secret_or_runtime_file_access(
    monkeypatch,
    tmp_path: Path,
    blocked_environment: str,
) -> None:
    monkeypatch.setenv(blocked_environment, "1")
    output = tmp_path / "downloads"
    archive = tmp_path / "archive.txt"
    with pytest.raises(RuntimeError, match="deferred"):
        AuthorizedYouTubePlaylistSyncer(
            YouTubeSyncConfig(
                playlist_url="https://www.youtube.com/playlist?list=synthetic",
                output_dir=output,
                archive_file=archive,
            )
        )
    assert not output.exists()
    assert not archive.exists()


def test_metadata_queue_defers_without_database_or_factory_activity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(NO_NETWORK_ENVIRONMENT, "1")
    calls: list[str] = []
    database = SimpleNamespace(
        db_path=tmp_path / "unopened.sqlite3",
        backup_dir=tmp_path / "backups",
        migration_performed=False,
    )
    service = MetadataIntelligenceService(
        database,
        {"metadata_intelligence_enabled": True},
        discogs_provider_factory=lambda _token: calls.append("discogs"),
        musicbrainz_provider_factory=lambda: calls.append("musicbrainz"),
    )

    result = service.process_automatic_queue()
    assert result.processed == 0
    assert calls == []
    assert not database.db_path.exists()


def test_app_status_exports_only_safe_provider_deferral_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(NO_NETWORK_ENVIRONMENT, "1")
    monkeypatch.setenv(NO_SECRETS_ENVIRONMENT, "1")
    data = tmp_path / "data"
    status = data / "music_vault_status.json"
    monkeypatch.setattr(app_status, "project_root", lambda: tmp_path)
    monkeypatch.setattr(app_status, "data_dir", lambda: data)
    monkeypatch.setattr(app_status, "app_status_path", lambda: status)
    monkeypatch.setattr(app_status, "config_path", lambda: data / "config.json")
    monkeypatch.setattr(app_status, "default_downloads_dir", lambda: data / "downloads")
    monkeypatch.setattr(app_status, "path_resolution_source", lambda: "synthetic")
    monkeypatch.setattr(app_status, "_ffmpeg_ready", lambda _config=None: False)
    database = SimpleNamespace(
        db_path=data / "database.sqlite3",
        conn=SimpleNamespace(execute=lambda *_args, **_kwargs: None),
        migration_performed=False,
    )

    app_status.write_app_status(database, {})
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["health"]["api_ready"] is False
    assert payload["discogs_ready"] is False
    assert payload["provider_work_deferred"] is True
    assert payload["provider_work_defer_reason"] == "acceptance_no_network"
    assert set(payload) >= {
        "provider_work_deferred",
        "provider_work_defer_reason",
    }
