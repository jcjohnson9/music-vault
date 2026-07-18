"""Typed, provider-neutral metadata candidate contracts.

Provider payloads are deliberately reduced to these accepted candidate fields.
Raw service responses are never part of the persistent Music Vault model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderQuery:
    """A bounded metadata lookup request without source secrets."""

    title: str
    artist: str | None = None
    album: str | None = None
    duration_seconds: float | None = None
    version_type: str | None = None
    version_label: str | None = None
    year_hint: int | None = None


@dataclass(frozen=True)
class ProviderArtistCredit:
    """Structured musical credit supplied by a catalogue provider."""

    name: str
    role: str = "primary"
    artist_id: str | None = None
    join_phrase: str = ""
    entity_type: str = "unknown"
    provider_reference: str | None = None


@dataclass(frozen=True)
class ProviderArtworkCandidate:
    """A release-catalogue front-image candidate, never embedded automatically."""

    source_url: str
    provider_page_url: str
    release_id: str
    image_type: str = "front"
    width: int | None = None
    height: int | None = None
    catalogue_image: bool = True

    @property
    def is_front(self) -> bool:
        return self.image_type.casefold() in {"front", "primary"}


@dataclass(frozen=True)
class ProviderReleaseCandidate:
    """Normalized metadata candidate safe for long-term accepted-field storage."""

    provider: str
    title: str
    artist: str
    artist_credits: tuple[ProviderArtistCredit, ...] = ()
    album: str | None = None
    album_artist: str | None = None
    release_date: str | None = None
    original_release_date: str | None = None
    version_type: str = "unknown"
    version_label: str | None = None
    duration_seconds: float | None = None
    provider_score: float = 0.0
    release_id: str | None = None
    master_id: str | None = None
    # A provider-qualified, stable family identity (for example,
    # ``catalogue:family-id``).  This is intentionally distinct from an
    # edition-specific release ID and is used only when the provider has
    # explicitly supplied release-family evidence.
    release_family_id: str | None = None
    track_position: str | None = None
    recording_id: str | None = None
    label: str | None = None
    country: str | None = None
    release_format: str | None = None
    provider_reference: str | None = None
    artwork: ProviderArtworkCandidate | None = None
    is_compilation: bool = False
    is_official: bool = True
    provider_order: int = 0
    reasons: tuple[str, ...] = ()
    field_scores: dict[str, float] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """Compatibility with the existing provider-candidate convention."""

        return self.provider_score

    def accepted_metadata(self) -> dict[str, Any]:
        """Return normalized catalogue facts only; raw response data is absent."""

        return {
            "provider": self.provider,
            "title": self.title,
            "artist": self.artist,
            "artist_credits": [
                {
                    "name": credit.name,
                    "role": credit.role,
                    "artist_id": credit.artist_id,
                    "join_phrase": credit.join_phrase,
                    "entity_type": credit.entity_type,
                    "provider_reference": credit.provider_reference,
                }
                for credit in self.artist_credits
            ],
            "album": self.album,
            "album_artist": self.album_artist,
            "release_date": self.release_date,
            "original_release_date": self.original_release_date,
            "version_type": self.version_type,
            "version_label": self.version_label,
            "duration_seconds": self.duration_seconds,
            "provider_score": self.provider_score,
            "release_id": self.release_id,
            "master_id": self.master_id,
            "release_family_id": self.release_family_id,
            "track_position": self.track_position,
            "recording_id": self.recording_id,
            "label": self.label,
            "country": self.country,
            "release_format": self.release_format,
            "provider_reference": self.provider_reference,
            "is_compilation": self.is_compilation,
            "is_official": self.is_official,
            "provider_order": self.provider_order,
            "reasons": list(self.reasons),
            "field_scores": dict(self.field_scores),
        }


__all__ = [
    "ProviderArtistCredit",
    "ProviderArtworkCandidate",
    "ProviderQuery",
    "ProviderReleaseCandidate",
]
