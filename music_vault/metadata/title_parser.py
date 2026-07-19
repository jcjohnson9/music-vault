"""Pure, conservative YouTube source-title hint extraction.

The parser never changes the source observation.  It produces search hints
only; callers still need provider confidence (or an explicit
YouTube-exclusive policy) before applying the hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace


_SPACE_RE = re.compile(r"\s+")
_DASH_SEPARATOR_RE = re.compile(r"\s+(?:-|\u2013|\u2014)\s+")
_DATE_LIKE_RE = re.compile(
    r"[+-]?\d{1,4}(?:[-/.]\d{1,2}){0,2}",
    re.IGNORECASE,
)
_ARTIST_COLON_TITLE_RE = re.compile(r"^(?P<artist>[^:]{1,160}):\s+(?P<title>.+)$")
_TITLE_BY_ARTIST_RE = re.compile(
    r"^(?P<title>.+?)\s+by\s+(?P<artist>[^\[\]()]{1,160})$", re.IGNORECASE
)
_FEATURED_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+(?P<artist>.+?)(?=\s*[\[(]|$)",
    re.IGNORECASE,
)
_ARTIST_VERSION_SUFFIX_RE = re.compile(
    r"^(?P<artist>.+?)\s+(?P<label>"
    r"live\s+(?:at|from|in)\s+[^|:;]{2,100}"
    r"|(?:radio|studio|acoustic)\s+session(?:\s+(?:at|from|in)\s+[^|:;]{2,100})?"
    r"|tiny\s+desk(?:\s+concert)?"
    r")$",
    re.IGNORECASE,
)
_YEAR_SUFFIX_RE = re.compile(r"\s*[\[(](?P<year>(?:18|19|20)\d{2})[\])]\s*$")
_BRACKET_SUFFIX_RE = re.compile(r"\s*(?P<open>[\[(])(?P<label>[^\[\]()]{1,120})[\])]\s*$")
_DELIMITED_SUFFIX_RE = re.compile(
    r"\s+(?:-|\||:|\u2013|\u2014)\s*(?P<label>[^|:]{2,120})\s*$"
)

_PRESENTATION_NORMALIZED = frozenset(
    {
        "audio",
        "official audio",
        "official video",
        "official music video",
        "lyrics",
        "lyric video",
        "official lyric video",
        "hd",
        "hq",
        "visualizer",
        "visualiser",
        "official visualizer",
        "official visualiser",
        "music video",
    }
)
_PRESENTATION_DELIMITED_RE = re.compile(
    r"\s+(?:-|\||:|\u2013|\u2014)\s*"
    r"(?P<label>official\s+(?:audio|video|music\s+video|lyric\s+video|visuali[sz]er)"
    r"|lyrics?|lyric\s+video|music\s+video|visuali[sz]er|hd|hq)\s*$",
    re.IGNORECASE,
)

# Order is significant: specific forms must win over their component words.
_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("radio_edit", re.compile(r"\bradio\s+edit\b", re.IGNORECASE)),
    ("sped_up", re.compile(r"\bsped[ -]?up\b", re.IGNORECASE)),
    ("re_recording", re.compile(r"\bre[ -]?record(?:ed|ing)?\b", re.IGNORECASE)),
    ("live", re.compile(r"\blive(?:\s+(?:at|from)\b[^\])}]*)?", re.IGNORECASE)),
    ("remix", re.compile(r"\b(?:remix|\w+\s+mix|12[ -]?inch\s+mix)\b", re.IGNORECASE)),
    ("acoustic", re.compile(r"\bacoustic\b", re.IGNORECASE)),
    ("cover", re.compile(r"\bcover\b", re.IGNORECASE)),
    ("instrumental", re.compile(r"\binstrumental\b", re.IGNORECASE)),
    ("demo", re.compile(r"\bdemo\b", re.IGNORECASE)),
    ("extended", re.compile(r"\bextended(?:\s+(?:mix|version))?\b", re.IGNORECASE)),
    ("slowed", re.compile(r"\bslowed(?:\s*(?:and|&)\s*reverb)?\b", re.IGNORECASE)),
    ("nightcore", re.compile(r"\bnightcore\b", re.IGNORECASE)),
    ("mashup", re.compile(r"\bmash[ -]?up\b", re.IGNORECASE)),
    (
        "soundtrack",
        re.compile(
            r"\b(?:soundtrack|original\s+score|cast\s+(?:album|recording)"
            r"|(?:broadway|west\s+end|film|movie)\s+cast"
            r"|from\s+the\s+(?:motion\s+picture|film))\b",
            re.IGNORECASE,
        ),
    ),
    ("edit", re.compile(r"\bedit\b", re.IGNORECASE)),
)
_REMASTER_RE = re.compile(r"\b(?:\d{4}\s+)?remaster(?:ed)?\b", re.IGNORECASE)


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _presentation_key(value: str) -> str:
    return _SPACE_RE.sub(" ", value.casefold().replace("-", " ")).strip()


def _strip_presentation(value: str) -> tuple[str, tuple[str, ...]]:
    text = value
    removed: list[str] = []
    while text:
        bracket = _BRACKET_SUFFIX_RE.search(text)
        if bracket and _presentation_key(bracket.group("label")) in _PRESENTATION_NORMALIZED:
            removed.append(bracket.group("label").strip())
            text = text[: bracket.start()].rstrip(" -|:\u2013\u2014")
            continue
        delimited = _PRESENTATION_DELIMITED_RE.search(text)
        if delimited:
            removed.append(delimited.group("label").strip())
            text = text[: delimited.start()].rstrip(" -|:\u2013\u2014")
            continue
        break
    return _clean(text), tuple(reversed(removed))


def classify_version_hint(*values: object) -> tuple[str, str | None]:
    """Return an allowed version type and a preserved human-readable label."""

    text = " | ".join(_clean(value) for value in values if _clean(value))
    if not text:
        return "unknown", None

    labels = [
        match.group("label").strip()
        for match in _BRACKET_SUFFIX_RE.finditer(text)
        if _presentation_key(match.group("label")) not in _PRESENTATION_NORMALIZED
    ]
    for version_type, pattern in _VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            label = labels[-1] if labels else _clean(match.group(0))
            return version_type, label
    remaster = _REMASTER_RE.search(text)
    if remaster:
        # Remaster is a version label, not one of Music Vault's normalized
        # recording types.  Keep it visible while avoiding a fabricated type.
        return "unknown", labels[-1] if labels else _clean(remaster.group(0))
    return "unknown", labels[-1] if labels else None


def _without_version_suffix(value: str, version_label: str | None) -> str:
    if not version_label:
        return value
    bracket = _BRACKET_SUFFIX_RE.search(value)
    if bracket and _clean(bracket.group("label")).casefold() == _clean(version_label).casefold():
        return _clean(value[: bracket.start()])
    delimited = _DELIMITED_SUFFIX_RE.search(value)
    if delimited:
        candidate_label = _clean(delimited.group("label"))
        candidate_type, _ = classify_version_hint(candidate_label)
        recognized = candidate_type != "unknown" or bool(
            _REMASTER_RE.fullmatch(candidate_label)
        )
        if (
            recognized
            and candidate_label.casefold() == _clean(version_label).casefold()
        ):
            return _clean(value[: delimited.start()])
    return value


def _extract_featured(value: str) -> tuple[str, str | None]:
    match = _FEATURED_RE.search(value)
    if not match:
        return value, None
    featured = _clean(match.group("artist"))
    return _clean(value[: match.start()] + value[match.end() :]), featured or None


def _meaningful_dash_side(value: object) -> bool:
    text = _clean(value)
    return bool(text and any(character.isalnum() for character in text))


def _recognized_version_tail(value: object) -> bool:
    """Return whether a final dash segment is a performance/version suffix."""

    text = _clean(value)
    if not text:
        return False
    version_type, version_label = classify_version_hint(text)
    return bool(
        version_type != "unknown"
        or (version_label and _REMASTER_RE.search(text) is not None)
    )


def _dash_title_parts(value: object) -> tuple[str, str] | None:
    """Return one safe structural dash split, never an unbounded split set.

    Ordinary hyphens, numeric/date ranges, and ambiguous multi-dash strings
    are deliberately left untouched.  One extra trailing separator is allowed
    only when its final segment is an established version qualifier, preserving
    inputs such as ``Title - Artist - Live`` from the earlier parser contract.
    """

    text = _clean(value)
    separators = tuple(_DASH_SEPARATOR_RE.finditer(text))
    if not separators:
        return None
    if len(separators) > 2:
        return None
    if len(separators) == 2:
        tail = text[separators[1].end() :]
        if not _recognized_version_tail(tail):
            return None

    separator = separators[0]
    left = _clean(text[: separator.start()])
    right = _clean(text[separator.end() :])
    if not (_meaningful_dash_side(left) and _meaningful_dash_side(right)):
        return None
    if _DATE_LIKE_RE.fullmatch(left) and _DATE_LIKE_RE.fullmatch(right):
        return None
    return left, right


def split_artist_version_suffix(
    value: object,
) -> tuple[str, str, str] | None:
    """Split only an anchored, explicit performance suffix from an artist.

    This intentionally does not split punctuation, ampersands, collaborations,
    or ordinary words such as ``Live`` used without a venue/session phrase.
    """

    text = _clean(value)
    match = _ARTIST_VERSION_SUFFIX_RE.fullmatch(text)
    if match is None:
        return None
    artist = _clean(match.group("artist"))
    label = _clean(match.group("label"))
    if not artist or not label:
        return None
    return artist, classify_artist_version_label(label), label


def classify_artist_version_label(value: object) -> str:
    """Classify the anchored performance labels shared by parser and repair."""

    folded = _clean(value).casefold()
    if folded.startswith("acoustic session"):
        return "acoustic"
    if folded.startswith(("radio session", "studio session")):
        return "session"
    return "live"


STRONG_TITLE_PATTERNS = frozenset(
    {"artist_dash_title", "title_by_artist", "artist_colon_title"}
)


@dataclass(frozen=True)
class TitleOrientationHypothesis:
    """One non-authoritative interpretation of a source title.

    Dash-delimited source titles are intentionally represented both ways.
    Provider or embedded evidence, rather than the parser, selects the final
    orientation.
    """

    artist: str
    title: str
    orientation: str
    year_hint: int | None = None
    version_type: str = "unknown"
    version_label: str | None = None
    featured_artist: str | None = None
    source_pattern: str | None = None
    confidence_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedTitle:
    raw_title: str
    search_title: str
    artist_hint: str | None
    title_hint: str | None
    year_hint: int | None
    version_type: str
    version_label: str | None
    featured_artist_hint: str | None
    presentation_suffixes: tuple[str, ...]
    pattern: str | None
    orientation_hypotheses: tuple[TitleOrientationHypothesis, ...] = ()

    @property
    def strong_pattern(self) -> bool:
        return bool(
            self.artist_hint
            and self.title_hint
            and self.pattern in STRONG_TITLE_PATTERNS
        )

    # Small compatibility aliases for callers that prefer candidate names.
    @property
    def artist(self) -> str | None:
        return self.artist_hint

    @property
    def title(self) -> str | None:
        return self.title_hint

    @property
    def featured_artist(self) -> str | None:
        return self.featured_artist_hint

    @property
    def year(self) -> int | None:
        return self.year_hint

    def for_orientation(
        self, hypothesis: TitleOrientationHypothesis
    ) -> "ParsedTitle":
        """Return provider-search hints for one hypothesis without losing provenance."""

        version_type = hypothesis.version_type
        if version_type == "unknown" and self.version_type != "unknown":
            version_type = self.version_type
        return replace(
            self,
            search_title=hypothesis.title,
            artist_hint=hypothesis.artist,
            title_hint=hypothesis.title,
            year_hint=(
                hypothesis.year_hint
                if hypothesis.year_hint is not None
                else self.year_hint
            ),
            version_type=version_type,
            version_label=hypothesis.version_label or self.version_label,
            featured_artist_hint=(
                hypothesis.featured_artist or self.featured_artist_hint
            ),
            pattern=hypothesis.source_pattern or self.pattern,
        )


def parse_youtube_title(value: object) -> ParsedTitle:
    """Extract non-authoritative hints while preserving the exact raw value."""

    raw = str(value or "")
    working, presentation = _strip_presentation(_clean(raw))
    year: int | None = None
    year_match = _YEAR_SUFFIX_RE.search(working)
    if year_match:
        year = int(year_match.group("year"))
        working = _clean(working[: year_match.start()])

    artist: str | None = None
    title_part = working
    pattern: str | None = None
    orientation_hypotheses: tuple[TitleOrientationHypothesis, ...] = ()
    dash_parts = _dash_title_parts(working)
    if dash_parts:
        artist = dash_parts[0] or None
        title_part = dash_parts[1]
        pattern = "artist_dash_title"
    else:
        match = _TITLE_BY_ARTIST_RE.match(working)
        if match:
            artist = _clean(match.group("artist")) or None
            title_part = _clean(match.group("title"))
            pattern = "title_by_artist"
        else:
            match = _ARTIST_COLON_TITLE_RE.match(working)
            if match:
                artist = _clean(match.group("artist")) or None
                title_part = _clean(match.group("title"))
                pattern = "artist_colon_title"

    version_type, version_label = classify_version_hint(title_part)
    search_title = title_part
    title_without_feature, featured_from_title = _extract_featured(title_part)
    featured_from_artist: str | None = None
    if artist:
        artist, featured_from_artist = _extract_featured(artist)
        artist_version = split_artist_version_suffix(artist)
        if artist_version is not None:
            artist, artist_version_type, artist_version_label = artist_version
            if version_type == "unknown":
                version_type = artist_version_type
                version_label = artist_version_label
        artist = artist or None
    featured = featured_from_title or featured_from_artist
    title_hint = _without_version_suffix(title_without_feature, version_label)
    title_hint = _clean(title_hint) or None
    if pattern == "artist_dash_title" and artist and title_hint:
        # Provider queries must use the same cleaned artist/title identities as
        # the main parse. Otherwise a featured or live/version suffix on the
        # right-hand side poisons the reverse-orientation artist lookup.
        orientation_hypotheses = (
            TitleOrientationHypothesis(
                artist,
                title_hint,
                "left_is_artist",
                year,
                version_type,
                version_label,
                featured,
                pattern,
                ("conventional_dash_orientation",),
            ),
            TitleOrientationHypothesis(
                title_hint,
                artist,
                "right_is_artist",
                year,
                version_type,
                version_label,
                featured,
                pattern,
                ("alternate_dash_orientation",),
            ),
        )

    return ParsedTitle(
        raw_title=raw,
        search_title=search_title,
        artist_hint=artist,
        title_hint=title_hint,
        year_hint=year,
        version_type=version_type,
        version_label=version_label,
        featured_artist_hint=featured,
        presentation_suffixes=presentation,
        pattern=pattern,
        orientation_hypotheses=orientation_hypotheses,
    )


def title_orientation_hypotheses(value: object) -> tuple[TitleOrientationHypothesis, ...]:
    """Return both bounded dash orientations, or the one explicit parse.

    This helper performs no provider lookup and always keeps the raw source
    observation untouched.
    """

    parsed = value if isinstance(value, ParsedTitle) else parse_youtube_title(value)
    if parsed.orientation_hypotheses:
        return parsed.orientation_hypotheses
    if parsed.artist_hint and parsed.title_hint:
        return (
            TitleOrientationHypothesis(
                parsed.artist_hint,
                parsed.title_hint,
                parsed.pattern or "explicit",
                parsed.year_hint,
                parsed.version_type,
                parsed.version_label,
                parsed.featured_artist_hint,
                parsed.pattern,
                ("explicit_source_orientation",),
            ),
        )
    return ()


parse_title_hint = parse_youtube_title


__all__ = [
    "classify_artist_version_label",
    "ParsedTitle",
    "TitleOrientationHypothesis",
    "STRONG_TITLE_PATTERNS",
    "classify_version_hint",
    "parse_title_hint",
    "parse_youtube_title",
    "split_artist_version_suffix",
    "title_orientation_hypotheses",
]
