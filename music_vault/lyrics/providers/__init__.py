"""Music Vault lyric provider interfaces."""

from .base import (
    LyricsContentError,
    LyricsProvider,
    LyricsProviderError,
    LyricsTemporaryError,
    LyricsUnavailableError,
    UnsafeLyricsUrlError,
)

__all__ = [
    "LyricsContentError",
    "LyricsProvider",
    "LyricsProviderError",
    "LyricsTemporaryError",
    "LyricsUnavailableError",
    "UnsafeLyricsUrlError",
]
