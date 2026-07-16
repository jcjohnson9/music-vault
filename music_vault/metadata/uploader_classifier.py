"""Local-only YouTube uploader classification and final-fallback policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .matching import normalize_for_comparison, text_similarity


class UploaderClass(str, Enum):
    LIKELY_OFFICIAL_ARTIST = "likely_official_artist"
    LIKELY_LABEL = "likely_label"
    LIKELY_DISTRIBUTOR = "likely_distributor"
    LIKELY_TOPIC = "likely_topic_auto_generated"
    LIKELY_FAN = "likely_fan_uploader"
    UNKNOWN = "unknown"


_LABEL_RE = re.compile(
    r"\b(?:records?|recordings?|record\s+label|music\s+group|publishing|"
    r"entertainment|productions?|label)\b",
    re.IGNORECASE,
)
_DISTRIBUTOR_RE = re.compile(
    r"\b(?:distribution|distributor|distrokid|tunecore|the\s+orchard|"
    r"believe\s+music|cdbaby|cd\s+baby|awal|vevo)\b",
    re.IGNORECASE,
)
_TOPIC_RE = re.compile(
    r"(?:\s+-\s+topic\s*$|\bauto[ -]?generated\b|\bprovided\s+to\s+youtube\b)",
    re.IGNORECASE,
)
_FAN_RE = re.compile(
    r"\b(?:fan(?:page|\s+page|\s+channel)?|tribute|unofficial|archive|"
    r"lyrics?|music\s+uploads?|collection)\b",
    re.IGNORECASE,
)
_OFFICIAL_SUFFIX_RE = re.compile(
    r"\s+(?:official(?:\s+channel)?|official\s+music|tv)\s*$", re.IGNORECASE
)


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _identity_key(value: object) -> str:
    text = _clean(value)
    text = _OFFICIAL_SUFFIX_RE.sub("", text)
    text = re.sub(r"vevo$", "", text, flags=re.IGNORECASE).strip()
    return normalize_for_comparison(text)


@dataclass(frozen=True)
class UploaderClassification:
    uploader: str
    classification: UploaderClass
    confidence: float
    reasons: tuple[str, ...]
    matched_artist: str | None = None

    @property
    def may_be_primary_artist(self) -> bool:
        return self.classification is UploaderClass.LIKELY_OFFICIAL_ARTIST

    @property
    def prevents_artist_use(self) -> bool:
        return self.classification in {
            UploaderClass.LIKELY_LABEL,
            UploaderClass.LIKELY_DISTRIBUTOR,
            UploaderClass.LIKELY_TOPIC,
            UploaderClass.LIKELY_FAN,
        }


def classify_uploader(
    uploader: object,
    *,
    provider_artists: Iterable[object] = (),
    parsed_artist: object | None = None,
) -> UploaderClassification:
    """Classify a channel using only local names and supplied provider context."""

    channel = _clean(uploader)
    if not channel:
        return UploaderClassification("", UploaderClass.UNKNOWN, 0.0, ("uploader_missing",))
    if _TOPIC_RE.search(channel):
        return UploaderClassification(
            channel, UploaderClass.LIKELY_TOPIC, 0.98, ("auto_generated_marker",)
        )
    if _DISTRIBUTOR_RE.search(channel):
        return UploaderClassification(
            channel, UploaderClass.LIKELY_DISTRIBUTOR, 0.94, ("distribution_marker",)
        )
    if _LABEL_RE.search(channel):
        return UploaderClassification(
            channel, UploaderClass.LIKELY_LABEL, 0.92, ("label_marker",)
        )
    if _FAN_RE.search(channel):
        return UploaderClassification(
            channel, UploaderClass.LIKELY_FAN, 0.9, ("fan_or_generic_upload_marker",)
        )

    channel_key = _identity_key(channel)
    artist_candidates = [_clean(value) for value in provider_artists if _clean(value)]
    for artist in artist_candidates:
        artist_key = _identity_key(artist)
        if channel_key and artist_key and channel_key == artist_key:
            return UploaderClassification(
                channel,
                UploaderClass.LIKELY_OFFICIAL_ARTIST,
                0.72,
                ("provider_artist_name_match",),
                matched_artist=artist,
            )

    parsed = _clean(parsed_artist)
    if parsed and channel_key == _identity_key(parsed):
        return UploaderClassification(
            channel,
            UploaderClass.LIKELY_OFFICIAL_ARTIST,
            0.62,
            ("parsed_artist_name_match",),
            matched_artist=parsed,
        )
    if _OFFICIAL_SUFFIX_RE.search(channel) and artist_candidates:
        closest = max(
            artist_candidates,
            key=lambda candidate: text_similarity(_identity_key(channel), _identity_key(candidate)),
        )
        similarity = text_similarity(_identity_key(channel), _identity_key(closest))
        if similarity >= 95.0:
            return UploaderClassification(
                channel,
                UploaderClass.LIKELY_OFFICIAL_ARTIST,
                0.66,
                ("official_marker_and_provider_similarity",),
                matched_artist=closest,
            )
    return UploaderClassification(channel, UploaderClass.UNKNOWN, 0.2, ("no_local_marker",))


@dataclass(frozen=True)
class ArtistFallback:
    artist: str | None
    provenance: str
    confidence: float
    uploader_classification: UploaderClassification


def choose_artist_fallback(
    *,
    uploader: object,
    discogs_artist: object | None = None,
    musicbrainz_artist: object | None = None,
    embedded_artist: object | None = None,
    parsed_artist: object | None = None,
) -> ArtistFallback:
    """Choose the best available artist, keeping uploader as the final fallback."""

    providers = [value for value in (discogs_artist, musicbrainz_artist) if _clean(value)]
    classification = classify_uploader(
        uploader, provider_artists=providers, parsed_artist=parsed_artist
    )
    for value, provenance, confidence in (
        (discogs_artist, "discogs", 0.96),
        (musicbrainz_artist, "musicbrainz", 0.9),
        (embedded_artist, "embedded", 0.82),
        (parsed_artist, "youtube_title_parsed", 0.72),
    ):
        clean = _clean(value)
        if clean:
            return ArtistFallback(clean, provenance, confidence, classification)

    if classification.may_be_primary_artist:
        return ArtistFallback(
            classification.matched_artist or classification.uploader,
            "youtube_uploader_official_hint",
            0.52,
            classification,
        )
    if not classification.prevents_artist_use and classification.uploader:
        return ArtistFallback(
            classification.uploader,
            "youtube_uploader_fallback",
            0.25,
            classification,
        )
    return ArtistFallback(None, "unknown", 0.0, classification)


__all__ = [
    "ArtistFallback",
    "UploaderClass",
    "UploaderClassification",
    "choose_artist_fallback",
    "classify_uploader",
]
