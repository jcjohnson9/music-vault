from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QWidget

from music_vault.ui import windows_transport as transport


class Player(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    sourceChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.url = QUrl("file:///synthetic.wav")
        self.status = QMediaPlayer.MediaStatus.LoadedMedia
        self.state = QMediaPlayer.PlaybackState.PausedState
        self.at = 0

    def source(self): return self.url
    def mediaStatus(self): return self.status
    def playbackState(self): return self.state
    def position(self): return self.at
    def duration(self): return 20_000


class Smtc(QObject):
    command = Signal(str, int)
    availability = Signal(bool, int, str)

    def __init__(self):
        super().__init__()
        self.snapshots = []
        self.closed = 0
        self.can_close = True

    def start(self, hwnd, generation): self.started = (hwnd, generation)
    def publish(self, snapshot): self.snapshots.append(snapshot)
    def close(self):
        self.closed += 1
        return self.can_close


class Taskbar:
    taskbar_created_message = 4999

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.epochs = 0
        self.closed = 0
        self.snapshots = []

    def publish(self, snapshot): self.snapshots.append(snapshot)
    def taskbar_ready(self): self.epochs += 1
    def handle_message(self, message, wparam, lparam):
        return "next" if message == 999 else None
    def close(self): self.closed += 1


@pytest.fixture
def native_host(qapp):
    host = QWidget()
    host.player = Player()
    host.current_track_id = 1
    host.base_playback_context = {"track_ids": [1, 2, 3], "current_track_id": 1}
    host.manual_queue = []
    host.shuffle_enabled = False
    host.repeat_mode = "off"
    host.db_reads = []
    host.row = dict(title="Canonical title", artist="Canonical artist", album="Album",
                    album_artist="Album artist", cover_path=None, path="never-export-this-path")
    def get_track(track_id):
        host.db_reads.append(track_id)
        return host.row
    host.db = SimpleNamespace(get_track=get_track)
    host.calls = []
    def toggle():
        host.calls.append("toggle")
        host.player.state = (QMediaPlayer.PlaybackState.PausedState
                             if host.player.state == QMediaPlayer.PlaybackState.PlayingState
                             else QMediaPlayer.PlaybackState.PlayingState)
    host.toggle_loaded_playback_from_global_shortcut = toggle
    host.play_next = lambda: host.calls.append("next")
    host.play_previous = lambda: host.calls.append("previous")
    controller = transport.WindowsTransportController(
        host, enabled=True, smtc_factory=Smtc, taskbar_factory=Taskbar,
    )
    host.transport = controller
    controller.attach(100)
    yield host
    for backend in [controller.smtc, *controller._retiring]:
        if backend is not None:
            backend.can_close = True
    assert controller.close()
    host.close()
    host.deleteLater()
    qapp.processEvents()


def test_metadata_cache_uses_playing_identity_not_browsed_selection(native_host):
    host = native_host
    controller = host.transport
    first = controller.snapshot()
    host.selected_track_id = lambda: pytest.fail("must not read selection")
    for position in range(100):
        host.player.at = position
        assert controller.snapshot().title == "Canonical title"
    assert host.db_reads == [1]
    assert "never-export-this-path" not in repr(first)
    host.row["title"] = "Corrected canonical title"
    controller.invalidate_metadata()
    corrected = controller.snapshot()
    assert corrected.title == "Corrected canonical title"
    assert corrected.revision > first.revision
    assert host.db_reads == [1, 1]


def test_failed_transition_clears_native_state_and_retries_new_metadata(native_host):
    host = native_host
    controller = host.transport
    first = controller.snapshot()
    original_get = host.db.get_track
    host.current_track_id = 2
    def fail_once(_track_id):
        host.db.get_track = original_get
        raise RuntimeError("synthetic transient read failure")
    host.db.get_track = fail_once
    controller._publish()
    failed = controller.smtc.snapshots[-1]
    assert not failed.loaded and not failed.title
    assert failed.revision > first.revision
    host.row["title"] = "Second canonical title"
    controller._publish()
    recovered = controller.smtc.snapshots[-1]
    assert recovered.track_id == 2
    assert recovered.title == "Second canonical title"
    assert recovered.revision > failed.revision


@pytest.mark.parametrize("status", [QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia])
def test_empty_failed_media_clears_stale_metadata_and_art(native_host, status):
    controller = native_host.transport
    assert controller.snapshot().title
    native_host.player.status = status
    snapshot = controller.snapshot()
    assert snapshot.track_id is None
    assert not snapshot.loaded
    assert snapshot.title == snapshot.artist == snapshot.album == ""
    assert snapshot.artwork == b""
    assert snapshot.state == "stopped"
    assert snapshot.position_ms == snapshot.duration_ms == 0


def test_actual_player_state_and_base_capabilities_survive_queue_playback(native_host):
    host = native_host
    host.current_track_id = 99  # Manual track; base anchor remains track 1.
    host.player.state = QMediaPlayer.PlaybackState.PlayingState
    snapshot = host.transport.snapshot()
    assert snapshot.state == "playing"
    assert snapshot.can_next and not snapshot.can_previous
    host.base_playback_context["current_track_id"] = 3
    assert not host.transport.snapshot().can_next
    host.manual_queue.append(100)
    assert host.transport.snapshot().can_next


def test_native_callbacks_dispatch_only_on_gui_turn_and_reject_old_generation(native_host, qapp):
    controller = native_host.transport
    backend = controller.smtc
    backend.command.emit("play", controller.generation)
    assert native_host.calls == []
    qapp.processEvents()
    assert native_host.calls == ["toggle"]
    old_generation = controller.generation
    controller.attach(101)
    backend.command.emit("next", old_generation)
    qapp.processEvents()
    assert native_host.calls == ["toggle"]
    assert backend.closed == 1


def test_smtc_owns_media_keys_and_fallback_waits_for_confirmed_failure(native_host):
    controller = native_host.transport
    command = (11 | 0x8000) << 16  # Key-device flags are not the action ID.
    assert not controller.handle_native_message(100, transport.WM_APPCOMMAND, 0, command)
    controller._availability(True, controller.generation, "")
    assert not controller.handle_native_message(100, transport.WM_APPCOMMAND, 0, command)
    controller._availability(False, controller.generation, "smtc_unavailable")
    assert controller.handle_native_message(100, transport.WM_APPCOMMAND, 0, command)
    assert native_host.calls == ["next"]
    assert not controller.handle_native_message(200, transport.WM_APPCOMMAND, 0, command)
    assert not controller.handle_native_message(100, transport.WM_APPCOMMAND, 0, 10 << 16)


def test_taskbar_epochs_replay_without_creating_another_smtc_session(native_host):
    controller = native_host.transport
    backend = controller.smtc
    for _ in range(2):
        assert not controller.handle_native_message(100, 4999, 0, 0)
        controller._publish()
    assert controller.taskbar.epochs == 2
    assert controller.smtc is backend
    assert controller.handle_native_message(100, 999, 0, 0)
    assert native_host.calls == ["next"]


def test_close_rejects_pending_commands_and_retains_unjoined_worker(native_host):
    controller = native_host.transport
    backend = controller.smtc
    backend.can_close = False
    assert not controller.close()
    assert controller._retiring == [backend]
    controller._command("next", controller.generation)
    assert native_host.calls == []
    backend.can_close = True
    assert controller.close()
    assert not controller._retiring


def test_taskbar_readiness_survives_deferred_handle_replacement(native_host):
    controller = native_host.transport
    old = controller.smtc
    old.can_close = False
    controller.attach(101)
    assert controller.hwnd == 0
    assert controller.smtc is None
    assert controller.taskbar is None
    controller.handle_native_message(101, 4999, 0, 0)
    assert controller._ready_hwnd == 101
    old.can_close = True
    controller._finish_attach()
    assert controller.hwnd == 101
    assert controller.taskbar.epochs == 1
    assert controller._ready_hwnd == 0


def test_cleanup_failure_does_not_enable_competing_hardware_route(native_host):
    controller = native_host.transport
    controller._availability(False, controller.generation, "cleanup_failed")
    assert controller.smtc_available is None
    assert not controller.handle_native_message(100, transport.WM_APPCOMMAND, 0, 11 << 16)


def test_position_updates_are_throttled_but_seek_is_prompt(native_host, monkeypatch):
    controller = native_host.transport
    now = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    calls = []
    monkeypatch.setattr(controller, "schedule_update", lambda: calls.append(True))
    for position in range(0, 4000, 100):
        controller._position_changed(position)
        now[0] += 0.1
    assert len(calls) == 1
    controller._position_changed(10_000)
    assert len(calls) == 2
    assert native_host.db_reads == []


def test_artwork_is_bounded_in_memory_and_missing_art_is_empty(tmp_path, qapp):
    image = QImage(1200, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#257060"))
    path = tmp_path / "synthetic.png"
    assert image.save(str(path))
    payload = transport._artwork_png(path)
    decoded = QImage.fromData(payload)
    assert decoded.size().width() == 512
    assert decoded.size().height() == 256
    assert len(payload) <= 1024 * 1024
    assert transport._artwork_png(tmp_path / "missing.png") == b""


def test_offscreen_default_never_constructs_native_backends(native_host):
    disabled = transport.WindowsTransportController(
        native_host, smtc_factory=lambda: pytest.fail("offscreen native activation"),
        taskbar_factory=lambda _: pytest.fail("offscreen shell activation"),
    )
    assert not disabled.enabled
    disabled.attach(333)
    assert disabled.smtc is None
    assert disabled.close()


def test_native_commands_fail_closed_without_escaping_qt_callback(native_host):
    controller = native_host.transport
    controller.actions._next = lambda: (_ for _ in ()).throw(RuntimeError("synthetic"))
    controller._command("next", controller.generation)
    assert controller.last_error == "transport_command_failed"


def test_readiness_received_before_first_attachment_is_replayed(native_host):
    host = native_host
    disabled = transport.WindowsTransportController(
        host, enabled=True, smtc_factory=Smtc, taskbar_factory=Taskbar,
    )
    hwnd = int(host.winId())
    try:
        disabled.handle_native_message(hwnd, 4999, 0, 0)
        assert disabled._ready_hwnd == hwnd
        disabled.attach(hwnd)
        assert disabled.taskbar.epochs == 1
    finally:
        assert disabled.close()
