from __future__ import annotations

from collections.abc import Callable
from queue import Empty, SimpleQueue
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot


class _BrowserSummaryJob(QRunnable):
    def __init__(
        self,
        kind: str,
        token: int,
        revision: object,
        query: Callable[[], object],
        completions: SimpleQueue,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.token = token
        self.revision = revision
        self.query = query
        self.completions = completions
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.query()
        except Exception:
            # Browser errors are intentionally neutral. In particular, do not
            # pass SQLite exception text containing a private database path to
            # the user interface.
            self.completions.put(
                (
                    False,
                    self.kind,
                    self.token,
                    self.revision,
                    f"The {self.kind} browser could not be loaded.",
                )
            )
            return
        self.completions.put(
            (True, self.kind, self.token, self.revision, result)
        )


class BrowserSummaryLoader(QObject):
    """Run browser-summary queries off the GUI thread with stale-result guards."""

    loaded = Signal(str, int, object, object)
    failed = Signal(str, int, object, str)

    def __init__(self, parent: QObject | None = None, *, max_workers: int = 2) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, min(int(max_workers), 2)))
        self._tokens: dict[str, int] = {}
        self._jobs: dict[tuple[str, int], _BrowserSummaryJob] = {}
        self._completions: SimpleQueue = SimpleQueue()
        self._completion_timer = QTimer(self)
        self._completion_timer.setInterval(15)
        self._completion_timer.timeout.connect(self._drain_completions)
        self._completion_timer.start()
        self._closed = False

    def request(
        self,
        kind: str,
        revision: object,
        query: Callable[[], object],
    ) -> int:
        if self._closed:
            return -1
        normalized_kind = str(kind).strip().casefold()
        token = self._tokens.get(normalized_kind, 0) + 1
        self._tokens[normalized_kind] = token
        job = _BrowserSummaryJob(
            normalized_kind,
            token,
            revision,
            query,
            self._completions,
        )
        key = (normalized_kind, token)
        self._jobs[key] = job
        self._pool.start(job)
        return token

    def current_token(self, kind: str) -> int:
        return self._tokens.get(str(kind).strip().casefold(), 0)

    def is_current(self, kind: str, token: int) -> bool:
        return not self._closed and self.current_token(kind) == int(token)

    def invalidate(self, kind: str | None = None) -> None:
        if kind is None:
            for browser_kind in tuple(self._tokens):
                self._tokens[browser_kind] += 1
            return
        normalized_kind = str(kind).strip().casefold()
        self._tokens[normalized_kind] = self._tokens.get(normalized_kind, 0) + 1

    @Slot()
    def _drain_completions(self) -> None:
        while True:
            try:
                succeeded, kind, token, revision, payload = self._completions.get_nowait()
            except Empty:
                return
            self._jobs.pop((kind, token), None)
            if not self.is_current(kind, token):
                continue
            if succeeded:
                self.loaded.emit(kind, token, revision, payload)
            else:
                self.failed.emit(kind, token, revision, str(payload))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.invalidate()
        self._completion_timer.stop()
        self._pool.clear()
        self._jobs.clear()
