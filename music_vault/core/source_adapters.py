from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SourceItem:
    source_id: str
    title: str
    artist: str | None = None
    url: str | None = None
    license_note: str | None = None


class AuthorizedSourceAdapter(ABC):
    """
    Plugin interface for authorized music sources.

    Examples:
    - folder full of purchased downloads
    - public domain archive
    - Creative Commons catalog
    - your own uploads
    - artist-provided downloads
    - future Watchtower-controlled local drop folder

    This starter intentionally does not implement a copyrighted YouTube ripper.
    """

    @abstractmethod
    def scan(self) -> list[SourceItem]:
        raise NotImplementedError

    @abstractmethod
    def sync_new(self) -> int:
        raise NotImplementedError
