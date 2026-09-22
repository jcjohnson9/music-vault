"""Occurrence-safe edits over the player's existing FIFO queue.

This module does not own playback or another queue. The host's list remains the
authority; tokens identify duplicate occurrences only while that list is known.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class QueueEntry:
    token: int
    track_id: int


@dataclass(frozen=True)
class QueueSnapshot:
    revision: int
    entries: tuple[QueueEntry, ...]
    can_undo: bool


class ManualQueueEditor:
    """Mutate one host-owned list in place, rejecting stale occurrence edits.

    Undo restores just the most recent edit. Appending, consuming, or observing
    an external mutation expires it so already-played songs cannot reappear.
    All calls belong on the same thread as the host's playback transitions.
    """

    def __init__(self, queue_getter: Callable[[], list[int]]) -> None:
        self._queue_getter = queue_getter
        self._bound_queue: list[int] | None = None
        self._entries: tuple[QueueEntry, ...] = ()
        self._revision = 0
        self._next_token = 1
        self._undo_entries: tuple[QueueEntry, ...] | None = None
        self._synchronize()

    def _new_entry(self, track_id: int) -> QueueEntry:
        entry = QueueEntry(self._next_token, track_id)
        self._next_token += 1
        return entry

    def _synchronize(self) -> list[int]:
        queue = self._queue_getter()
        if not isinstance(queue, list):
            raise TypeError("The manual queue must be the host's list.")
        if queue is not self._bound_queue or tuple(queue) != tuple(
            entry.track_id for entry in self._entries
        ):
            self._bound_queue = queue
            self._entries = tuple(self._new_entry(track_id) for track_id in queue)
            self._undo_entries = None
            self._revision += 1
        return queue

    def snapshot(self) -> QueueSnapshot:
        self._synchronize()
        return QueueSnapshot(self._revision, self._entries, self._undo_entries is not None)

    def append(self, track_id: int) -> QueueSnapshot:
        queue = self._synchronize()
        queue.append(track_id)
        self._entries += (self._new_entry(track_id),)
        self._undo_entries = None
        self._revision += 1
        return self.snapshot()

    def pop_next(self) -> int | None:
        queue = self._synchronize()
        if not queue:
            return None
        track_id = queue.pop(0)
        self._entries = self._entries[1:]
        self._undo_entries = None
        self._revision += 1
        return track_id

    def _edit(self, entries: tuple[QueueEntry, ...]) -> bool:
        if entries == self._entries:
            return False
        self._undo_entries = self._entries
        self._bound_queue[:] = [entry.track_id for entry in entries]
        self._entries = entries
        self._revision += 1
        return True

    def remove(self, token: int, expected_revision: int) -> bool:
        self._synchronize()
        if expected_revision != self._revision:
            return False
        return self._edit(tuple(entry for entry in self._entries if entry.token != token))

    def move(self, token: int, before_token: int | None, expected_revision: int) -> bool:
        self._synchronize()
        if expected_revision != self._revision or token == before_token:
            return False
        entries = list(self._entries)
        moving = next((entry for entry in entries if entry.token == token), None)
        if moving is None:
            return False
        entries.remove(moving)
        if before_token is None:
            destination = len(entries)
        else:
            destination = next(
                (index for index, entry in enumerate(entries) if entry.token == before_token),
                None,
            )
            if destination is None:
                return False
        entries.insert(destination, moving)
        return self._edit(tuple(entries))

    def clear(self, expected_revision: int) -> bool:
        self._synchronize()
        if expected_revision != self._revision:
            return False
        return self._edit(())

    def undo(self, expected_revision: int) -> bool:
        self._synchronize()
        if expected_revision != self._revision or self._undo_entries is None:
            return False
        entries = self._undo_entries
        self._undo_entries = None
        self._bound_queue[:] = [entry.track_id for entry in entries]
        self._entries = entries
        self._revision += 1
        return True


@dataclass(frozen=True)
class ContinuationPreview:
    track_ids: tuple[int, ...]
    mode: str
    automatic: bool
    repeat_one_blocks: bool
    wraps: bool
    explanation: str
    queue_explanation: str


def preview_continuation(
    captured_ids: Sequence[int],
    current_base_id: int | None,
    shuffle: bool,
    autoplay: bool,
    repeat: str,
    has_current: bool,
) -> ContinuationPreview:
    """Describe existing transport decisions without selecting or playing music.

    A shuffle list is an unordered pool, never an invented upcoming order.
    Repeat One blocks automatic progression but the explicit Next button does
    not. The current *base* song, not a queued interruption, anchors resumption.
    """

    ids = tuple(captured_ids)
    blocked = bool(has_current and repeat == "one")
    if blocked:
        queue_explanation = (
            "Repeat One keeps the current track playing. Turn it off to continue "
            "automatically, or use Next to play the first queued track."
        )
    elif has_current:
        queue_explanation = (
            "Plays in this order before the original context, even when Auto is off."
        )
    else:
        queue_explanation = "Nothing is playing. Use Next to start the first queued track."

    wraps = False
    if not ids:
        candidates: tuple[int, ...] = ()
        mode = "empty"
        explanation = "No captured playback context. Play a track to establish one."
    elif shuffle:
        candidates = tuple(track_id for track_id in ids if track_id != current_base_id) or ids
        mode = "shuffle"
        explanation = "Shuffle chooses from this context. The next track is not chosen yet."
    else:
        mode = "sequential"
        try:
            index = ids.index(current_base_id)
        except ValueError:
            index = -1
        candidates = ids[index + 1 :]
        if repeat == "all" and index >= 0:
            candidates += ids[: index + 1]
            wraps = True
        if not candidates:
            explanation = "End of this playback context."
        elif repeat == "all":
            explanation = "Continues in context order, then Repeat All wraps to the start."
        elif not autoplay:
            explanation = "Auto is off. After queued tracks finish, use Next to continue here."
        else:
            explanation = "Resumes after the last song played from this context."

    automatic = bool(
        has_current and not blocked and candidates and (shuffle or autoplay or repeat == "all")
    )
    if blocked and candidates:
        explanation = "Repeat One blocks automatic continuation. Next bypasses it. " + explanation
    elif not has_current and candidates:
        explanation = "Nothing is playing; this is the captured context for Next. " + explanation
    return ContinuationPreview(
        candidates, mode, automatic, blocked, wraps, explanation, queue_explanation
    )
