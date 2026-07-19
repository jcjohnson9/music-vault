"""Pure soundtrack context classification for metadata acceptance and grouping.

The classifier consumes normalized, already available metadata.  It performs no
provider lookup and deliberately keeps ``Various Artists`` as release context,
never as a performer identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


_SPACE_RE = re.compile(r"\s+")


class SoundtrackKind(str, Enum):
    NONE = "none"
    SOUNDTRACK = "soundtrack"
    GAME_SOUNDTRACK = "game_soundtrack"
    FILM_SOUNDTRACK = "film_soundtrack"
    TELEVISION_SOUNDTRACK = "television_soundtrack"
    SCORE = "score"
    STAGE_CAST = "stage_cast_recording"
    FILM_CAST = "film_cast_recording"
    MUSICAL_SOUNDTRACK = "musical_soundtrack"
    CHARACTER_PERFORMANCE = "character_performance"


@dataclass(frozen=True)
class SoundtrackClassification:
    kind: SoundtrackKind
    album_kind: str
    is_soundtrack: bool
    various_artists_release_context: bool
    evidence: tuple[str, ...]


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _credit_names(credits: Iterable[object]) -> tuple[str, ...]:
    names: list[str] = []
    for credit in credits:
        if isinstance(credit, Mapping):
            value = credit.get("name", credit.get("display_name", ""))
        else:
            value = getattr(credit, "name", getattr(credit, "display_name", ""))
        cleaned = _clean(value)
        if cleaned:
            names.append(cleaned)
    return tuple(names)


def is_various_artists(value: object) -> bool:
    key = re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()
    return key in {"various artists", "various", "v a"}


def classify_soundtrack(
    *,
    title: object = None,
    album: object = None,
    version_type: object = None,
    source_title: object = None,
    release_type: object = None,
    release_format: object = None,
    album_artist: object = None,
    provider_credits: Iterable[object] = (),
) -> SoundtrackClassification:
    """Classify soundtrack context without deciding performer identity.

    Specific score/cast contexts win over the generic word ``soundtrack`` so
    film scores and stage casts cannot collapse into a songs soundtrack.
    """

    values = {
        "title": _clean(title),
        "album": _clean(album),
        "source_title": _clean(source_title),
        "release_type": _clean(release_type),
        "release_format": _clean(release_format),
        "version_type": _clean(version_type),
    }
    haystack = " | ".join(value for value in values.values() if value).casefold()
    evidence: list[str] = []

    def has(pattern: str) -> bool:
        return re.search(pattern, haystack, re.IGNORECASE) is not None

    if has(r"\b(?:original\s+)?(?:broadway|west\s+end|stage|theatre|theater)\s+cast\b"):
        kind = SoundtrackKind.STAGE_CAST
        evidence.append("stage_cast_phrase")
    elif has(r"\b(?:motion\s+picture|film|movie)\s+cast\b"):
        kind = SoundtrackKind.FILM_CAST
        evidence.append("film_cast_phrase")
    elif has(r"\b(?:original\s+)?cast\s+(?:album|recording)\b"):
        kind = SoundtrackKind.STAGE_CAST
        evidence.append("cast_recording_phrase")
    elif has(r"\b(?:original\s+)?(?:motion\s+picture|film|television|tv|game|video\s+game)?\s*score\b"):
        kind = SoundtrackKind.SCORE
        evidence.append("score_phrase")
    elif has(r"\b(?:video\s+game|game)\s+(?:original\s+)?soundtrack\b") or has(
        r"\b(?:ost|soundtrack)\b.*\b(?:video\s+game|game)\b"
    ):
        kind = SoundtrackKind.GAME_SOUNDTRACK
        evidence.append("game_soundtrack_phrase")
    elif has(r"\b(?:television|tv|series)\s+(?:original\s+)?soundtrack\b"):
        kind = SoundtrackKind.TELEVISION_SOUNDTRACK
        evidence.append("television_soundtrack_phrase")
    elif has(r"\b(?:motion\s+picture|film|movie)\s+(?:original\s+)?soundtrack\b") or has(
        r"\bfrom\s+the\s+(?:motion\s+picture|film)\b"
    ):
        kind = SoundtrackKind.FILM_SOUNDTRACK
        evidence.append("film_soundtrack_phrase")
    elif has(r"\bmusical\s+(?:original\s+)?soundtrack\b"):
        kind = SoundtrackKind.MUSICAL_SOUNDTRACK
        evidence.append("musical_soundtrack_phrase")
    elif has(r"\b(?:performed\s+by|as\s+performed\s+by)\b.*\bcharacter\b"):
        kind = SoundtrackKind.CHARACTER_PERFORMANCE
        evidence.append("character_performance_phrase")
    elif values["version_type"].casefold() == "soundtrack" or has(r"\b(?:soundtrack|\bost\b)\b"):
        kind = SoundtrackKind.SOUNDTRACK
        evidence.append("generic_soundtrack_phrase")
    else:
        kind = SoundtrackKind.NONE

    if kind is SoundtrackKind.SCORE:
        album_kind = "score"
    elif kind in {SoundtrackKind.STAGE_CAST, SoundtrackKind.FILM_CAST}:
        album_kind = "cast_recording"
    elif kind is SoundtrackKind.NONE:
        album_kind = "unknown"
    else:
        album_kind = "soundtrack"

    credits = _credit_names(provider_credits)
    various = is_various_artists(album_artist) or any(is_various_artists(name) for name in credits)
    if various:
        evidence.append("various_artists_release_context")
    return SoundtrackClassification(
        kind=kind,
        album_kind=album_kind,
        is_soundtrack=kind is not SoundtrackKind.NONE,
        various_artists_release_context=various,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def soundtrack_search_variants(
    *,
    track_title: object,
    artist: object = None,
    work_title: object = None,
    composer: object = None,
    limit: int = 6,
) -> tuple[tuple[str, str | None], ...]:
    """Return bounded, deduplicated catalogue queries for soundtrack work.

    The result is data only; constructing it never contacts a provider.
    """

    title = _clean(track_title)
    if not title or limit < 1:
        return ()
    artist_name = _clean(artist) or None
    work = _clean(work_title)
    composer_name = _clean(composer) or None
    candidates: list[tuple[str, str | None]] = [(title, artist_name)]
    if work:
        candidates.extend(
            (
                (f"{title} {work}", artist_name),
                (f"{title} Original Soundtrack", artist_name),
                (f"{work} soundtrack", None),
            )
        )
    if composer_name:
        candidates.append((title, composer_name))
    result: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for query_title, query_artist in candidates:
        key = (query_title.casefold(), (query_artist or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append((query_title, query_artist))
        if len(result) >= min(6, int(limit)):
            break
    return tuple(result)


__all__ = [
    "SoundtrackClassification",
    "SoundtrackKind",
    "classify_soundtrack",
    "is_various_artists",
    "soundtrack_search_variants",
]
