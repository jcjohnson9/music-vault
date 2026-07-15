"""Private local lyrics and consent-gated provider support for Music Vault."""

from .cache import (
    LYRICS_CACHE_SCHEMA_VERSION,
    LyricsCache,
    LyricsCacheError,
)
from .models import (
    CacheRecord,
    LyricLine,
    LyricsQuery,
    LyricsResult,
    LyricsSource,
    LyricsStatus,
    LyricsTrack,
    LookupState,
    ParsedLyrics,
    ProviderMatch,
    TrackLyricsIdentity,
)
from .parser import (
    LyricsParseError,
    normalize_plain_text,
    parse_lrc,
    parse_plain_text,
)
from .providers.base import LyricsProvider
from .providers.lrclib import LRCLIBProvider, SafeLyricsTransport
from .service import (
    EmbeddedLyrics,
    LyricsService,
    extract_embedded_lyrics,
    find_adjacent_sidecar,
    read_sidecar,
)

__all__ = [
    "CacheRecord",
    "EmbeddedLyrics",
    "LRCLIBProvider",
    "LYRICS_CACHE_SCHEMA_VERSION",
    "LyricLine",
    "LyricsCache",
    "LyricsCacheError",
    "LyricsParseError",
    "LyricsProvider",
    "LyricsQuery",
    "LyricsResult",
    "LyricsService",
    "LyricsSource",
    "LyricsStatus",
    "LyricsTrack",
    "LookupState",
    "ParsedLyrics",
    "ProviderMatch",
    "SafeLyricsTransport",
    "TrackLyricsIdentity",
    "extract_embedded_lyrics",
    "find_adjacent_sidecar",
    "normalize_plain_text",
    "parse_lrc",
    "parse_plain_text",
    "read_sidecar",
]
