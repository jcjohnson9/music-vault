"""Field-level Discogs-first metadata candidate ensemble.

The ensemble is intentionally pure: it performs no I/O and grants no single
overall score permission to mutate every metadata field.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .matching import normalize_for_comparison
from .providers import ProviderArtistCredit, ProviderReleaseCandidate
from .title_parser import ParsedTitle, classify_version_hint
from .uploader_classifier import UploaderClassification, classify_uploader


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
    LOCKED = "locked"
    CONFLICT = "conflict"


class FieldAction(str, Enum):
    APPLY = "apply"
    REVIEW = "review"
    KEEP = "keep"


@dataclass(frozen=True)
class FieldCandidate:
    field_name: str
    value: Any
    source: str
    score: float
    provider_reference: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldResolution:
    field_name: str
    current_value: Any
    value: Any
    source: str | None
    confidence: ConfidenceLevel
    score: float
    action: FieldAction
    provider_reference: str | None = None
    conflict: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def safe_to_apply(self) -> bool:
        return self.action is FieldAction.APPLY and self.confidence is ConfidenceLevel.HIGH


@dataclass(frozen=True)
class VersionAssessment:
    version_type: str
    version_label: str | None
    conflict: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MetadataEnsemble:
    fields: tuple[FieldResolution, ...]
    discogs_candidate: ProviderReleaseCandidate | None
    musicbrainz_candidate: Any | None
    parsed_title: ParsedTitle | None
    uploader: UploaderClassification
    provider_agreement: tuple[str, ...]
    provider_disagreement: tuple[str, ...]
    reasons: tuple[str, ...]
    recording_group_key: str | None

    def field(self, field_name: str) -> FieldResolution | None:
        return next((item for item in self.fields if item.field_name == field_name), None)

    @property
    def apply_values(self) -> dict[str, Any]:
        return {item.field_name: item.value for item in self.fields if item.safe_to_apply}

    @property
    def review_fields(self) -> tuple[str, ...]:
        return tuple(item.field_name for item in self.fields if item.action is FieldAction.REVIEW)


_FIELD_ATTRS = {
    "title": "title",
    "artist": "artist",
    "artist_credits": "artist_credits",
    "album": "album",
    "album_artist": "album_artist",
    "release_date": "release_date",
    "original_release_date": "original_release_date",
    "version_type": "version_type",
    "version_label": "version_label",
    "discogs_release_id": "release_id",
    "discogs_master_id": "master_id",
    "discogs_track_position": "track_position",
    "musicbrainz_recording_id": "recording_id",
    "musicbrainz_release_id": "release_id",
    "artwork": "artwork",
}
_FIELD_ORDER = tuple(_FIELD_ATTRS)
_SOURCE_RANK = {
    "discogs": 0,
    "musicbrainz": 1,
    "embedded": 2,
    "youtube_title_parsed": 3,
    "youtube_uploader_official_hint": 4,
    "youtube_uploader_fallback": 5,
}
_ALLOWED_VERSION_TYPES = frozenset(
    {
        "studio",
        "live",
        "remix",
        "edit",
        "acoustic",
        "cover",
        "instrumental",
        "demo",
        "radio_edit",
        "extended",
        "sped_up",
        "slowed",
        "nightcore",
        "mashup",
        "re_recording",
        "soundtrack",
        "youtube_exclusive",
        "unknown",
    }
)
_NON_STUDIO_VERSION_TYPES = _ALLOWED_VERSION_TYPES - {"studio", "unknown", "youtube_exclusive"}


def _value(source: object | None, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _score(source: object | None) -> float:
    raw = _value(source, "provider_score", _value(source, "score", 0.0))
    try:
        return max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _candidate_reference(source: object | None) -> str | None:
    return _clean(_value(source, "provider_reference"))


def _canonical(value: Any) -> str:
    if isinstance(value, (tuple, list)):
        normalized: list[dict[str, str]] = []
        for item in value:
            normalized.append(
                {
                    "name": normalize_for_comparison(_value(item, "name")),
                    "role": _clean(_value(item, "role")) or "primary",
                    "join": _clean(_value(item, "join_phrase")) or "",
                }
            )
        return json.dumps(normalized, sort_keys=True)
    return normalize_for_comparison(value, title=False)


def classify_version(
    *values: object,
    source_version_type: object | None = None,
    candidate_version_type: object | None = None,
) -> VersionAssessment:
    """Classify version identity and surface conservative source conflicts."""

    inferred_type, label = classify_version_hint(*values)
    source_type = (_clean(source_version_type) or inferred_type or "unknown").casefold()
    candidate_type = (_clean(candidate_version_type) or "unknown").casefold()
    if source_type not in _ALLOWED_VERSION_TYPES:
        source_type = "unknown"
    if candidate_type not in _ALLOWED_VERSION_TYPES:
        candidate_type = "unknown"
    conflict = bool(
        source_type in _NON_STUDIO_VERSION_TYPES
        and candidate_type in {"studio", "unknown"}
        or candidate_type in _NON_STUDIO_VERSION_TYPES
        and source_type in _NON_STUDIO_VERSION_TYPES
        and source_type != candidate_type
    )
    reasons: list[str] = []
    if conflict:
        reasons.append("version_identity_conflict")
    elif source_type != "unknown" and candidate_type not in {"unknown", source_type}:
        reasons.append("version_identity_requires_review")
    resolved = source_type if source_type != "unknown" else candidate_type
    return VersionAssessment(resolved or "unknown", label, conflict, tuple(reasons))


def versions_compatible(source_type: object, candidate_type: object) -> bool:
    return not classify_version(
        source_version_type=source_type, candidate_version_type=candidate_type
    ).conflict


def recording_group_key(
    title: object,
    primary_artist: object,
    *,
    master_id: object | None = None,
) -> str | None:
    """Return a stable informational key; it is never a uniqueness key."""

    title_key = normalize_for_comparison(title, title=True)
    artist_key = normalize_for_comparison(primary_artist)
    master_key = _clean(master_id)
    if not title_key or not artist_key:
        return None
    identity = f"{artist_key}\n{title_key}\n{master_key or ''}".encode("utf-8")
    return "rg1_" + hashlib.sha256(identity).hexdigest()[:32]


def _candidate_for_field(
    field_name: str,
    source: object,
    provider: str,
    *,
    score: float,
) -> FieldCandidate | None:
    attribute = _FIELD_ATTRS[field_name]
    if provider == "musicbrainz" and field_name.startswith("discogs_"):
        return None
    if provider == "discogs" and field_name.startswith("musicbrainz_"):
        return None
    value = _value(source, attribute)
    if field_name == "artwork" and value is None:
        value = _value(source, "artwork_available")
        if value is not True:
            value = None
    if value is None or value == "" or value == () or value == []:
        return None
    field_scores = _value(source, "field_scores", {})
    if isinstance(field_scores, Mapping):
        try:
            score = float(field_scores.get(field_name, score))
        except (TypeError, ValueError, OverflowError):
            pass
    return FieldCandidate(
        field_name,
        value,
        provider,
        max(0.0, min(100.0, score)),
        _candidate_reference(source),
        tuple(_value(source, "reasons", ()) or ()),
    )


def _confidence(score: float) -> ConfidenceLevel:
    if score >= 85.0:
        return ConfidenceLevel.HIGH
    if score >= 60.0:
        return ConfidenceLevel.MEDIUM
    if score > 0.0:
        return ConfidenceLevel.LOW
    return ConfidenceLevel.NONE


def _resolve_field(
    field_name: str,
    current_value: Any,
    candidates: Sequence[FieldCandidate],
    *,
    locked: bool,
    version_conflict: bool,
    unofficial_live: bool,
) -> FieldResolution:
    if locked:
        return FieldResolution(
            field_name,
            current_value,
            current_value,
            "locked",
            ConfidenceLevel.LOCKED,
            100.0,
            FieldAction.KEEP,
            reasons=("manual_or_confirmed_lock",),
        )
    if unofficial_live and field_name in {"release_date", "album"}:
        return FieldResolution(
            field_name,
            current_value,
            None,
            None,
            ConfidenceLevel.NONE,
            0.0,
            FieldAction.KEEP,
            reasons=("unofficial_live_field_withheld",),
        )
    if not candidates:
        return FieldResolution(
            field_name,
            current_value,
            current_value,
            None,
            ConfidenceLevel.NONE,
            0.0,
            FieldAction.KEEP,
            reasons=("no_candidate",),
        )

    ordered = sorted(
        candidates,
        key=lambda candidate: (_SOURCE_RANK.get(candidate.source, 99), -candidate.score),
    )
    discogs = next((item for item in ordered if item.source == "discogs"), None)
    musicbrainz = next((item for item in ordered if item.source == "musicbrainz"), None)

    chosen = ordered[0]
    reasons = list(chosen.reasons)
    conflict = False
    agreement = False
    if discogs and musicbrainz:
        if _canonical(discogs.value) == _canonical(musicbrainz.value):
            chosen = discogs
            chosen = FieldCandidate(
                chosen.field_name,
                chosen.value,
                chosen.source,
                min(100.0, max(chosen.score, musicbrainz.score) + 3.0),
                chosen.provider_reference,
                chosen.reasons + ("musicbrainz_corroborated",),
            )
            reasons = list(chosen.reasons)
            agreement = True
        elif discogs.score >= 95.0 and musicbrainz.score < 95.0:
            chosen = discogs
            reasons.append("strong_discogs_over_weaker_musicbrainz")
        elif discogs.score < 90.0 and musicbrainz.score >= 92.0:
            chosen = musicbrainz
            reasons.append("strong_musicbrainz_over_weak_discogs")
        else:
            conflict = True
            chosen = discogs if discogs.score >= musicbrainz.score else musicbrainz
            reasons.append("provider_value_conflict")

    if version_conflict and chosen.source in {"discogs", "musicbrainz"}:
        conflict = True
        reasons.append("version_identity_conflict")
    if "release_ambiguous" in chosen.reasons and field_name in {"album", "release_date"}:
        conflict = True
        reasons.append("release_context_ambiguous")

    confidence = ConfidenceLevel.CONFLICT if conflict else _confidence(chosen.score)
    if conflict:
        action = FieldAction.REVIEW
    elif confidence is ConfidenceLevel.HIGH:
        action = FieldAction.APPLY
    elif confidence in {ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW}:
        action = FieldAction.REVIEW
    else:
        action = FieldAction.KEEP
    if _canonical(current_value) == _canonical(chosen.value):
        action = FieldAction.KEEP
        reasons.append("effective_value_already_matches")
    if agreement:
        reasons.append("provider_agreement")
    return FieldResolution(
        field_name,
        current_value,
        chosen.value,
        chosen.source,
        confidence,
        chosen.score,
        action,
        chosen.provider_reference,
        conflict,
        tuple(dict.fromkeys(reasons)),
    )


def build_metadata_ensemble(
    *,
    current: Mapping[str, Any],
    discogs_candidates: Sequence[ProviderReleaseCandidate] = (),
    musicbrainz_candidates: Sequence[object] = (),
    parsed_title: ParsedTitle | None = None,
    embedded: Mapping[str, Any] | None = None,
    uploader: object = "",
    locked_fields: Iterable[str] = (),
    confirmed_locked_fields: Iterable[str] = (),
    youtube_exclusive: bool = False,
    unofficial_live: bool = False,
) -> MetadataEnsemble:
    """Build independent apply/review/keep decisions for every supported field."""

    discogs = discogs_candidates[0] if discogs_candidates else None
    musicbrainz = musicbrainz_candidates[0] if musicbrainz_candidates else None
    parsed = parsed_title
    uploader_state = classify_uploader(
        uploader,
        provider_artists=(
            _value(discogs, "artist", ""),
            _value(musicbrainz, "artist", ""),
        ),
        parsed_artist=_value(parsed, "artist_hint"),
    )
    locks = {str(item).strip().casefold() for item in locked_fields}
    locks.update(str(item).strip().casefold() for item in confirmed_locked_fields)

    source_version = _value(parsed, "version_type", current.get("version_type", "unknown"))
    candidate_version = _value(discogs, "version_type", _value(musicbrainz, "version_type", "unknown"))
    version = classify_version(
        _value(parsed, "search_title", current.get("title")),
        source_version_type=source_version,
        candidate_version_type=candidate_version,
    )

    per_field: dict[str, list[FieldCandidate]] = {name: [] for name in _FIELD_ORDER}
    if discogs is not None:
        base_score = _score(discogs)
        for field_name in _FIELD_ORDER:
            candidate = _candidate_for_field(field_name, discogs, "discogs", score=base_score)
            if candidate:
                per_field[field_name].append(candidate)
    if musicbrainz is not None:
        base_score = _score(musicbrainz)
        for field_name in _FIELD_ORDER:
            candidate = _candidate_for_field(
                field_name, musicbrainz, "musicbrainz", score=base_score
            )
            if candidate:
                per_field[field_name].append(candidate)

    if embedded:
        for field_name in ("title", "artist", "album", "album_artist", "release_date"):
            value = embedded.get(field_name)
            if value not in (None, ""):
                per_field[field_name].append(
                    FieldCandidate(field_name, value, "embedded", 82.0, reasons=("embedded_tag",))
                )

    if parsed:
        parsed_score = 86.0 if youtube_exclusive and parsed.strong_pattern else 72.0
        parsed_values = {
            "title": parsed.title_hint,
            "artist": parsed.artist_hint,
            "version_type": (
                "youtube_exclusive"
                if youtube_exclusive and parsed.version_type == "unknown"
                else parsed.version_type
            ),
            "version_label": parsed.version_label,
        }
        for field_name, value in parsed_values.items():
            if value not in (None, "", "unknown"):
                per_field[field_name].append(
                    FieldCandidate(
                        field_name,
                        value,
                        "youtube_title_parsed",
                        parsed_score,
                        reasons=(
                            "youtube_exclusive_supported"
                            if youtube_exclusive
                            else "parsed_title_hint_only",
                        ),
                    )
                )
        if parsed.artist_hint and parsed.featured_artist_hint:
            credits = (
                ProviderArtistCredit(parsed.artist_hint, role="primary", join_phrase=" feat. "),
                ProviderArtistCredit(parsed.featured_artist_hint, role="featured"),
            )
            per_field["artist_credits"].append(
                FieldCandidate(
                    "artist_credits",
                    credits,
                    "youtube_title_parsed",
                    parsed_score,
                    reasons=("provisional_featured_credit",),
                )
            )

    # Uploader stays provenance.  It is offered only after every better source
    # and never when local classification identifies a company or fan channel.
    if not per_field["artist"] and not uploader_state.prevents_artist_use and uploader_state.uploader:
        source = (
            "youtube_uploader_official_hint"
            if uploader_state.may_be_primary_artist
            else "youtube_uploader_fallback"
        )
        per_field["artist"].append(
            FieldCandidate(
                "artist",
                uploader_state.matched_artist or uploader_state.uploader,
                source,
                52.0 if uploader_state.may_be_primary_artist else 25.0,
                reasons=("uploader_is_last_fallback",),
            )
        )

    resolutions = tuple(
        _resolve_field(
            field_name,
            current.get(field_name),
            per_field[field_name],
            locked=field_name.casefold() in locks,
            version_conflict=version.conflict,
            unofficial_live=unofficial_live,
        )
        for field_name in _FIELD_ORDER
    )

    agreements = tuple(
        item.field_name
        for item in resolutions
        if "provider_agreement" in item.reasons
    )
    disagreements = tuple(item.field_name for item in resolutions if item.conflict)
    title = next((item.value for item in resolutions if item.field_name == "title" and item.value), current.get("title"))
    artist = next((item.value for item in resolutions if item.field_name == "artist" and item.value), current.get("artist"))
    group_key = recording_group_key(title, artist, master_id=_value(discogs, "master_id"))
    reasons: list[str] = list(version.reasons)
    if youtube_exclusive and discogs is None and musicbrainz is None:
        reasons.append("youtube_exclusive_fallback")
    if disagreements:
        reasons.append("provider_conflict_requires_review")
    return MetadataEnsemble(
        resolutions,
        discogs,
        musicbrainz,
        parsed,
        uploader_state,
        agreements,
        disagreements,
        tuple(dict.fromkeys(reasons)),
        group_key,
    )


build_ensemble = build_metadata_ensemble


__all__ = [
    "ConfidenceLevel",
    "FieldAction",
    "FieldCandidate",
    "FieldResolution",
    "MetadataEnsemble",
    "VersionAssessment",
    "build_ensemble",
    "build_metadata_ensemble",
    "classify_version",
    "recording_group_key",
    "versions_compatible",
]
