from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


_SAFE_ERROR_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")


def sanitized_provider_error(exc: BaseException) -> str:
    text = str(exc or "").strip().casefold()
    return text if _SAFE_ERROR_RE.fullmatch(text) else "provider_unavailable"


@dataclass(frozen=True)
class MetadataTaskResult:
    kind: str
    request_id: int
    value: Any = None
    error: str | None = None


class _Task(QRunnable):
    def __init__(
        self,
        *,
        kind: str,
        request_id: int,
        cancel_event: threading.Event,
        callback: Callable[[threading.Event], Any],
        deliver: Callable[[MetadataTaskResult], None],
    ) -> None:
        super().__init__()
        self.kind = kind
        self.request_id = request_id
        self.cancel_event = cancel_event
        self.callback = callback
        self.deliver = deliver

    def run(self) -> None:
        if self.cancel_event.is_set():
            return
        try:
            value = self.callback(self.cancel_event)
            result = MetadataTaskResult(self.kind, self.request_id, value=value)
        except Exception as exc:
            result = MetadataTaskResult(
                self.kind,
                self.request_id,
                error=sanitized_provider_error(exc),
            )
        if not self.cancel_event.is_set():
            self.deliver(result)


class MetadataTaskRunner(QObject):
    completed = Signal(object)

    def __init__(self, parent: QObject | None = None, *, max_workers: int = 2) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_workers)))
        self._next_request_id = 0
        self._active: dict[int, threading.Event] = {}
        self._closed = False
        self.completed.connect(self._forget)

    @property
    def pending_count(self) -> int:
        return len(self._active)

    def _forget(self, result: MetadataTaskResult) -> None:
        self._active.pop(int(result.request_id), None)

    def submit(
        self,
        kind: str,
        callback: Callable[[threading.Event], Any],
    ) -> int:
        if self._closed:
            raise RuntimeError("Metadata task runner is closed.")
        self._next_request_id += 1
        request_id = self._next_request_id
        cancel_event = threading.Event()
        self._active[request_id] = cancel_event
        self._pool.start(
            _Task(
                kind=kind,
                request_id=request_id,
                cancel_event=cancel_event,
                callback=callback,
                deliver=self.completed.emit,
            )
        )
        return request_id

    def cancel(self, request_id: int) -> None:
        event = self._active.pop(int(request_id), None)
        if event is not None:
            event.set()

    def cancel_all(self) -> None:
        for event in self._active.values():
            event.set()
        self._active.clear()
        self._pool.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_all()
