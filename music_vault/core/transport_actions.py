"""Native controls delegate to the existing player; they never own playback."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TransportSnapshot:
    """Canonical display-only state, safe to hand to the operating system.

    Artwork is a bounded in-memory PNG. No media/artwork paths, provider IDs,
    credentials or database objects cross the native adapter boundary.
    """

    revision: int = 0
    track_id: int | None = None
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    artwork: bytes = b""
    loaded: bool = False
    state: str = "stopped"
    position_ms: int = 0
    duration_ms: int = 0
    can_next: bool = False
    can_previous: bool = False


def context_capabilities(
    track_ids, current_base_id, queue_count: int, *, shuffle: bool, repeat: str,
) -> tuple[bool, bool]:
    """Predict navigation without consulting the browsed selection or files."""
    ids = tuple(track_ids)
    try:
        index = ids.index(current_base_id)
    except ValueError:
        index = -1
    can_next = bool(queue_count or (
        ids and (shuffle or repeat == "all" or index < len(ids) - 1)
    ))
    can_previous = bool(ids and (index > 0 or repeat == "all"))
    return can_next, can_previous


class TransportActions:
    """Fresh state on every command, including repeated explicit Play/Pause."""

    def __init__(
        self, snapshot: Callable[[], TransportSnapshot], *,
        toggle: Callable[[], object], next_track: Callable[[], object],
        previous_track: Callable[[], object],
    ):
        self._snapshot = snapshot
        self._toggle = toggle
        self._next = next_track
        self._previous = previous_track
        self.closed = False

    def dispatch(self, command: str) -> bool:
        if self.closed:
            return False
        state = self._snapshot()
        if command in {"play", "pause", "toggle"}:
            if not state.loaded:
                return False
            if command == "play" and state.state == "playing":
                return True
            if command == "pause" and state.state != "playing":
                return True
            self._toggle()
            return True
        if command == "next" and state.can_next:
            self._next()
            return True
        if command == "previous" and state.can_previous:
            self._previous()
            return True
        return False
