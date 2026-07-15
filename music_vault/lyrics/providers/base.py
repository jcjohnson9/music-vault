"""Provider-neutral lyric lookup contract."""

from __future__ import annotations

import threading
from typing import Protocol

from ..models import LyricsQuery, LyricsResult


class LyricsProvider(Protocol):
    name: str

    def lookup(
        self,
        query: LyricsQuery,
        cancel_event: threading.Event | None = None,
    ) -> LyricsResult:
        """Resolve one current track without modifying media or application data."""


class LyricsProviderError(RuntimeError):
    """Sanitized provider failure; messages are stable codes, never response text."""


class UnsafeLyricsUrlError(LyricsProviderError):
    pass


class LyricsTemporaryError(LyricsProviderError):
    pass


class LyricsUnavailableError(LyricsProviderError):
    pass


class LyricsContentError(LyricsProviderError):
    pass
