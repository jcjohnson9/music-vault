"""Thin Qt observation bridge over the host's one authoritative player."""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer

from music_vault.core.listening_history import ListeningHistoryObserver


class ListeningHistoryBridge(QObject):
    changed = Signal()
    degraded = Signal(bool, str)

    def __init__(self, host, store, *, monotonic=None, utc_now=None):
        super().__init__(host)
        self.host = host
        self.player = host.player
        self.observer = ListeningHistoryObserver(
            store.save_event, monotonic=monotonic, utc_now=utc_now,
            changed=self.changed.emit, degraded=self.degraded.emit,
        )
        self._connections = []
        self._intents = []
        self._row = None
        self._context = None
        self._closed = False
        self._empty_check_generation = None

    @property
    def run_id(self):
        return self.observer.run_id

    @contextmanager
    def intent(self, *, reason=None, origin=None):
        self._intents.append((reason, origin))
        try:
            yield
        finally:
            self._intents.pop()

    def _intent_value(self, index, fallback):
        return next((entry[index] for entry in reversed(self._intents) if entry[index] is not None), fallback)

    def prepare_track(self, row, origin=None, reason=None, context=None):
        if self._closed:
            return self.observer.generation
        self._row = dict(row)
        self._context = dict(context or {})
        generation = self.observer.prepare_track(
            row, source=QUrl.fromLocalFile(str(self._row["path"])).toString(),
            origin=origin or self._intent_value(1, "manual"),
            reason=reason or self._intent_value(0, "replaced"), context=context,
        )
        self._disconnect()
        for signal, callback in (
            (self.player.positionChanged, lambda value, g=generation: self._position(g, value)),
            (self.player.durationChanged, lambda _value, g=generation: self._sample(g)),
            (self.player.playbackStateChanged, lambda _value, g=generation: self._sample(g)),
            (self.player.mediaStatusChanged, lambda value, g=generation: self._media_status(g, value)),
            (self.player.sourceChanged, lambda value, g=generation: self._source_changed(g, value)),
        ):
            signal.connect(callback)
            self._connections.append((signal, callback))
        return generation

    def repeat_current(self, context=None):
        if self._row is not None and not self._closed:
            self.observer.finish("ended")
            return self.prepare_track(
                self._row, origin="repeat", reason="ended",
                context=self._context if context is None else context,
            )
        return self.observer.generation

    def before_seek(self):
        if not self._closed:
            self.observer.before_seek()

    def finish(self, reason):
        if not self._closed:
            self.observer.finish(reason)

    def _position(self, generation, position):
        # Queued signals can outlive a disconnected source; do not interpret an
        # old payload as the current player's progress.
        if position == self.player.position():
            self._sample(generation)

    def _media_status(self, generation, status):
        if self._closed or generation != self.observer.generation or status != self.player.mediaStatus():
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            if self._matches_source():
                self.observer.finish("error")
            return
        if status == QMediaPlayer.MediaStatus.NoMedia:
            if self.observer.current is not None and self.observer.current.started_at is not None:
                self.observer.finish("error")
            else:
                self._defer_empty_source(generation)
            return
        # Host finalizes EndOfMedia BEFORE it makes its existing queue/repeat
        # choice. A stopped-state signal alone must never invent an end reason.
        self._sample(generation)

    def _source_changed(self, generation, source):
        if self._closed or generation != self.observer.generation or source != self.player.source():
            return
        if source.isEmpty():
            self._defer_empty_source(generation)
        self._sample(generation)

    def _defer_empty_source(self, generation):
        # setSource can synchronously clear the old source before accepting the
        # new one. Cancel a pending occurrence only if it is still genuinely
        # empty after that transition, never from the intermediate signal.
        if self._empty_check_generation == generation:
            return
        self._empty_check_generation = generation

        def check():
            if self._empty_check_generation == generation:
                self._empty_check_generation = None
            if (
                not self._closed and generation == self.observer.generation
                and self.player.source().isEmpty()
                and self.player.mediaStatus() == QMediaPlayer.MediaStatus.NoMedia
            ):
                self.observer.finish("error")

        QTimer.singleShot(0, self, check)

    def _matches_source(self):
        current = self.observer.current
        return (
            current is not None
            and getattr(self.host, "current_track_id", None) == current.track_id
            and self.player.source().toString() == self.observer.expected_source
        )

    def _sample(self, generation):
        if self._closed or generation != self.observer.generation:
            return
        status = self.player.mediaStatus()
        self.observer.observe(
            generation=generation, track_id=getattr(self.host, "current_track_id", None),
            source=self.player.source().toString(), position_ms=self.player.position(),
            playing=self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
            usable=status in {QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia},
            duration_ms=self.player.duration(), playback_rate=self.player.playbackRate(),
        )

    def _disconnect(self):
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()

    def accepted_close(self):
        if not self._closed:
            self.observer.finish("app_closed")
            self._closed = True
            self._disconnect()
        return self.observer.retry_pending()
