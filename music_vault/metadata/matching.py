from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .schema import normalize_release_date


HIGH_CONFIDENCE_PROVIDER_SCORE = 95.0
HIGH_CONFIDENCE_DURATION_DELTA_SECONDS = 5.0
UNIQUE_PROVIDER_SCORE_MARGIN = 5.0

_WHITESPACE_RE = re.compile(r"\s+")
_BRACKETED_SUFFIX_RE = re.compile(r"\s*[\[(]([^\[\]()]{1,100})[\])]\s*$")
_DELIMITED_SUFFIX_RE = re.compile(r"\s+(?:[-|:\u2013\u2014])\s*([^|]{1,100})\s*$")
_PRESENTATION_WORDS = frozenset(
    {
        "4k",
        "audio",
        "clip",
        "hd",
        "hq",
        "lyric",
        "lyrics",
        "music",
        "mv",
        "official",
        "officially",
        "video",
        "visualiser",
        "visualizer",
    }
)
_PRESENTATION_MARKERS = frozenset(
    {
        "4k",
        "audio",
        "clip",
        "hd",
        "hq",
        "lyric",
        "lyrics",
        "mv",
        "official",
        "video",
        "visualiser",
        "visualizer",
    }
)
_RISK_PATTERNS = (
    ("acoustic", re.compile(r"\bacoustic\b")),
    ("clean", re.compile(r"\bclean(?:\s+version)?\b")),
    ("cover", re.compile(r"\bcover\b")),
    ("demo", re.compile(r"\bdemo\b")),
    ("edit", re.compile(r"\b(?:(?:radio|single|extended|game)\s+)?edit\b")),
    ("extended", re.compile(r"\bextended\b")),
    ("explicit", re.compile(r"\bexplicit\b")),
    ("fan_made", re.compile(r"\bfan[ -]?made\b")),
    ("instrumental", re.compile(r"\binstrumental\b")),
    ("karaoke", re.compile(r"\bkaraoke\b")),
    ("live", re.compile(r"\blive(?:\s+(?:at|from|version))?\b")),
    ("fan_upload", re.compile(r"\bfan[ -]?upload(?:ed)?\b")),
    ("game_version", re.compile(r"\bgame\s+(?:edit|version)\b")),
    ("mashup", re.compile(r"\bmash[ -]?up\b")),
    ("mix", re.compile(r"\b(?:(?:club|dance|extended|original)\s+)?mix\b")),
    ("nightcore", re.compile(r"\bnightcore\b")),
    ("remaster", re.compile(r"\bremaster(?:ed)?\b")),
    ("remix", re.compile(r"\bremix(?:ed)?\b")),
    ("parody", re.compile(r"\bparody\b")),
    ("radio_version", re.compile(r"\bradio\s+version\b")),
    ("rerecorded", re.compile(r"\bre[ -]?record(?:ed|ing)\b")),
    ("slowed", re.compile(r"\bslowed(?:\s+and\s+reverb)?\b")),
    ("sped_up", re.compile(r"\bsped\s+up\b")),
    ("theme", re.compile(r"\btheme\b")),
    ("unofficial", re.compile(r"\b(?:bootleg|unofficial)\b")),
    ("unreleased", re.compile(r"\bunreleased\b")),
    ("version", re.compile(r"\bversion\b")),
)
_KNOWN_RISK_CODES = frozenset(code for code, _pattern in _RISK_PATTERNS)

_REASON_ORDER = (
    "explicit_skip",
    "missing_required_query_metadata",
    "authoritative_lock_present",
    "no_candidates",
    "no_viable_candidates",
    "candidate_not_unique",
    "provider_score_below_high_confidence",
    "title_not_exact",
    "artist_not_exact",
    "duration_unavailable",
    "duration_conflict",
    "version_risk_present",
    "identity_conflict",
    "field_conflict_present",
    "strict_high_confidence_match",
)


class MatchClassification(str, Enum):
    HIGH_CONFIDENCE = "high_confidence"
    NEEDS_REVIEW = "needs_review"
    REVIEW = "needs_review"  # Backward-friendly alias for callers using the shorter name.
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"
    SKIPPED = "skipped"


class FieldConfidence(str, Enum):
    EXACT = "exact"
    HIGH = "high"
    COMPATIBLE = "compatible"
    REVIEW = "review"
    CONFLICT = "conflict"
    MISSING = "missing"
    LOCKED = "locked"


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    return text or None


def _presentation_only(value: str) -> bool:
    words = normalize_for_comparison(value).split()
    return bool(words) and bool(set(words) & _PRESENTATION_MARKERS) and set(words) <= _PRESENTATION_WORDS


def clean_presentation_suffixes(value: object) -> str:
    """Remove display-only media suffixes without erasing version qualifiers."""

    text = _clean_optional(value) or ""
    changed = True
    while text and changed:
        changed = False
        bracketed = _BRACKETED_SUFFIX_RE.search(text)
        if bracketed and _presentation_only(bracketed.group(1)):
            text = text[: bracketed.start()].rstrip(" -|:\u2013\u2014")
            changed = True
            continue
        delimited = _DELIMITED_SUFFIX_RE.search(text)
        if delimited and _presentation_only(delimited.group(1)):
            text = text[: delimited.start()].rstrip(" -|:\u2013\u2014")
            changed = True
    return _WHITESPACE_RE.sub(" ", text).strip()


strip_presentation_suffixes = clean_presentation_suffixes


def normalize_for_comparison(value: object, *, title: bool = False) -> str:
    """Produce a Unicode-, case-, whitespace-, and punctuation-stable key."""

    text = clean_presentation_suffixes(value) if title else (_clean_optional(value) or "")
    text = unicodedata.normalize("NFKD", text.casefold()).replace("&", " and ")
    normalized: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("M"):
            continue
        if character in {"'", "\u2019", "\u02bc"}:
            continue
        if category.startswith(("P", "S", "Z")):
            normalized.append(" ")
        else:
            normalized.append(character)
    return _WHITESPACE_RE.sub(" ", "".join(normalized)).strip()


normalize_text = normalize_for_comparison


def text_similarity(left: object, right: object, *, title: bool = False) -> float:
    left_key = normalize_for_comparison(left, title=title)
    right_key = normalize_for_comparison(right, title=title)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 100.0
    return round(SequenceMatcher(None, left_key, right_key).ratio() * 100.0, 3)


def extract_risk_qualifiers(value: object) -> tuple[str, ...]:
    """Return stable qualifier codes from every part of a presentation title."""

    text = _clean_optional(value) or ""
    segments = [match.group(1) for match in re.finditer(r"[\[(]([^\[\]()]{1,100})[\])]", text)]
    delimited = _DELIMITED_SUFFIX_RE.search(text)
    if delimited:
        segments.append(delimited.group(1))
    # Providers and existing filenames place version qualifiers in inconsistent
    # positions. Strict automatic remediation must treat any occurrence as a
    # review signal, including prefixes such as "Live at Wembley - Song".
    segments.append(text)
    normalized = " | ".join(normalize_for_comparison(segment) for segment in segments)
    return tuple(code for code, pattern in _RISK_PATTERNS if pattern.search(normalized))


@dataclass(frozen=True)
class QueryNormalization:
    title: str
    artist: str
    normalized_title: str
    normalized_artist: str
    risk_flags: tuple[str, ...]

    def to_dict(self, *, include_values: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "has_title": bool(self.title),
            "has_artist": bool(self.artist),
            "risk_flags": list(self.risk_flags),
        }
        if include_values:
            payload.update(
                {
                    "title": self.title,
                    "artist": self.artist,
                    "normalized_title": self.normalized_title,
                    "normalized_artist": self.normalized_artist,
                }
            )
        return payload


def normalize_query(title: object, artist: object) -> QueryNormalization:
    clean_title = clean_presentation_suffixes(title)
    clean_artist = _clean_optional(artist) or ""
    return QueryNormalization(
        title=clean_title,
        artist=clean_artist,
        normalized_title=normalize_for_comparison(clean_title, title=True),
        normalized_artist=normalize_for_comparison(clean_artist),
        risk_flags=extract_risk_qualifiers(title),
    )


@dataclass(frozen=True)
class TrackQuery:
    title: str | None
    artist: str | None
    album: str | None = None
    album_artist: str | None = None
    release_date: str | None = None
    artwork: str | None = None
    duration_seconds: float | None = None
    recording_id: str | None = None
    release_id: str | None = None
    locked_fields: frozenset[str] = field(default_factory=frozenset)
    skip: bool = False


MetadataMatchQuery = TrackQuery


@dataclass(frozen=True)
class MatchCandidate:
    title: str | None
    artist: str | None
    provider_score: float
    album: str | None = None
    album_artist: str | None = None
    release_date: str | None = None
    artwork: str | None = None
    duration_seconds: float | None = None
    recording_id: str | None = None
    release_id: str | None = None
    provider: str = "unknown"
    provider_order: int = 0
    risk_flags: tuple[str, ...] = ()

    def to_dict(self, *, include_values: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "provider_score": self.provider_score,
            "provider_order": self.provider_order,
            "has_title": bool(self.title),
            "has_artist": bool(self.artist),
            "has_album": bool(self.album),
            "has_album_artist": bool(self.album_artist),
            "has_release_date": bool(self.release_date),
            "has_artwork": bool(self.artwork),
            "has_duration": self.duration_seconds is not None,
            "has_recording_id": bool(self.recording_id),
            "has_release_id": bool(self.release_id),
            "risk_flags": list(self.risk_flags),
        }
        if include_values:
            payload.update(
                {
                    "title": self.title,
                    "artist": self.artist,
                    "album": self.album,
                    "album_artist": self.album_artist,
                    "release_date": self.release_date,
                    "artwork": self.artwork,
                    "duration_seconds": self.duration_seconds,
                    "recording_id": self.recording_id,
                    "release_id": self.release_id,
                }
            )
        return payload


@dataclass(frozen=True)
class FieldDecision:
    field_name: str
    confidence: FieldConfidence
    confidence_score: float
    recommended: bool
    reason: str
    current_value: str | None = None
    candidate_value: str | None = None

    @property
    def safe_to_apply(self) -> bool:
        return self.recommended and self.confidence in {
            FieldConfidence.EXACT,
            FieldConfidence.HIGH,
            FieldConfidence.COMPATIBLE,
        }

    def to_dict(self, *, include_values: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "field_name": self.field_name,
            "confidence": self.confidence.value,
            "confidence_score": self.confidence_score,
            "recommended": self.recommended,
            "safe_to_apply": self.safe_to_apply,
            "reason": self.reason,
            "has_current_value": self.current_value is not None,
            "has_candidate_value": self.candidate_value is not None,
        }
        if include_values:
            payload["current_value"] = self.current_value
            payload["candidate_value"] = self.candidate_value
        return payload


@dataclass(frozen=True)
class CandidateAssessment:
    candidate_index: int
    candidate: MatchCandidate
    match_score: float
    title_similarity: float
    artist_similarity: float
    duration_delta_seconds: float | None
    risk_flags: tuple[str, ...]
    conflicts: tuple[str, ...]
    field_decisions: tuple[FieldDecision, ...]
    reasons: tuple[str, ...]

    @property
    def title_exact(self) -> bool:
        return self.title_similarity == 100.0

    @property
    def artist_exact(self) -> bool:
        return self.artist_similarity == 100.0

    @property
    def field_confidences(self) -> dict[str, FieldConfidence]:
        return {item.field_name: item.confidence for item in self.field_decisions}

    def field_decision(self, field_name: str) -> FieldDecision | None:
        return next((item for item in self.field_decisions if item.field_name == field_name), None)

    def to_dict(self, *, include_values: bool = False) -> dict[str, Any]:
        return {
            "candidate_index": self.candidate_index,
            "candidate": self.candidate.to_dict(include_values=include_values),
            "match_score": self.match_score,
            "title_similarity": self.title_similarity,
            "artist_similarity": self.artist_similarity,
            "duration_delta_seconds": self.duration_delta_seconds,
            "risk_flags": list(self.risk_flags),
            "conflicts": list(self.conflicts),
            "reasons": list(self.reasons),
            "field_decisions": [
                item.to_dict(include_values=include_values) for item in self.field_decisions
            ],
        }


@dataclass(frozen=True)
class MatchResult:
    classification: MatchClassification
    reasons: tuple[str, ...]
    assessments: tuple[CandidateAssessment, ...] = ()
    selected: CandidateAssessment | None = None
    unique_margin: float | None = None

    @property
    def outcome(self) -> MatchClassification:
        return self.classification

    @property
    def is_high_confidence(self) -> bool:
        return self.classification is MatchClassification.HIGH_CONFIDENCE

    @property
    def selected_candidate(self) -> MatchCandidate | None:
        return self.selected.candidate if self.selected is not None else None

    def to_dict(self, *, include_values: bool = False) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "classification": self.classification.value,
            "reasons": list(self.reasons),
            "unique_margin": self.unique_margin,
            "selected_candidate_index": (
                self.selected.candidate_index if self.selected is not None else None
            ),
            "assessment_count": len(self.assessments),
            "assessments": [
                item.to_dict(include_values=include_values) for item in self.assessments
            ],
        }


def _value(source: object, *names: str) -> object:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _duration(source: object) -> float | None:
    raw = _value(source, "duration_seconds", "duration")
    if raw is None:
        milliseconds = _value(source, "duration_ms", "length_ms")
        if milliseconds is not None:
            try:
                raw = float(milliseconds) / 1000.0
            except (TypeError, ValueError):
                return None
    try:
        result = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    if result is None or not math.isfinite(result) or result < 0:
        return None
    return result


def _provider_score(source: object) -> float:
    raw = _value(source, "provider_score", "score")
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return round(min(100.0, max(0.0, score)), 3)


def _coerce_query(query: TrackQuery | Mapping[str, Any] | object) -> TrackQuery:
    if isinstance(query, TrackQuery):
        return query
    locked_value = _value(query, "locked_fields") or ()
    locked = (locked_value,) if isinstance(locked_value, str) else locked_value
    return TrackQuery(
        title=_clean_optional(_value(query, "title")),
        artist=_clean_optional(_value(query, "artist")),
        album=_clean_optional(_value(query, "album")),
        album_artist=_clean_optional(_value(query, "album_artist")),
        release_date=_clean_optional(_value(query, "release_date", "year")),
        artwork=_clean_optional(_value(query, "artwork", "cover_path")),
        duration_seconds=_duration(query),
        recording_id=_clean_optional(
            _value(query, "recording_id", "musicbrainz_recording_id")
        ),
        release_id=_clean_optional(_value(query, "release_id", "musicbrainz_release_id")),
        locked_fields=frozenset(
            str(item).strip().casefold() for item in locked if str(item).strip()
        ),
        skip=bool(_value(query, "skip", "skipped")),
    )


def _coerce_candidate(candidate: MatchCandidate | Mapping[str, Any] | object, index: int) -> MatchCandidate:
    if isinstance(candidate, MatchCandidate):
        return candidate
    explicit_value = _value(candidate, "risk_flags") or ()
    explicit_risks = (explicit_value,) if isinstance(explicit_value, str) else explicit_value
    release_date = _clean_optional(_value(candidate, "release_date", "date"))
    if release_date is None:
        release_date = _clean_optional(_value(candidate, "year"))
    title = _clean_optional(_value(candidate, "title"))
    risks: set[str] = set()
    for item in explicit_risks:
        code = str(item).strip().casefold()
        if code:
            risks.add(code if code in _KNOWN_RISK_CODES else "provider_risk")
    risks.update(extract_risk_qualifiers(title))
    return MatchCandidate(
        title=title,
        artist=_clean_optional(_value(candidate, "artist")),
        provider_score=_provider_score(candidate),
        album=_clean_optional(_value(candidate, "album")),
        album_artist=_clean_optional(_value(candidate, "album_artist")),
        release_date=release_date,
        artwork=_clean_optional(_value(candidate, "artwork", "cover_path", "artwork_url")),
        duration_seconds=_duration(candidate),
        recording_id=_clean_optional(
            _value(candidate, "recording_id", "musicbrainz_recording_id")
        ),
        release_id=_clean_optional(_value(candidate, "release_id", "musicbrainz_release_id")),
        provider=_clean_optional(_value(candidate, "provider")) or "unknown",
        provider_order=int(_value(candidate, "provider_order") or index),
        risk_flags=tuple(sorted(risks)),
    )


def _field_decision(
    field_name: str,
    current: object,
    candidate: object,
    *,
    locked: bool,
    title: bool = False,
) -> FieldDecision:
    current_text = _clean_optional(current)
    candidate_text = _clean_optional(candidate)
    if locked:
        return FieldDecision(
            field_name, FieldConfidence.LOCKED, 100.0, False, "authoritative_lock", current_text, candidate_text
        )
    if candidate_text is None:
        return FieldDecision(
            field_name, FieldConfidence.MISSING, 0.0, False, "candidate_value_missing", current_text, None
        )
    if current_text is None:
        return FieldDecision(
            field_name, FieldConfidence.HIGH, 90.0, True, "current_value_missing", None, candidate_text
        )
    similarity = text_similarity(current_text, candidate_text, title=title)
    if similarity == 100.0:
        return FieldDecision(
            field_name,
            FieldConfidence.EXACT,
            100.0,
            current_text != candidate_text,
            "normalized_values_match",
            current_text,
            candidate_text,
        )
    if similarity >= 90.0:
        return FieldDecision(
            field_name, FieldConfidence.REVIEW, similarity, False, "values_nearly_match", current_text, candidate_text
        )
    return FieldDecision(
        field_name, FieldConfidence.CONFLICT, similarity, False, "values_conflict", current_text, candidate_text
    )


def _release_decision(current: object, candidate: object, *, locked: bool) -> FieldDecision:
    current_text = _clean_optional(current)
    candidate_text = _clean_optional(candidate)
    if locked:
        return FieldDecision(
            "release_date", FieldConfidence.LOCKED, 100.0, False, "authoritative_lock", current_text, candidate_text
        )
    if candidate_text is None:
        return FieldDecision(
            "release_date", FieldConfidence.MISSING, 0.0, False, "candidate_value_missing", current_text, None
        )
    try:
        candidate_date = normalize_release_date(candidate_text)
    except ValueError:
        return FieldDecision(
            "release_date", FieldConfidence.CONFLICT, 0.0, False, "candidate_date_invalid", current_text, candidate_text
        )
    if current_text is None:
        return FieldDecision(
            "release_date", FieldConfidence.HIGH, 90.0, True, "current_value_missing", None, candidate_date
        )
    try:
        current_date = normalize_release_date(current_text)
    except ValueError:
        return FieldDecision(
            "release_date", FieldConfidence.CONFLICT, 0.0, False, "current_date_invalid", current_text, candidate_date
        )
    if current_date == candidate_date:
        return FieldDecision(
            "release_date", FieldConfidence.EXACT, 100.0, False, "normalized_values_match", current_date, candidate_date
        )
    if candidate_date.startswith(f"{current_date}-"):
        return FieldDecision(
            "release_date", FieldConfidence.COMPATIBLE, 95.0, True, "candidate_adds_precision", current_date, candidate_date
        )
    if current_date.startswith(f"{candidate_date}-"):
        return FieldDecision(
            "release_date", FieldConfidence.COMPATIBLE, 90.0, False, "current_has_more_precision", current_date, candidate_date
        )
    return FieldDecision(
        "release_date", FieldConfidence.CONFLICT, 0.0, False, "release_dates_conflict", current_date, candidate_date
    )


def _ordered_reasons(reasons: Iterable[str]) -> tuple[str, ...]:
    unique = set(reasons)
    order = {reason: index for index, reason in enumerate(_REASON_ORDER)}
    return tuple(sorted(unique, key=lambda reason: (order.get(reason, len(order)), reason)))


def _assess(
    query: TrackQuery,
    candidate: MatchCandidate,
    candidate_index: int,
    locked_fields: frozenset[str],
) -> CandidateAssessment:
    title_similarity = text_similarity(query.title, candidate.title, title=True)
    artist_similarity = text_similarity(query.artist, candidate.artist)
    duration_delta = (
        round(abs(query.duration_seconds - candidate.duration_seconds), 3)
        if query.duration_seconds is not None and candidate.duration_seconds is not None
        else None
    )
    query_risks = extract_risk_qualifiers(query.title)
    candidate_risks = extract_risk_qualifiers(candidate.title)
    declared_risks = {
        code if code in _KNOWN_RISK_CODES else "provider_risk"
        for item in candidate.risk_flags
        if (code := str(item).strip().casefold())
    }
    risks = tuple(
        sorted(set(query_risks) | set(candidate_risks) | declared_risks)
    )
    decisions = (
        _field_decision(
            "title", query.title, candidate.title, locked="title" in locked_fields, title=True
        ),
        _field_decision(
            "artist", query.artist, candidate.artist, locked="artist" in locked_fields
        ),
        _field_decision("album", query.album, candidate.album, locked="album" in locked_fields),
        _field_decision(
            "album_artist",
            query.album_artist,
            candidate.album_artist,
            locked="album_artist" in locked_fields,
        ),
        _release_decision(
            query.release_date, candidate.release_date, locked="release_date" in locked_fields
        ),
        _field_decision(
            "artwork", query.artwork, candidate.artwork, locked="artwork" in locked_fields
        ),
    )
    conflicts: list[str] = []
    if query.recording_id and candidate.recording_id and query.recording_id != candidate.recording_id:
        conflicts.append("recording_identity_conflict")
    if query.release_id and candidate.release_id and query.release_id != candidate.release_id:
        conflicts.append("release_identity_conflict")
    if duration_delta is not None and duration_delta > HIGH_CONFIDENCE_DURATION_DELTA_SECONDS:
        conflicts.append("duration_conflict")
    conflicts.extend(
        f"{item.field_name}_conflict"
        for item in decisions
        if item.confidence is FieldConfidence.CONFLICT
    )
    duration_score = (
        100.0
        if duration_delta is not None and duration_delta <= 1.0
        else 75.0
        if duration_delta is not None and duration_delta <= HIGH_CONFIDENCE_DURATION_DELTA_SECONDS
        else 0.0
        if duration_delta is not None
        else 40.0
    )
    score = (
        candidate.provider_score * 0.35
        + title_similarity * 0.30
        + artist_similarity * 0.25
        + duration_score * 0.10
    )
    score -= min(30.0, len(conflicts) * 8.0)
    if risks:
        score -= 10.0
    reasons: list[str] = []
    if candidate.provider_score < HIGH_CONFIDENCE_PROVIDER_SCORE:
        reasons.append("provider_score_below_high_confidence")
    if title_similarity != 100.0:
        reasons.append("title_not_exact")
    if artist_similarity != 100.0:
        reasons.append("artist_not_exact")
    if duration_delta is None:
        reasons.append("duration_unavailable")
    elif duration_delta > HIGH_CONFIDENCE_DURATION_DELTA_SECONDS:
        reasons.append("duration_conflict")
    if risks:
        reasons.append("version_risk_present")
    if any(item.endswith("identity_conflict") for item in conflicts):
        reasons.append("identity_conflict")
    if any(item.endswith("_conflict") for item in conflicts):
        reasons.append("field_conflict_present")
    return CandidateAssessment(
        candidate_index=candidate_index,
        candidate=candidate,
        match_score=round(max(0.0, min(100.0, score)), 3),
        title_similarity=title_similarity,
        artist_similarity=artist_similarity,
        duration_delta_seconds=duration_delta,
        risk_flags=risks,
        conflicts=tuple(sorted(set(conflicts))),
        field_decisions=decisions,
        reasons=_ordered_reasons(reasons),
    )


def _identity_key(assessment: CandidateAssessment) -> tuple[object, ...]:
    candidate = assessment.candidate
    if candidate.recording_id:
        return ("provider", candidate.recording_id)
    return (
        "text",
        # Without a stable provider identity, two rows cannot safely be
        # assumed to represent the same recording/release.
        assessment.candidate_index,
        normalize_for_comparison(candidate.title, title=True),
        normalize_for_comparison(candidate.artist),
        normalize_for_comparison(candidate.album),
        _clean_optional(candidate.release_date) or "",
    )


def _viable(assessment: CandidateAssessment) -> bool:
    return (
        assessment.title_similarity >= 70.0
        and assessment.artist_similarity >= 70.0
        and assessment.match_score >= 55.0
    )


def _release_key(assessment: CandidateAssessment) -> tuple[str, ...]:
    candidate = assessment.candidate
    if candidate.release_id:
        return ("provider", candidate.release_id)
    return (
        "text",
        normalize_for_comparison(candidate.album),
        _clean_optional(candidate.release_date) or "",
    )


def _mark_uncertain_release_fields(
    assessments: tuple[CandidateAssessment, ...],
    query: TrackQuery,
) -> tuple[CandidateAssessment, ...]:
    """Keep recording identity distinct from tied release-level choices."""

    by_recording: dict[str, list[CandidateAssessment]] = {}
    for assessment in assessments:
        recording_id = assessment.candidate.recording_id
        if recording_id:
            by_recording.setdefault(recording_id, []).append(assessment)

    uncertain_indexes: set[int] = set()
    for group in by_recording.values():
        release_groups: dict[tuple[str, ...], list[CandidateAssessment]] = {}
        for assessment in group:
            release_groups.setdefault(_release_key(assessment), []).append(assessment)
        if len(release_groups) <= 1:
            continue
        best_per_release = sorted(
            (max(items, key=lambda item: item.candidate.provider_score) for items in release_groups.values()),
            key=lambda item: (-item.candidate.provider_score, item.candidate.provider_order, item.candidate_index),
        )
        existing_release = next(
            (
                item
                for item in best_per_release
                if query.release_id and item.candidate.release_id == query.release_id
            ),
            None,
        )
        clearly_preferred = existing_release is not None or (
            best_per_release[0].candidate.provider_score
            - best_per_release[1].candidate.provider_score
            >= UNIQUE_PROVIDER_SCORE_MARGIN
        )
        if not clearly_preferred:
            uncertain_indexes.update(item.candidate_index for item in group)
        else:
            preferred_key = _release_key(existing_release or best_per_release[0])
            uncertain_indexes.update(
                item.candidate_index for item in group if _release_key(item) != preferred_key
            )

    if not uncertain_indexes:
        return assessments
    updated: list[CandidateAssessment] = []
    for assessment in assessments:
        if assessment.candidate_index not in uncertain_indexes:
            updated.append(assessment)
            continue
        decisions = tuple(
            replace(
                decision,
                confidence=FieldConfidence.REVIEW,
                confidence_score=min(decision.confidence_score, 50.0),
                recommended=False,
                reason="release_identity_ambiguous",
            )
            if decision.field_name in {"album", "album_artist", "release_date", "artwork"}
            and decision.candidate_value is not None
            else decision
            for decision in assessment.field_decisions
        )
        updated.append(replace(assessment, field_decisions=decisions))
    return tuple(updated)


def classify_candidates(
    query: TrackQuery | Mapping[str, Any] | object,
    candidates: Sequence[MatchCandidate | Mapping[str, Any] | object] | Iterable[MatchCandidate | Mapping[str, Any] | object],
    *,
    locked_fields: frozenset[str] = frozenset(),
) -> MatchResult:
    """Rank candidates and apply the conservative Batch 7 match policy."""

    current = _coerce_query(query)
    locks = frozenset(
        str(item).strip().casefold()
        for item in (set(current.locked_fields) | set(locked_fields))
        if str(item).strip()
    )
    normalized_query = normalize_query(current.title, current.artist)
    if current.skip:
        return MatchResult(MatchClassification.SKIPPED, ("explicit_skip",))
    if not normalized_query.normalized_title or not normalized_query.normalized_artist:
        return MatchResult(
            MatchClassification.SKIPPED, ("missing_required_query_metadata",)
        )

    coerced = tuple(_coerce_candidate(candidate, index) for index, candidate in enumerate(candidates))
    assessments = tuple(
        sorted(
            (
                _assess(current, candidate, index, locks)
                for index, candidate in enumerate(coerced)
            ),
            key=lambda item: (
                -item.match_score,
                -item.candidate.provider_score,
                item.candidate.provider_order,
                item.candidate_index,
            ),
        )
    )
    assessments = _mark_uncertain_release_fields(assessments, current)
    if locks:
        return MatchResult(
            MatchClassification.SKIPPED,
            ("authoritative_lock_present",),
            assessments,
            assessments[0] if assessments else None,
        )
    if not assessments:
        return MatchResult(MatchClassification.NO_MATCH, ("no_candidates",))
    selected = assessments[0]
    if not _viable(selected):
        return MatchResult(
            MatchClassification.NO_MATCH,
            ("no_viable_candidates",),
            assessments,
            selected,
        )

    distinct_rivals = [
        item
        for item in assessments[1:]
        if _viable(item) and _identity_key(item) != _identity_key(selected)
    ]
    unique_margin: float | None = None
    ambiguous = False
    if distinct_rivals:
        runner_up = distinct_rivals[0]
        if selected.title_exact and selected.artist_exact and runner_up.title_exact and runner_up.artist_exact:
            unique_margin = round(
                selected.candidate.provider_score - runner_up.candidate.provider_score, 3
            )
            ambiguous = unique_margin < UNIQUE_PROVIDER_SCORE_MARGIN
        else:
            unique_margin = round(selected.match_score - runner_up.match_score, 3)
            ambiguous = unique_margin < UNIQUE_PROVIDER_SCORE_MARGIN
    if ambiguous:
        return MatchResult(
            MatchClassification.AMBIGUOUS,
            ("candidate_not_unique",),
            assessments,
            selected,
            unique_margin,
        )

    reasons = list(selected.reasons)
    if distinct_rivals and unique_margin is not None and unique_margin < UNIQUE_PROVIDER_SCORE_MARGIN:
        reasons.append("candidate_not_unique")
    critical_conflicts = {
        "recording_identity_conflict",
        "release_identity_conflict",
        "duration_conflict",
        "title_conflict",
        "artist_conflict",
    }
    strict = (
        selected.candidate.provider_score >= HIGH_CONFIDENCE_PROVIDER_SCORE
        and bool(selected.candidate.recording_id)
        and selected.title_exact
        and selected.artist_exact
        and selected.duration_delta_seconds is not None
        and selected.duration_delta_seconds <= HIGH_CONFIDENCE_DURATION_DELTA_SECONDS
        and not selected.risk_flags
        and not (set(selected.conflicts) & critical_conflicts)
        and "candidate_not_unique" not in reasons
    )
    if strict:
        return MatchResult(
            MatchClassification.HIGH_CONFIDENCE,
            ("strict_high_confidence_match",),
            assessments,
            selected,
            unique_margin,
        )
    return MatchResult(
        MatchClassification.REVIEW,
        _ordered_reasons(reasons),
        assessments,
        selected,
        unique_margin,
    )


score_candidates = classify_candidates
