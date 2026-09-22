"""Optional Windows surfaces over Music Vault's single playback authority."""
from __future__ import annotations

import ctypes
import sys
import time

from PySide6.QtCore import (
    QAbstractNativeEventFilter, QBuffer, QEvent, QIODevice, QObject,
    QSize, Qt, QTimer, Slot,
)
from PySide6.QtGui import QImageReader
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from music_vault.core.transport_actions import (
    TransportActions, TransportSnapshot, context_capabilities,
)


WM_APPCOMMAND = 0x0319
APP_COMMANDS = {11: "next", 12: "previous", 14: "toggle", 46: "play", 47: "pause"}


def _taskbar_message_id() -> int:
    if sys.platform != "win32":
        return 0
    try:
        register = ctypes.WinDLL("user32").RegisterWindowMessageW
        register.argtypes = [ctypes.c_wchar_p]
        register.restype = ctypes.c_uint32
        return int(register("TaskbarButtonCreated"))
    except Exception:
        return 0


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
        ("wparam", ctypes.c_size_t), ("lparam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32), ("point", _Point), ("private", ctypes.c_uint32),
    ]


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def nativeEventFilter(self, event_type, pointer):
        if bytes(event_type) not in {b"windows_generic_MSG", b"windows_dispatcher_MSG"}:
            return False, 0
        try:
            message = _Message.from_address(int(pointer))
            handled = self.controller.handle_native_message(
                int(message.hwnd or 0), message.message, message.wparam, message.lparam,
            )
            return handled, 1 if handled else 0
        except Exception:
            # Native callbacks must never unwind into Qt/Windows.
            self.controller.last_error = "native_message_failed"
            return False, 0


def _artwork_png(path) -> bytes:
    if not path:
        return b""
    reader = QImageReader(str(path))
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 64_000_000:
        return b""
    reader.setAutoTransform(True)
    reader.setScaledSize(size.scaled(QSize(512, 512), Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return b""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            return b""
        payload = bytes(buffer.data())
        return payload if len(payload) <= 1024 * 1024 else b""
    finally:
        buffer.close()


class WindowsTransportController(QObject):
    """GUI-thread snapshot/callback boundary; native failures are non-fatal.

    Tests inject backends and use temporary fixtures. The default is inert on
    non-Windows and offscreen Qt platforms, without importing WinRT at all.
    """

    def __init__(self, host, *, enabled=None, smtc_factory=None, taskbar_factory=None):
        super().__init__(host)
        self.host = host
        app = QApplication.instance()
        self.enabled = (
            sys.platform == "win32" and app.platformName() == "windows"
            if enabled is None else enabled
        )
        self._smtc_factory = smtc_factory
        self._taskbar_factory = taskbar_factory
        self.smtc = self.taskbar = None
        self.smtc_available = None
        self.last_error = ""
        self.hwnd = 0
        self.generation = 0
        self.closing = False
        self._revision = 0
        self._metadata_id = None
        self._metadata_dirty = True
        self._metadata = {}
        self._last_timeline = 0.0
        self._last_position = 0
        self._attach_pending = False
        self._retiring = []
        self._desired_hwnd = 0
        self._ready_hwnd = 0
        self._taskbar_message = (
            getattr(taskbar_factory, "taskbar_created_message", 0) or _taskbar_message_id()
        ) if self.enabled else 0
        self.actions = TransportActions(
            self.snapshot,
            toggle=host.toggle_loaded_playback_from_global_shortcut,
            next_track=host.play_next, previous_track=host.play_previous,
        )
        self._publish_timer = QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.timeout.connect(self._publish)
        self._reattach_timer = QTimer(self)
        self._reattach_timer.setSingleShot(True)
        self._reattach_timer.setInterval(100)
        self._reattach_timer.timeout.connect(self._finish_attach)
        self._native_filter = _NativeFilter(self)
        if self.enabled:
            app.installNativeEventFilter(self._native_filter)
            host.installEventFilter(self)
            host.player.positionChanged.connect(self._position_changed)
            for signal in (
                host.player.durationChanged, host.player.playbackStateChanged,
                host.player.mediaStatusChanged, host.player.sourceChanged,
            ):
                signal.connect(self.schedule_update)

    def eventFilter(self, watched, event):
        if watched is self.host and event.type() in {QEvent.Type.Show, QEvent.Type.WinIdChange}:
            if not self.closing and not self._attach_pending:
                self._attach_pending = True
                QTimer.singleShot(0, self._attach_visible_window)
        return False

    def _attach_visible_window(self):
        self._attach_pending = False
        if not self.closing and self.host.isVisible():
            self.attach(int(self.host.winId()))

    def attach(self, hwnd: int):
        if not self.enabled or self.closing or not hwnd or hwnd == self.hwnd:
            return
        self.generation += 1
        self.smtc_available = None
        self._desired_hwnd = hwnd
        self.hwnd = 0
        self._finish_attach()

    def _finish_attach(self):
        self._reattach_timer.stop()
        if self.closing or not self._desired_hwnd:
            return
        self._retire_backends()
        if self._retiring:
            # Never overlap two sessions when an old window's worker is still
            # unwinding. Keep it alive and retry, without forced termination.
            self._reattach_timer.start()
            return
        self.hwnd = self._desired_hwnd
        try:
            if self._smtc_factory is None:
                from music_vault.platform.windows_smtc import SmtcBackend
                self._smtc_factory = SmtcBackend
            self.smtc = self._smtc_factory()
            self.smtc.command.connect(self._command, Qt.ConnectionType.QueuedConnection)
            self.smtc.availability.connect(self._availability, Qt.ConnectionType.QueuedConnection)
            self.smtc.start(self.hwnd, self.generation)
        except Exception:
            self.last_error = "smtc_initialization_failed"
            self.smtc_available = False
        try:
            if self._taskbar_factory is None:
                from music_vault.platform.windows_taskbar import WindowsTaskbar
                self._taskbar_factory = WindowsTaskbar
            self.taskbar = self._taskbar_factory(self.hwnd)
            self._taskbar_message = self.taskbar.taskbar_created_message
            if self._ready_hwnd == self.hwnd:
                self._ready_hwnd = 0
                self.taskbar.taskbar_ready()
        except Exception:
            self.last_error = "taskbar_initialization_failed"
        self.schedule_update()

    @Slot(bool, int, str)
    def _availability(self, available, generation, error_code):
        if not self.closing and generation == self.generation:
            # Failed cleanup cannot establish exclusive fallback ownership.
            self.smtc_available = None if error_code == "cleanup_failed" else available
            if error_code and error_code != "ready":
                self.last_error = error_code

    @Slot(str, int)
    def _command(self, command, generation):
        if not self.closing and generation == self.generation:
            self._dispatch(command)

    def _dispatch(self, command):
        try:
            return self.actions.dispatch(command)
        except Exception:
            self.last_error = "transport_command_failed"
            return False
        finally:
            self.schedule_update()

    def handle_native_message(self, hwnd, message, wparam, lparam) -> bool:
        if not self.enabled or self.closing or not hwnd:
            return False
        if self._taskbar_message and message == self._taskbar_message:
            owned = hwnd in {self.hwnd, self._desired_hwnd} or hwnd == int(self.host.effectiveWinId())
            if owned:
                if hwnd == self.hwnd and self.taskbar is not None:
                    self.taskbar.taskbar_ready()
                    self.schedule_update()
                else:
                    # Windows sends readiness once. Preserve it through Show's
                    # queued attachment or a previous HWND's retiring worker.
                    self._ready_hwnd = hwnd
            return False
        if hwnd != self.hwnd:
            return False
        if self.taskbar is not None:
            command = self.taskbar.handle_message(message, wparam, lparam)
            if command:
                return self._dispatch(command)
        # Initialization-pending is not failure. Only a known unavailable SMTC
        # session enables foreground fallback, avoiding duplicate hardware Next.
        if message == WM_APPCOMMAND and self.smtc_available is False:
            command = APP_COMMANDS.get((int(lparam) >> 16) & 0x0FFF)
            if command:
                return self._dispatch(command)
        return False

    def invalidate_metadata(self):
        self._metadata_dirty = True
        self.schedule_update()

    def schedule_update(self, *_args):
        if self.enabled and not self.closing and not self._publish_timer.isActive():
            # setSource emits synchronously between current-ID and label updates.
            # Read only after the complete host transition, never halfway through.
            self._publish_timer.start(0)

    def _position_changed(self, position):
        now = time.monotonic()
        seek = abs(position - self._last_position) > 1500
        self._last_position = position
        if seek or now - self._last_timeline >= 5.0:
            self._last_timeline = now
            self.schedule_update()

    def snapshot(self) -> TransportSnapshot:
        player = self.host.player
        status = player.mediaStatus()
        loaded = not player.source().isEmpty() and status not in {
            QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia,
        }
        track_id = self.host.current_track_id if loaded else None
        if self._metadata_dirty or track_id != self._metadata_id:
            row = self.host.db.get_track(track_id) if track_id is not None else None
            metadata = {
                "title": str(row["title"] or "Untitled track"),
                "artist": str(row["artist"] or ""),
                "album": str(row["album"] or ""),
                "album_artist": str(row["album_artist"] or ""),
                "artwork": _artwork_png(row["cover_path"]),
            } if row is not None else {}
            # A failed read must never relabel track A's cached fields as B.
            # Commit the identity and cache together only after all work succeeds.
            self._metadata = metadata
            self._metadata_id = track_id
            self._metadata_dirty = False
            self._revision += 1
        state = {
            QMediaPlayer.PlaybackState.PlayingState: "playing",
            QMediaPlayer.PlaybackState.PausedState: "paused",
        }.get(player.playbackState(), "stopped") if loaded else "stopped"
        context = self.host.base_playback_context or {}
        next_enabled, previous_enabled = context_capabilities(
            context.get("track_ids", ()), context.get("current_track_id"),
            len(self.host.manual_queue), shuffle=self.host.shuffle_enabled,
            repeat=self.host.repeat_mode,
        )
        return TransportSnapshot(
            revision=self._revision, track_id=track_id, **self._metadata,
            loaded=loaded, state=state,
            position_ms=max(0, int(player.position())) if loaded else 0,
            duration_ms=max(0, int(player.duration())) if loaded else 0,
            can_next=next_enabled, can_previous=previous_enabled,
        )

    def _publish(self):
        if self.closing:
            return
        try:
            snapshot = self.snapshot()
        except Exception:
            self.last_error = "transport_snapshot_failed"
            self._revision += 1
            self._metadata_dirty = True
            snapshot = TransportSnapshot(revision=self._revision)
        for backend in (self.smtc, self.taskbar):
            if backend is not None:
                try:
                    backend.publish(snapshot)
                except Exception:
                    self.last_error = "native_publish_failed"

    def _retire_backends(self):
        if self.taskbar is not None:
            try:
                self.taskbar.close()
            except Exception:
                self.last_error = "taskbar_close_failed"
            self.taskbar = None
        if self.smtc is not None:
            self._retiring.append(self.smtc)
            self.smtc = None
        pending = []
        for backend in self._retiring:
            try:
                if not backend.close():
                    pending.append(backend)
            except Exception:
                pending.append(backend)
                self.last_error = "smtc_close_failed"
        self._retiring = pending

    def close(self) -> bool:
        self.closing = True
        self.actions.closed = True
        self._publish_timer.stop()
        self._reattach_timer.stop()
        if self.enabled:
            QApplication.instance().removeNativeEventFilter(self._native_filter)
            self.host.removeEventFilter(self)
        self._retire_backends()
        return not self._retiring
