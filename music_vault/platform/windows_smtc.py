"""Optional Windows SMTC display/commands; never a player or a file reader.

WinRT imports and COM objects belong exclusively to the worker MTA. Native
callbacks cross into Qt as signals, and only the host decides what to play.
"""
from __future__ import annotations

from datetime import timedelta
import sys
import threading
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from music_vault.core.transport_actions import TransportSnapshot


MAX_ARTWORK_BYTES = 1024 * 1024
ARTWORK_WAIT_SECONDS = 2.0
CLOSE_WAIT_MS = 5000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A timed-out close must never destroy a still-running QThread. Callers retain
# their backend/window and retry close; this also protects accidental disposal.
_RETIRED_WORKERS: set[QThread] = set()


class _CleanupFailed(RuntimeError):
    """Native ownership could not be confirmed released; do not enable fallback."""


class _NativeSession:
    """All construction, publication and disposal occur on one MTA thread."""

    def __init__(self, hwnd: int, command: Callable[[str], None]):
        self._runtime = self._session = self._updater = self._token = None
        self._stream = self._reference = self._metadata = None
        self._initialized = False
        self._callback = None
        try:
            if sys.platform != "win32":
                raise RuntimeError("unsupported_platform")
            from winrt import runtime
            self._runtime = runtime
            runtime.init_apartment(runtime.ApartmentType.MULTI_THREADED)
            self._initialized = True
            from winrt.windows.foundation import AsyncStatus
            import winrt.windows.foundation.collections  # Required projected types.
            from winrt.windows.media import (
                MediaPlaybackStatus, MediaPlaybackType,
                SystemMediaTransportControlsButton,
                SystemMediaTransportControlsTimelineProperties,
            )
            from winrt.windows.media.interop import get_for_window
            from winrt.windows.storage.streams import (
                DataWriter, InMemoryRandomAccessStream, RandomAccessStreamReference,
            )
            self._status = MediaPlaybackStatus
            self._type = MediaPlaybackType
            self._timeline = SystemMediaTransportControlsTimelineProperties
            self._async_status = AsyncStatus
            self._writer_type = DataWriter
            self._stream_type = InMemoryRandomAccessStream
            self._reference_type = RandomAccessStreamReference
            buttons = {
                SystemMediaTransportControlsButton.PLAY: "play",
                SystemMediaTransportControlsButton.PAUSE: "pause",
                SystemMediaTransportControlsButton.NEXT: "next",
                SystemMediaTransportControlsButton.PREVIOUS: "previous",
            }

            def pressed(_sender, args):
                try:
                    action = buttons.get(args.button)
                    if action is not None:
                        command(action)
                except Exception:
                    # Never propagate a Python exception through a COM callback.
                    pass

            self._session = get_for_window(hwnd)
            self._session.is_enabled = False
            self._updater = self._session.display_updater
            self._callback = pressed
            self._token = self._session.add_button_pressed(pressed)
        except Exception:
            self.close()
            raise

    def _replace_artwork(self, payload: bytes, is_current):
        if len(payload) > MAX_ARTWORK_BYTES or not payload.startswith(_PNG_SIGNATURE):
            payload = b""
        stream = writer = operation = reference = None
        try:
            if payload:
                stream = self._stream_type()
                writer = self._writer_type(stream)
                writer.write_bytes(payload)
                operation = writer.store_async()
                if operation.wait(ARTWORK_WAIT_SECONDS) != self._async_status.COMPLETED:
                    operation.cancel()
                    raise TimeoutError("artwork_timeout")
                if operation.get_results() != len(payload):
                    raise RuntimeError("artwork_incomplete")
                writer.detach_stream()
                stream.seek(0)
                reference = self._reference_type.create_from_stream(stream)
            if not is_current():
                return False
            self._updater.thumbnail = reference
            old_stream = self._stream
            self._stream, self._reference = stream, reference
            stream = None
            if old_stream is not None:
                old_stream.close()
            return True
        finally:
            cleanup_failed = False
            for resource in (operation, writer, stream):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        cleanup_failed = True
            if cleanup_failed:
                raise RuntimeError("artwork_cleanup_failed")

    def publish(self, snapshot: TransportSnapshot, is_current=lambda: True):
        if not is_current():
            return
        session, updater = self._session, self._updater
        metadata = (
            snapshot.loaded, snapshot.track_id, snapshot.title, snapshot.artist,
            snapshot.album, snapshot.album_artist, snapshot.artwork,
        )
        if metadata != self._metadata:
            if snapshot.loaded:
                updater.type = self._type.MUSIC
                properties = updater.music_properties
                properties.title = snapshot.title[:1024]
                properties.artist = snapshot.artist[:1024]
                properties.album_title = snapshot.album[:1024]
                properties.album_artist = snapshot.album_artist[:1024]
                if not self._replace_artwork(snapshot.artwork, is_current):
                    self._metadata = None
                    return
            else:
                updater.clear_all()
                if self._stream is not None:
                    self._stream.close()
                self._stream = self._reference = None
            if not is_current():
                # The updater may hold staged properties; force a full refresh
                # even if the next snapshot returns to the previous identity.
                self._metadata = None
                return
            updater.update()
            self._metadata = metadata
        if not is_current():
            return
        session.is_play_enabled = snapshot.loaded
        session.is_pause_enabled = snapshot.loaded
        session.is_next_enabled = snapshot.can_next
        session.is_previous_enabled = snapshot.can_previous
        session.is_enabled = snapshot.loaded
        session.playback_status = (
            self._status.CLOSED if not snapshot.loaded else {
                "playing": self._status.PLAYING,
                "paused": self._status.PAUSED,
            }.get(snapshot.state, self._status.STOPPED)
        )
        duration = max(0, snapshot.duration_ms)
        timeline = self._timeline()
        timeline.start_time = timedelta(0)
        timeline.end_time = timedelta(milliseconds=duration)
        timeline.position = timedelta(milliseconds=min(duration, max(0, snapshot.position_ms)))
        timeline.min_seek_time = timedelta(0)
        timeline.max_seek_time = timedelta(milliseconds=duration)
        session.update_timeline_properties(timeline)

    def close(self):
        failed = False

        def attempt(action):
            nonlocal failed
            try:
                action()
            except Exception:
                failed = True

        session, updater = self._session, self._updater
        if session is not None:
            attempt(lambda: setattr(session, "is_enabled", False))
            if self._token is not None:
                attempt(lambda: session.remove_button_pressed(self._token))
            if hasattr(self, "_status"):
                attempt(lambda: setattr(session, "playback_status", self._status.CLOSED))
        if updater is not None:
            attempt(updater.clear_all)
            attempt(updater.update)
        if self._stream is not None:
            attempt(self._stream.close)
        # Release every projected instance before leaving its apartment.
        self._session = self._updater = self._token = None
        self._stream = self._reference = self._callback = self._metadata = None
        session = updater = None
        if self._initialized:
            self._initialized = False
            attempt(self._runtime.uninit_apartment)
        self._runtime = None
        if failed:
            raise _CleanupFailed("cleanup_failed")


class _SmtcWorker(QThread):
    command = Signal(str, int)
    availability = Signal(bool, int, str)

    def __init__(self, hwnd, generation, factory):
        super().__init__()  # Not parented: a pending worker must outlive its caller.
        self._hwnd = hwnd
        self._generation = generation
        self._factory = factory
        self._condition = threading.Condition()
        self._closing = False
        self._latest = None
        self._offered_revision = -1
        self._offered_sequence = 0
        self.cleanup_failed = False

    def offer(self, snapshot):
        with self._condition:
            # A revision identifies metadata, not position/state/capabilities.
            if not self._closing and snapshot.revision >= self._offered_revision:
                self._offered_revision = snapshot.revision
                self._offered_sequence += 1
                self._latest = snapshot
                self._condition.notify()

    def request_close(self):
        with self._condition:
            self._closing = True
            self._latest = None
            self._condition.notify()

    def _command(self, action):
        with self._condition:
            allowed = not self._closing and action in {"play", "pause", "next", "previous"}
        if allowed:
            self.command.emit(action, self._generation)

    def _is_current(self, sequence):
        with self._condition:
            return not self._closing and sequence == self._offered_sequence

    def run(self):
        session = None
        error = "unavailable"
        failure = None
        try:
            session = self._factory(self._hwnd, self._command)
            with self._condition:
                closing = self._closing
            if not closing:
                self.availability.emit(True, self._generation, "ready")
            error = "publish_failed"
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closing or self._latest is not None)
                    if self._closing:
                        break
                    snapshot, self._latest = self._latest, None
                    sequence = self._offered_sequence
                session.publish(snapshot, lambda: self._is_current(sequence))
        except _CleanupFailed:
            self.cleanup_failed = True
            failure = "cleanup_failed"
        except Exception:
            failure = error
        finally:
            self.request_close()
            if session is not None:
                try:
                    session.close()
                except Exception:
                    self.cleanup_failed = True
                    failure = "cleanup_failed"
            session = None
            # Hardware-key fallback is safe only after disposal, not while a
            # failing native session may still own and deliver the same key.
            if failure is not None:
                self.availability.emit(False, self._generation, failure)


class SmtcBackend(QObject):
    """One HWND generation. Its owner must close before destroying the HWND.

    An injected factory has signature ``(hwnd, command_callback) -> session``;
    session.publish(snapshot, is_current) and session.close() run only on the
    worker. The predicate rejects in-flight work superseded during artwork IO.
    """

    command = Signal(str, int)
    availability = Signal(bool, int, str)

    def __init__(self, parent=None, *, session_factory=None):
        super().__init__(parent)
        self._factory = session_factory or _NativeSession
        self._worker = None
        self._generation = 0
        self._closed = False
        self._available = False

    def start(self, hwnd: int, generation: int):
        if self._worker is not None or self._closed:
            return
        self._generation = generation
        if not hwnd:
            self.availability.emit(False, generation, "unavailable")
            return
        self._worker = _SmtcWorker(hwnd, generation, self._factory)
        self._worker.command.connect(self._on_command)
        self._worker.availability.connect(self._on_availability)
        self._worker.start()

    @Slot(str, int)
    def _on_command(self, command, generation):
        if not self._closed and self._available and generation == self._generation:
            self.command.emit(command, generation)

    @Slot(bool, int, str)
    def _on_availability(self, available, generation, code):
        if generation == self._generation and (not self._closed or not available):
            self._available = available
            self.availability.emit(available, generation, code)

    def publish(self, snapshot: TransportSnapshot):
        if not self._closed and self._worker is not None:
            self._worker.offer(snapshot)

    def close(self) -> bool:
        self._closed = True
        self._available = False
        if self._worker is None:
            return True
        self._worker.request_close()
        complete = self._worker.wait(CLOSE_WAIT_MS)
        if complete:
            _RETIRED_WORKERS.discard(self._worker)
            code = "cleanup_failed" if self._worker.cleanup_failed else "closed"
            self.availability.emit(False, self._generation, code)
        else:
            _RETIRED_WORKERS.add(self._worker)
            self.availability.emit(False, self._generation, "shutdown_pending")
        return complete
