"""Private listening evidence over an existing player, never playback control."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import math
import time
from typing import Callable, Mapping
import uuid


CHECKPOINT_MS = 30_000
MAX_SAMPLE_GAP_SECONDS = 5.0
MAX_RETRY_EVENTS = 32
MAX_WRITES_PER_FLUSH = 4
ORIGINS = {"manual", "manual_queue", "base", "repeat"}
END_REASONS = {"ended", "next", "previous", "replaced", "error", "stopped", "app_closed"}
_REASONS = {"end": "ended", "stop": "stopped", "close": "app_closed"}


def normalized_reason(reason: str) -> str:
    result = _REASONS.get(reason, reason)
    if result not in END_REASONS:
        raise ValueError("unsupported_history_end_reason")
    return result


def _text(value, limit=1024):
    return str(value or "")[:limit]


@dataclass(frozen=True)
class ListeningSnapshot:
    event_id: str
    run_id: str
    track_id: int
    recorded_track_id: int
    title_at_start: str
    artist_at_start: str
    album_at_start: str
    started_at: str | None = None
    last_observed_at: str | None = None
    ended_at: str | None = None
    qualified_at: str | None = None
    listened_ms: int = 0
    duration_ms: int | None = None
    end_reason: str | None = None
    playback_origin: str = "manual"
    context_kind: str | None = None
    context_playlist_id: int | None = None
    context_label: str | None = None
    update_sequence: int = 0

    def to_record(self) -> dict:
        return asdict(self)


class ListeningHistoryObserver:
    """Pure, owner-thread observation and bounded cumulative persistence.

    UTC labels observations; only the injected monotonic clock measures time.
    A failed sink never affects playback. At most 32 latest event snapshots are
    retained, with at most four writes per coarse flush (never tick retries).
    """

    def __init__(
        self, sink: Callable[[Mapping], bool], *, monotonic=None, utc_now=None,
        run_id=None, event_id_factory=None, changed=None, degraded=None,
        retry_capacity=MAX_RETRY_EVENTS,
    ):
        self._sink = sink
        self._monotonic = monotonic or time.monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.run_id = run_id or str(uuid.uuid4())
        self._event_id = event_id_factory or (lambda: str(uuid.uuid4()))
        self._changed = changed or (lambda: None)
        self._degraded = degraded or (lambda _value, _code: None)
        self._capacity = max(1, int(retry_capacity))
        self._pending: OrderedDict[str, ListeningSnapshot] = OrderedDict()
        self.generation = 0
        self.current: ListeningSnapshot | None = None
        self.expected_source = ""
        self._anchor = None
        self._active = False
        self._listened = 0.0
        self._checkpoint_at = 0
        self._degraded_state = (False, "")
        self.dropped_events = 0

    def _utc(self):
        value = self._utc_now()
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def pending_count(self):
        return len(self._pending)

    @property
    def is_degraded(self):
        return self._degraded_state[0]

    def prepare_track(self, row, *, source="", origin="manual", reason="replaced", context=None):
        row, context = dict(row), dict(context or {})
        track_id = int(row["id"])
        if track_id <= 0 or origin not in ORIGINS:
            raise ValueError("invalid_history_track_or_origin")
        self.finish(reason)
        self.generation += 1
        playlist_id = context.get("playlist_id")
        self.current = ListeningSnapshot(
            event_id=self._event_id(), run_id=self.run_id,
            track_id=track_id, recorded_track_id=track_id,
            title_at_start=_text(row.get("title")),
            artist_at_start=_text(row.get("artist")), album_at_start=_text(row.get("album")),
            playback_origin=origin,
            context_kind=_text(context.get("kind"), 64) or None,
            context_playlist_id=int(playlist_id) if playlist_id is not None else None,
            context_label=_text(context.get("label", context.get("playlist_name", context.get("name"))), 256) or None,
        )
        self.expected_source = str(source)
        self._anchor = None
        self._active = False
        self._listened = 0.0
        self._checkpoint_at = 0
        return self.generation

    def before_seek(self):
        self._anchor = None

    def observe(
        self, *, generation, track_id, source, position_ms, playing, usable=True,
        duration_ms=None, playback_rate=1.0,
    ):
        current = self.current
        if current is None or generation != self.generation:
            return
        if track_id != current.track_id or str(source) != self.expected_source:
            self._anchor = None
            self._active = False
            return
        if duration_ms is not None and duration_ms > 0:
            current = self.current = replace(current, duration_ms=int(duration_ms))
        active = bool(playing and usable)
        if not active:
            if self._active and current.started_at is not None:
                self._persist()
            self._active = False
            self._anchor = None
            return
        self._active = True
        now = float(self._monotonic())
        position = max(0, int(position_ms))
        previous, self._anchor = self._anchor, (now, position)
        if previous is None:
            return
        elapsed, delta = now - previous[0], position - previous[1]
        rate = float(playback_rate)
        if not math.isfinite(rate) or rate <= 0:
            return
        expected = elapsed * rate * 1000
        if not 0 < elapsed <= MAX_SAMPLE_GAP_SECONDS or delta <= 0:
            return
        # Explicit seeks clear the anchor. Also reject unannounced jumps, rather
        # than crediting a seek-to-end as an entire meaningful listen.
        if delta > expected + max(750, expected * 0.5):
            return
        self._listened += min(delta, expected)
        listened = int(self._listened + 1e-6)  # Avoid losing 1 ms to float summation.
        if listened <= current.listened_ms:
            return
        utc = self._utc()
        started = current.started_at is None
        threshold = min(CHECKPOINT_MS, current.duration_ms / 2) if current.duration_ms else CHECKPOINT_MS
        qualified = current.qualified_at is None and listened >= threshold
        self.current = replace(
            current, started_at=current.started_at or utc, last_observed_at=utc,
            listened_ms=listened, qualified_at=(utc if qualified else current.qualified_at),
        )
        if started or qualified or listened - self._checkpoint_at >= CHECKPOINT_MS:
            self._persist()

    def finish(self, reason):
        reason = normalized_reason(reason)
        current = self.current
        if current is not None and current.started_at is not None:
            self.current = replace(current, ended_at=self._utc(), end_reason=reason)
            self._persist()
        self.current = None
        self._active = False
        self._anchor = None

    def _status(self, value, code):
        state = (value, code)
        if state != self._degraded_state:
            self._degraded_state = state
            self._degraded(*state)

    def _persist(self):
        if self.current is None or self.current.started_at is None:
            return
        self.current = replace(self.current, update_sequence=self.current.update_sequence + 1)
        self._checkpoint_at = self.current.listened_ms
        event_id = self.current.event_id
        if event_id not in self._pending and len(self._pending) >= self._capacity:
            self._pending.popitem(last=False)
            self.dropped_events += 1
        self._pending[event_id] = self.current
        self.retry_pending()

    def retry_pending(self):
        changed = False
        for event_id in list(self._pending)[:MAX_WRITES_PER_FLUSH]:
            try:
                changed = bool(self._sink(self._pending[event_id].to_record())) or changed
            except Exception:
                self._status(True, "history_incomplete" if self.dropped_events else "history_write_failed")
                break
            else:
                del self._pending[event_id]
        if self.dropped_events:
            self._status(True, "history_incomplete")
        elif not self._pending:
            self._status(False, "")
        if changed:
            self._changed()
        return not self._pending and not self.dropped_events
