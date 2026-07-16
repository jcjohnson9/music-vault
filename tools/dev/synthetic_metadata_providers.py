from __future__ import annotations

"""Deterministic, offline-only providers for Batch 10.1 acceptance tools.

This module lives below ``tools/dev`` deliberately.  Production code and the
PyInstaller specification never import it.  The providers return only Music
Vault's normalized candidate types; they contain no copied provider payloads,
credentials, network client, or copyrighted names/artwork.
"""

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.providers import (
    ProviderArtistCredit,
    ProviderArtworkCandidate,
    ProviderQuery,
    ProviderReleaseCandidate,
)


class SyntheticProviderRateLimit(RuntimeError):
    pass


class SyntheticProviderTemporaryFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyntheticScenario:
    key: str
    lookup_title: str
    source_artist: str
    canonical_artist: str
    outcome: str = "agreement"
    version_type: str = "studio"
    featured_artist: str | None = None
    collaborator: str | None = None
    entity_type: str = "person"
    artwork: bool = False
    preserve_artwork: bool = False
    unofficial: bool = False

    @property
    def source_title(self) -> str:
        return f"{self.source_artist} - {self.lookup_title}"


# The matrix mirrors the eighteen required Batch 10.1 synthetic scenarios.
# Values are invented test fixtures and are safe to expose in test failures.
SYNTHETIC_SCENARIOS: tuple[SyntheticScenario, ...] = (
    SyntheticScenario("older_random_uploader", "Amber Circuit", "Random Archive", "Aster Vale"),
    SyntheticScenario("older_many_reissues", "Copper Horizon", "Collector Channel", "Mira North"),
    SyntheticScenario("record_label_uploader", "Glass Meridian", "Synthetic Records", "Lowland Unit", entity_type="group"),
    SyntheticScenario("group_duo", "Binary Lantern", "Unofficial Uploads", "The Parallel Duo", entity_type="duo"),
    SyntheticScenario("primary_featured", "Cloud Geometry", "Fan Channel", "Sable Current", featured_artist="Guest Signal"),
    SyntheticScenario("joint_collaboration", "Shared Orbit", "Archive Channel", "Delta Form", collaborator="Juniper Assembly"),
    SyntheticScenario("studio_version", "Static Bloom", "Video Archive", "Violet Engine"),
    SyntheticScenario("unofficial_live", "Static Bloom Live Hall", "Audience Capture", "Violet Engine", version_type="live", unofficial=True),
    SyntheticScenario("remix_conflict", "Paper Satellites Remix", "Mix Archive", "Quiet Metric", outcome="disagreement", version_type="remix"),
    SyntheticScenario("youtube_exclusive", "Neon Notebook Session", "Independent Channel", "Independent Channel", outcome="no_match", version_type="youtube_exclusive"),
    SyntheticScenario("provider_agreement", "Frosted Relay", "Loose Upload", "North Relay"),
    SyntheticScenario("provider_disagreement", "Velvet Transit", "Loose Upload", "Velvet Transit Unit", outcome="disagreement"),
    SyntheticScenario("missing_artwork", "Prism Harbor", "Loose Upload", "Prism Harbor Group", artwork=True, entity_type="group"),
    SyntheticScenario("preserve_valid_artwork", "Stone Frequency", "Loose Upload", "Stone Frequency", artwork=True, preserve_artwork=True, entity_type="group"),
    SyntheticScenario("ambiguous_reissue", "Ivory Switch", "Archive Upload", "Ivory Switch Artist", outcome="ambiguous"),
    SyntheticScenario("rate_limit", "Rate Limit Probe", "Test Harness", "Rate Limit Artist", outcome="rate_limit"),
    SyntheticScenario("temporary_failure", "Temporary Failure Probe", "Test Harness", "Retry Artist", outcome="temporary_failure"),
    SyntheticScenario("file_write_rollback", "Verified Write Probe", "Test Harness", "Writeback Artist"),
)


def scenario_by_title(title: object) -> SyntheticScenario | None:
    normalized = str(title or "").strip().casefold()
    return next(
        (item for item in SYNTHETIC_SCENARIOS if item.lookup_title.casefold() == normalized),
        None,
    )


def _identity(value: str, *, offset: int = 0) -> str:
    number = int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:12], 16)
    return str(100_000 + offset + number % 800_000_000)


def _credits(
    artist: str,
    *,
    scenario: SyntheticScenario | None,
) -> tuple[ProviderArtistCredit, ...]:
    if scenario is None:
        return (
            ProviderArtistCredit(
                artist,
                role="primary",
                artist_id=_identity(artist, offset=1),
                entity_type="group",
            ),
        )
    values: list[ProviderArtistCredit] = [
        ProviderArtistCredit(
            artist,
            role="primary",
            artist_id=_identity(artist, offset=1),
            join_phrase=" feat. " if scenario.featured_artist else (
                " & " if scenario.collaborator else ""
            ),
            entity_type=scenario.entity_type,
        )
    ]
    if scenario.featured_artist:
        values.append(
            ProviderArtistCredit(
                scenario.featured_artist,
                role="featured",
                artist_id=_identity(scenario.featured_artist, offset=2),
                entity_type="person",
            )
        )
    if scenario.collaborator:
        values.append(
            ProviderArtistCredit(
                scenario.collaborator,
                role="collaborator",
                artist_id=_identity(scenario.collaborator, offset=3),
                entity_type="group",
            )
        )
    return tuple(values)


def discogs_candidate(query: ProviderQuery) -> ProviderReleaseCandidate | None:
    scenario = scenario_by_title(query.title)
    if scenario is not None and scenario.outcome == "no_match":
        return None
    artist = scenario.canonical_artist if scenario else (query.artist or "Synthetic Scale Unit")
    title = query.title
    release_id = _identity(f"release:{artist}:{title}")
    master_id = _identity(f"master:{artist}:{title}")
    score = 78.0 if scenario is not None and scenario.outcome == "ambiguous" else 98.0
    reasons = ("release_ambiguous",) if score < 85 else ()
    artwork = None
    if scenario is not None and scenario.artwork:
        artwork = ProviderArtworkCandidate(
            source_url=f"https://i.discogs.com/synthetic/{release_id}.png",
            provider_page_url=f"https://www.discogs.com/release/{release_id}",
            release_id=release_id,
            width=64,
            height=64,
        )
    return ProviderReleaseCandidate(
        provider="discogs",
        title=title,
        artist=artist,
        artist_credits=_credits(artist, scenario=scenario),
        album=f"Synthetic Release {release_id[-6:]}",
        album_artist=artist,
        release_date="1987",
        original_release_date="1984",
        version_type=scenario.version_type if scenario else "studio",
        version_label=(
            "Synthetic Hall Audience Recording"
            if scenario is not None and scenario.version_type == "live"
            else None
        ),
        duration_seconds=query.duration_seconds,
        provider_score=score,
        release_id=release_id,
        master_id=master_id,
        track_position="A1",
        label="Synthetic Catalogue Label",
        country="US",
        release_format="Synthetic",
        provider_reference=f"https://www.discogs.com/release/{release_id}",
        artwork=artwork,
        is_official=not bool(scenario and scenario.unofficial),
        reasons=reasons,
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
                "version_label",
                "discogs_release_id",
                "discogs_master_id",
                "discogs_track_position",
            )
        },
    )


class SyntheticTokenStore:
    """In-memory acceptance credential; no secret file is created or read."""

    def read(self) -> str:
        return "offline-acceptance-placeholder"

    def configured(self) -> bool:
        return True


class SyntheticDiscogsProvider:
    def __init__(self, *, latency_seconds: float = 0.0) -> None:
        self.latency_seconds = max(0.0, float(latency_seconds))
        self.calls: list[ProviderQuery] = []
        self._lock = threading.Lock()
        self.maximum_parallel_calls = 0
        self._active_calls = 0

    def search(
        self,
        query: ProviderQuery,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Sequence[ProviderReleaseCandidate]:
        scenario = scenario_by_title(query.title)
        if cancel_event is not None and cancel_event.is_set():
            return ()
        with self._lock:
            self.calls.append(query)
            self._active_calls += 1
            self.maximum_parallel_calls = max(self.maximum_parallel_calls, self._active_calls)
        try:
            if self.latency_seconds:
                time.sleep(self.latency_seconds)
            if scenario is not None and scenario.outcome == "rate_limit":
                raise SyntheticProviderRateLimit("synthetic_rate_limited")
            if scenario is not None and scenario.outcome == "temporary_failure":
                raise SyntheticProviderTemporaryFailure("synthetic_provider_unavailable")
            candidate = discogs_candidate(query)
            return (candidate,) if candidate is not None else ()
        finally:
            with self._lock:
                self._active_calls -= 1


class SyntheticMusicBrainzProvider:
    def __init__(self, *, latency_seconds: float = 0.0) -> None:
        self.latency_seconds = max(0.0, float(latency_seconds))
        self.calls: list[tuple[str, str | None]] = []
        self._lock = threading.Lock()

    def search(
        self,
        title: str,
        artist: str | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Sequence[MetadataCandidate]:
        scenario = scenario_by_title(title)
        if cancel_event is not None and cancel_event.is_set():
            return ()
        with self._lock:
            self.calls.append((title, artist))
        if self.latency_seconds:
            time.sleep(self.latency_seconds)
        if scenario is not None and scenario.outcome in {
            "no_match",
            "rate_limit",
            "temporary_failure",
        }:
            return ()
        canonical_artist = scenario.canonical_artist if scenario else (artist or "Synthetic Scale Unit")
        candidate_title = title
        if scenario is not None and scenario.outcome == "disagreement":
            candidate_title = f"{title} Alternate"
            canonical_artist = f"{canonical_artist} Alternate"
        matching_discogs = discogs_candidate(
            ProviderQuery(title=title, artist=artist)
        )
        return (
            MetadataCandidate(
                title=candidate_title,
                artist=canonical_artist,
                album=(
                    matching_discogs.album
                    if matching_discogs is not None
                    else f"Synthetic Release {_identity(title)[-6:]}"
                ),
                release_date="1987",
                recording_id=f"mb-recording-{_identity(title)}",
                release_id=f"mb-release-{_identity(title, offset=9)}",
                score=96,
                duration_seconds=None,
                album_artist=canonical_artist,
                country="US",
            ),
        )


def scenario_keys() -> tuple[str, ...]:
    return tuple(item.key for item in SYNTHETIC_SCENARIOS)


__all__ = [
    "SYNTHETIC_SCENARIOS",
    "SyntheticDiscogsProvider",
    "SyntheticMusicBrainzProvider",
    "SyntheticProviderRateLimit",
    "SyntheticProviderTemporaryFailure",
    "SyntheticScenario",
    "SyntheticTokenStore",
    "discogs_candidate",
    "scenario_by_title",
    "scenario_keys",
]
