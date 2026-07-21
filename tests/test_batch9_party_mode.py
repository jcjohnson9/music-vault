from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QMessageBox, QSlider

import music_vault.app as app_module
from music_vault.app import MusicVaultWindow
from music_vault.core.audio_analysis import AudioFeatures
from music_vault.core import paths
from music_vault.ui import review
from music_vault.ui.party_mode import (
    PARTY_MODE_DEFAULTS,
    PARTY_PRESETS,
    PartyAudioAnalysisThread,
    PartyModeWindow,
    normalize_party_mode_settings,
)
from music_vault.ui.party_palette import DEFAULT_PARTY_PALETTE


class _FakePlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._position = 30_000
        self._duration = 120_000
        self._state = QMediaPlayer.PlaybackState.PausedState

    def position(self) -> int:
        return self._position

    def duration(self) -> int:
        return self._duration

    def playbackState(self):
        return self._state

    def isSeekable(self) -> bool:
        return True

    def setPosition(self, value: int) -> None:
        self._position = max(0, min(self._duration, int(value)))
        self.positionChanged.emit(self._position)

    def set_state(self, state: QMediaPlayer.PlaybackState) -> None:
        self._state = state
        self.playbackStateChanged.emit(state)


class _FakeAudioOutput:
    def __init__(self) -> None:
        self._muted = False

    def isMuted(self) -> bool:
        return self._muted

    def setMuted(self, muted: bool) -> None:
        self._muted = bool(muted)


class _FakeDB:
    def __init__(self) -> None:
        self.tracks: dict[int, dict[str, object]] = {}

    def get_track(self, track_id: object):
        return self.tracks.get(int(track_id)) if track_id is not None else None


class _PartyHost(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = {
            **PARTY_MODE_DEFAULTS,
            "party_mode_reduced_motion": True,
            "party_mode_auto_hide_overlay": False,
        }
        self.current_track_id = None
        self.db = _FakeDB()
        self.player = _FakePlayer(self)
        self.audio_output = _FakeAudioOutput()
        self.volume_percent = 70
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(self.volume_percent)
        self.volume_slider.valueChanged.connect(self._set_volume)
        self.autoplay_enabled = True
        self.shuffle_enabled = False
        self.repeat_mode = "off"
        self.manual_queue = [31, 32]
        self.calls: list[str] = []
        self.save_count = 0

    def _set_volume(self, value: int) -> None:
        self.volume_percent = int(value)

    def save_config(self) -> None:
        self.save_count += 1

    def toggle_play(self) -> None:
        self.calls.append("play")
        state = (
            QMediaPlayer.PlaybackState.PausedState
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            else QMediaPlayer.PlaybackState.PlayingState
        )
        self.player.set_state(state)

    def play_previous(self) -> None:
        self.calls.append("previous")

    def play_next(self) -> None:
        self.calls.append("next")

    def toggle_autoplay(self) -> None:
        self.calls.append("auto")
        self.autoplay_enabled = not self.autoplay_enabled
        if self.autoplay_enabled:
            self.shuffle_enabled = False

    def toggle_shuffle(self) -> None:
        self.calls.append("shuffle")
        self.shuffle_enabled = not self.shuffle_enabled
        if self.shuffle_enabled:
            self.autoplay_enabled = False

    def cycle_repeat(self) -> None:
        self.calls.append("repeat")
        states = ("off", "all", "one")
        self.repeat_mode = states[(states.index(self.repeat_mode) + 1) % len(states)]


@pytest.fixture
def party_surface(qapp):
    host = _PartyHost()
    window = PartyModeWindow(host)
    yield host, window
    window.close()
    window.canvas.stop_rendering()
    window.deleteLater()
    host.close()
    host.deleteLater()
    qapp.processEvents()


@pytest.fixture
def isolated_music_vault_window(tmp_path: Path, monkeypatch, qapp):
    data = tmp_path / "runtime-data"
    result = paths.configure_data_dir(data, persist=False)
    assert result.configured is True
    (data / "music_vault_config.json").write_text(
        json.dumps({"volume_percent": 37, "unrelated_setting": "preserved"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "use_system_default_audio_output",
        lambda self: None,
    )
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "read_saved_api_key",
        lambda self: "",
    )
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "find_ffmpeg_bin",
        lambda self: None,
    )
    monkeypatch.setattr(app_module, "apply_dark_title_bar", lambda _window: False)
    monkeypatch.setattr(
        app_module,
        "export_app_status",
        lambda *_args, **_kwargs: data / "music_vault_status.json",
    )
    window = app_module.MusicVaultWindow()
    yield window, data
    window.audio_device_timer.stop()
    window.volume_save_timer.stop()
    if window.party_mode_window is not None:
        window.party_mode_window.close()
    window.close()
    window.db.close()
    window.deleteLater()
    qapp.processEvents()
    paths.clear_configured_data_dir()


def _key(
    key: Qt.Key,
    *,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    text: str = "",
) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text)


def test_party_settings_normalization_is_strict_bounded_and_non_mutating() -> None:
    assert normalize_party_mode_settings(None) == PARTY_MODE_DEFAULTS
    source = {
        "party_mode_preset": "AURORA",
        "party_mode_quality": "impossible",
        "party_mode_frame_rate": "60",
        "party_mode_reduced_motion": "true",
        "party_mode_show_artwork": False,
        "party_mode_auto_hide_overlay": 1,
        "party_mode_overlay_timeout_seconds": 99,
        "unrelated": "preserve outside the normalizer",
    }
    original = dict(source)
    normalized = normalize_party_mode_settings(source)
    assert normalized == {
        "party_mode_config_version": 2,
        "party_mode_preset": "aurora",
        "party_mode_quality": "auto",
        "party_mode_frame_rate": "60",
        "party_mode_reduced_motion": False,
        "party_mode_show_artwork": False,
        "party_mode_auto_hide_overlay": True,
        "party_mode_overlay_timeout_seconds": 10,
    }
    assert source == original
    assert normalize_party_mode_settings(
        {"party_mode_overlay_timeout_seconds": 0}
    )["party_mode_overlay_timeout_seconds"] == 1


def test_audio_analysis_thread_keeps_latest_buffer_and_shuts_down_cleanly(qapp) -> None:
    thread = PartyAudioAnalysisThread()
    spy = QSignalSpy(thread.features_ready)
    quiet = struct.pack("<" + "f" * 128, *((0.01,) * 128))
    loud = struct.pack("<" + "f" * 128, *((0.75,) * 128))
    try:
        assert thread.submit(quiet, "f32", 1, 48_000, 1_000) is True
        assert thread.submit(loud, "f32", 1, 48_000, 2_000) is True
        assert thread.submit(quiet, "f32", 1, 48_000, 1_500) is False
        assert thread.pending_count == 1
        assert thread.dropped_buffer_count == 2

        thread.start()
        deadline = time.monotonic() + 2.0
        while spy.count() == 0 and time.monotonic() < deadline:
            QTest.qWait(20)
        assert spy.count() == 1
        features = spy.at(0)[0]
        assert isinstance(features, AudioFeatures)
        assert features.timestamp == 2.0
        assert features.sample_count == 128
        assert features.rms > 0.2
    finally:
        thread.shutdown()
    assert thread.isRunning() is False
    assert thread.pending_count == 0
    assert thread.submit(loud, "f32", 1, 48_000, 3_000) is False
    thread.deleteLater()
    qapp.processEvents()


def test_party_window_reuses_host_player_and_tracks_player_signals(
    party_surface, qapp
) -> None:
    host, window = party_surface
    player = host.player
    assert window._host() is host
    assert host.player is player
    assert not hasattr(window, "player")
    assert window.findChildren(QMediaPlayer) == []

    window.show_on_screen(qapp.primaryScreen())
    qapp.processEvents()
    assert window.isFullScreen() is True
    assert window.windowHandle() is not None
    assert window.windowHandle().screen() is qapp.primaryScreen()

    player.setPosition(44_000)
    player.durationChanged.emit(180_000)
    assert window.elapsed_label.text() == "0:44"
    assert window.duration_label.text() == "3:00"
    assert window.progress_slider.value() == 44_000


def test_no_track_and_missing_artwork_use_neutral_fallbacks(
    party_surface,
    tmp_path: Path,
) -> None:
    host, window = party_surface
    assert window.title_label.text() == "Choose a song to begin"
    assert window.artist_label.text() == "Music Vault is ready"
    assert window.canvas._artwork is None
    assert window.canvas._has_track is False

    host.db.tracks[7] = {
        "path": str(tmp_path / "synthetic.mp3"),
        "title": "Synthetic title",
        "artist": "Synthetic artist",
        "album": "Synthetic album",
        "cover_path": str(tmp_path / "missing-cover.png"),
    }
    host.current_track_id = 7
    window.refresh_from_host(force=True)
    assert window.title_label.text() == "Synthetic title"
    assert window.artist_label.text() == "Synthetic artist"
    assert window.album_label.text() == "Synthetic album"
    assert window.overlay_artwork.pixmap().isNull() is False
    assert window.canvas._artwork is None
    assert window.canvas._title == "Synthetic title"
    assert window._palette == DEFAULT_PARTY_PALETTE


def test_party_artwork_decode_is_bounded(tmp_path: Path) -> None:
    source = QImage(2_048, 1_280, QImage.Format.Format_RGB32)
    source.fill(QColor("#4a73d8"))
    path = tmp_path / "synthetic-large-artwork.png"
    assert source.save(str(path), "PNG")
    artwork = PartyModeWindow._load_bounded_artwork(str(path))
    assert artwork.isNull() is False
    assert max(artwork.width(), artwork.height()) <= 1_024


def test_preset_overlay_help_and_safe_shortcuts_are_automatable(party_surface) -> None:
    host, window = party_surface
    preset_spy = QSignalSpy(window.preset_changed)
    start = window.current_preset
    cycled = window.cycle_preset()
    assert cycled == PARTY_PRESETS[(PARTY_PRESETS.index(start) + 1) % len(PARTY_PRESETS)]
    assert host.config["party_mode_preset"] == cycled
    assert host.save_count == 1
    assert preset_spy.count() == 1

    window.show_overlay()
    assert window.overlay_visible is True
    assert window.root_stack.currentWidget() is window.overlay
    window.hide_overlay()
    assert window.overlay_visible is False
    window.toggle_overlay()
    assert window.overlay_visible is True
    window.toggle_help()
    assert window.help_panel.isHidden() is False
    assert window.root_stack.currentWidget() is window.help_panel
    window.hide_overlay()
    assert window.overlay_visible is True
    window.toggle_help()
    assert window.root_stack.currentWidget() is window.overlay

    assert window._route_key(_key(Qt.Key.Key_Space, text=" ")) is True
    assert window._route_key(_key(Qt.Key.Key_V, text="v")) is True
    assert window._route_key(_key(Qt.Key.Key_H, text="h")) is True
    assert window.overlay_visible is False
    assert window._route_key(_key(Qt.Key.Key_H, text="h")) is True
    assert window.overlay_visible is True
    assert "play" in host.calls
    assert window.current_preset != cycled


def test_review_normalizes_editor_focus_before_party_overlay_shortcut(
    party_surface,
    qapp,
) -> None:
    _host, window = party_surface
    window.show()
    window.show_overlay()
    qapp.processEvents()

    editor = QLineEdit(window)
    editor.show()
    editor.setFocus(Qt.FocusReason.OtherFocusReason)
    qapp.processEvents()
    assert QApplication.focusWidget() is editor
    review._send_review_key(window, Qt.Key.Key_H, text="h")
    assert window.overlay_visible is True

    review._normalize_party_review_shortcut_focus(window, qapp)
    assert QApplication.focusWidget() is window.help_button
    review._send_review_key(window, Qt.Key.Key_H, text="h")
    assert window.overlay_visible is False


def test_escape_and_f11_each_close_party_mode(party_surface, qapp) -> None:
    _host, window = party_surface
    closed = QSignalSpy(window.party_closed)

    window.show_on_screen(qapp.primaryScreen())
    qapp.processEvents()
    assert window._route_key(_key(Qt.Key.Key_Escape)) is True
    qapp.processEvents()
    assert window.isVisible() is False
    assert closed.count() == 1

    window.show_on_screen(qapp.primaryScreen())
    qapp.processEvents()
    assert window._route_key(_key(Qt.Key.Key_F11)) is True
    qapp.processEvents()
    assert window.isVisible() is False
    assert closed.count() == 2


def test_keyboard_transport_volume_modes_and_help_delegate_safely(
    party_surface,
) -> None:
    host, window = party_surface
    queue_before = list(host.manual_queue)
    volume_before = host.volume_percent

    assert window._route_key(_key(Qt.Key.Key_Left)) is True
    assert host.player.position() == 20_000
    assert window._route_key(_key(Qt.Key.Key_Right)) is True
    assert host.player.position() == 30_000
    assert window._route_key(
        _key(Qt.Key.Key_Left, modifiers=Qt.KeyboardModifier.ControlModifier)
    ) is True
    assert window._route_key(
        _key(Qt.Key.Key_Right, modifiers=Qt.KeyboardModifier.ControlModifier)
    ) is True
    assert host.calls[-2:] == ["previous", "next"]

    assert window._route_key(_key(Qt.Key.Key_Up)) is True
    assert host.volume_percent == volume_before + 5
    assert window._route_key(_key(Qt.Key.Key_Down)) is True
    assert host.volume_percent == volume_before
    host.volume_slider.setValue(100)
    assert window._route_key(_key(Qt.Key.Key_Up)) is True
    assert host.volume_percent == 100
    host.volume_slider.setValue(0)
    assert window._route_key(_key(Qt.Key.Key_Down)) is True
    assert host.volume_percent == 0
    host.volume_slider.setValue(volume_before)

    assert window._route_key(_key(Qt.Key.Key_M, text="m")) is True
    assert host.audio_output.isMuted() is True
    assert host.volume_percent == volume_before
    assert window._route_key(_key(Qt.Key.Key_M, text="m")) is True
    assert host.audio_output.isMuted() is False
    assert host.volume_percent == volume_before

    assert window._route_key(_key(Qt.Key.Key_S, text="s")) is True
    assert host.shuffle_enabled is True
    assert host.autoplay_enabled is False
    assert window._route_key(_key(Qt.Key.Key_A, text="a")) is True
    assert host.autoplay_enabled is True
    assert host.shuffle_enabled is False
    assert window._route_key(_key(Qt.Key.Key_R, text="r")) is True
    assert host.repeat_mode == "all"
    assert host.manual_queue == queue_before

    assert window._route_key(_key(Qt.Key.Key_Question, text="?")) is True
    assert window._help_visible is True
    assert window.root_stack.currentWidget() is window.help_panel


def test_text_entry_focus_prevents_party_shortcut_theft(
    party_surface,
    qapp,
) -> None:
    host, window = party_surface
    editor = QLineEdit(window.overlay)
    editor.show()
    window.show()
    window.activateWindow()
    editor.setFocus(Qt.FocusReason.OtherFocusReason)
    qapp.processEvents()
    assert QApplication.focusWidget() is editor

    calls_before = list(host.calls)
    assert window._route_key(_key(Qt.Key.Key_Space, text=" ")) is False
    assert window._route_key(_key(Qt.Key.Key_V, text="v")) is False
    assert host.calls == calls_before


def test_overlay_auto_hide_mouse_reveal_and_all_controls_block_hiding(
    party_surface,
    qapp,
) -> None:
    _host, window = party_surface
    window.apply_settings(
        {
            "party_mode_auto_hide_overlay": True,
            "party_mode_overlay_timeout_seconds": 1,
            "party_mode_reduced_motion": True,
        }
    )
    window.show()
    qapp.processEvents()
    window.show_overlay()
    assert window.overlay_timer.isActive() is True

    window._hide_overlay_if_idle()
    assert window.overlay_visible is False
    window.eventFilter(window.overlay, QEvent(QEvent.Type.MouseMove))
    assert window.overlay_visible is True

    for control in (
        window.controls_panel,
        window.preset_button,
        window.help_button,
        window.exit_button,
    ):
        window.eventFilter(control, QEvent(QEvent.Type.Enter))
        assert window._overlay_hovered is True
        window._hide_overlay_if_idle()
        assert window.overlay_visible is True
        window.eventFilter(control, QEvent(QEvent.Type.Leave))
        assert window._overlay_hovered is False


def test_transport_and_modes_delegate_without_mutating_manual_queue(party_surface) -> None:
    host, window = party_surface
    queue_before = list(host.manual_queue)
    window.refresh_from_host(force=True)
    assert window.queue_label.text() == "Q: 2"

    window.previous_button.click()
    window.play_button.click()
    window.next_button.click()
    assert host.calls[:3] == ["previous", "play", "next"]
    assert window.seek_relative(10_000) == 40_000
    assert host.player.position() == 40_000
    assert window.adjust_volume(5) == 75
    assert host.volume_percent == 75
    assert window.toggle_mute() is True
    assert host.audio_output.isMuted() is True

    window._toggle_auto()
    window._toggle_shuffle()
    window._cycle_repeat()
    assert host.manual_queue == queue_before
    assert window.queue_label.text() == "Q: 2"


def test_audio_features_drive_capability_and_fall_back_when_stale(party_surface) -> None:
    host, window = party_surface
    host.player.set_state(QMediaPlayer.PlaybackState.PlayingState)
    signal = QSignalSpy(window.audio_reactivity_changed)
    features = AudioFeatures(
        rms=0.4,
        peak=0.6,
        bass=0.5,
        low_mid=0.2,
        mid=0.1,
        high=0.3,
        is_silent=False,
        sample_rate=48_000,
        sample_count=512,
        timestamp=1.0,
    )
    window.on_audio_features(features)
    assert window.audio_reactivity_available is True
    assert window.current_preset == "static"
    assert window.canvas._features is None
    assert window._latest_audio_features is features
    assert window.performance_metrics()["audio_reactivity_available"] is True
    assert signal.count() == 1

    window.on_audio_features(object())
    assert window._latest_audio_features is features
    window.cycle_preset()
    assert window.current_preset == "starfield"
    assert window.canvas._features is features
    window._last_audio_feature_at = time.monotonic() - 2.0
    window._update_audio_capability()
    assert window.audio_reactivity_available is False
    assert window.canvas._features is None
    assert window._latest_audio_features is None
    assert signal.count() == 2


def test_party_hide_clears_audio_capability_and_stale_features(
    party_surface, qapp
) -> None:
    host, window = party_surface
    host.player.set_state(QMediaPlayer.PlaybackState.PlayingState)
    window.show()
    qapp.processEvents()
    window.on_audio_features(
        AudioFeatures(
            rms=0.35,
            bass=0.42,
            sample_rate=48_000,
            sample_count=256,
            is_silent=False,
        )
    )
    assert window.audio_reactivity_available is True
    window.hide()
    qapp.processEvents()
    assert window.audio_reactivity_available is False
    assert window.canvas._features is None
    assert window._last_audio_feature_at == 0.0


def test_paused_playback_rejects_in_flight_audio_features(party_surface) -> None:
    host, window = party_surface
    host.player.set_state(QMediaPlayer.PlaybackState.PausedState)
    window.on_audio_features(
        AudioFeatures(
            rms=0.4,
            bass=0.5,
            sample_rate=48_000,
            sample_count=256,
            is_silent=False,
        )
    )

    assert window.audio_reactivity_available is False
    assert window.canvas._features is None


def test_party_window_close_stops_rendering_timers_and_emits_once(
    party_surface,
    qapp,
) -> None:
    _host, window = party_surface
    closed = QSignalSpy(window.party_closed)
    window.show()
    window.canvas.set_preset("starfield")
    window.canvas.start_rendering()
    window.state_timer.start()
    window.fallback_timer.start()
    window.toggle_help()
    window._overlay_hovered = True
    assert window.rendering_active is True

    assert window.close() is True
    qapp.processEvents()
    assert closed.count() == 1
    assert window.rendering_active is False
    assert window.state_timer.isActive() is False
    assert window.fallback_timer.isActive() is False
    assert window.palette_timer.isActive() is False
    assert window.help_panel.isHidden() is True
    assert window._overlay_hovered is False
    assert window.audio_reactivity_available is False


def test_app_status_reports_party_and_audio_capability_without_runtime_io(
    monkeypatch,
) -> None:
    captured = {}

    def capture(db, config, extra):
        captured.update({"db": db, "config": config, "extra": extra})

    monkeypatch.setattr(app_module, "export_app_status", capture)

    class Harness:
        db = _FakeDB()
        current_track_id = None
        player = _FakePlayer()
        shuffle_enabled = False
        autoplay_enabled = True
        repeat_mode = "off"
        manual_queue = [1, 2]
        party_mode_active = True
        party_audio_reactivity_available = True
        config = {"party_mode_preset": "aurora"}
        app_sync_status = None

        @staticmethod
        def read_saved_api_key() -> str:
            return ""

        @staticmethod
        def find_ffmpeg_bin():
            return None

    MusicVaultWindow.write_app_status(Harness())
    extra = captured["extra"]
    assert extra["party_mode_active"] is True
    assert extra["party_mode_preset"] == "aurora"
    assert extra["audio_reactivity_available"] is True
    assert extra["playback"]["queue_count"] == 2


def test_audio_buffer_integration_is_active_only_and_bounded_to_one_megabyte() -> None:
    class CaptureThread:
        def __init__(self) -> None:
            self.calls = []

        @staticmethod
        def isRunning() -> bool:
            return True

        def submit(self, *arguments) -> None:
            self.calls.append(arguments)

    class Format:
        @staticmethod
        def channelCount() -> int:
            return 2

        @staticmethod
        def sampleRate() -> int:
            return 48_000

        @staticmethod
        def sampleFormat():
            return "s16"

    class Buffer:
        payload = b"x" * 1_048_700

        @staticmethod
        def isValid() -> bool:
            return True

        @classmethod
        def byteCount(cls) -> int:
            return len(cls.payload)

        @staticmethod
        def format():
            return Format()

        @classmethod
        def constData(cls):
            return memoryview(cls.payload)

        @staticmethod
        def startTime() -> int:
            return 2_345_000

    class Player:
        @staticmethod
        def position() -> int:
            return 999

    thread = CaptureThread()
    harness = type(
        "Harness",
        (),
        {
            "party_mode_active": False,
            "party_audio_thread": thread,
            "player": Player(),
        },
    )()
    MusicVaultWindow.on_party_audio_buffer_received(harness, Buffer())
    assert thread.calls == []

    harness.party_mode_active = True
    MusicVaultWindow.on_party_audio_buffer_received(harness, Buffer())
    assert len(thread.calls) == 1
    pcm, sample_format, channels, sample_rate, timestamp_ms = thread.calls[0]
    assert len(pcm) == 1_048_576
    assert (sample_format, channels, sample_rate) == ("s16", 2, 48_000)
    assert 0 < timestamp_ms <= time.monotonic_ns() // 1_000_000


def test_music_vault_party_toggle_and_source_wiring_invariants() -> None:
    party_source = Path("music_vault/ui/party_mode.py").read_text(encoding="utf-8")
    app_source = Path("music_vault/app.py").read_text(encoding="utf-8")
    assert "QMediaPlayer(" not in party_source
    assert app_source.count("self.player = QMediaPlayer(self)") == 1
    assert "self.audio_buffer_output = QAudioBufferOutput(self)" in app_source
    assert "self.player.setAudioBufferOutput(self.audio_buffer_output)" in app_source
    assert "byte_count = min(len(view), 1_048_576)" in app_source
    assert "self.party_mode_shortcut = QShortcut(QKeySequence(Qt.Key_F11), self)" in app_source
    assert "self.party_mode_btn.clicked.connect(self.toggle_party_mode)" in app_source


def test_actual_music_vault_open_close_preserves_authoritative_playback_state(
    isolated_music_vault_window,
    qapp,
) -> None:
    window, _data = isolated_music_vault_window
    player = window.player
    audio_output = window.audio_output
    source = player.source()
    position = player.position()
    state = player.playbackState()
    volume = window.volume_percent
    window.manual_queue[:] = [101, 202, 303]
    base_context = {
        "kind": "playlist",
        "playlist_id": 9,
        "playlist_name": "Synthetic context",
        "track_ids": [8, 9, 10],
        "current_track_id": 8,
    }
    window.base_playback_context = dict(base_context)

    assert window.party_mode_btn.toolTip() == "Party Mode (F11)"
    assert window.party_mode_btn.accessibleName() == "Open Party Mode"
    window.resize(window.minimumSize())
    window.show()
    qapp.processEvents()
    assert window.party_mode_btn.isVisibleTo(window.player_bar)
    button_origin = window.party_mode_btn.mapTo(window.player_bar, window.party_mode_btn.rect().topLeft())
    assert window.player_bar.rect().contains(button_origin)
    assert window.player_bar.rect().contains(
        button_origin + window.party_mode_btn.rect().bottomRight()
    )

    window.open_party_mode()
    qapp.processEvents()
    party = window.party_mode_window
    assert party is not None and party.isVisible()
    assert window.party_mode_active is True
    assert window.player is player
    assert window.audio_output is audio_output
    assert player.source() == source
    assert player.position() == position
    assert player.playbackState() == state
    assert window.volume_percent == volume
    assert window.manual_queue == [101, 202, 303]
    assert window.base_playback_context == base_context

    assert len(window.findChildren(QMediaPlayer)) == 1
    assert party.findChildren(QMediaPlayer) == []
    # Static is intentionally idle: entering Party Mode records a render
    # request without running the high-frequency visual timer.
    assert party.current_preset == "static"
    assert party.rendering_active is False

    party.close()
    qapp.processEvents()
    assert window.party_mode_active is False
    assert window.party_audio_thread is None
    assert party.rendering_active is False
    assert player.source() == source
    assert player.position() == position
    assert player.playbackState() == state
    assert window.volume_percent == volume
    assert window.manual_queue == [101, 202, 303]
    assert window.base_playback_context == base_context

    for _ in range(3):
        window.open_party_mode()
        qapp.processEvents()
        assert window.party_mode_window is party
        assert party.current_preset == "static"
        assert party.rendering_active is False
        party.close()
        qapp.processEvents()
        assert window.party_audio_thread is None
        assert party.rendering_active is False


def test_actual_main_f11_opens_party_and_main_close_stops_it(
    isolated_music_vault_window,
    qapp,
) -> None:
    window, _data = isolated_music_vault_window
    window.show()
    window.activateWindow()
    qapp.processEvents()

    QTest.keyClick(window, Qt.Key.Key_F11)
    qapp.processEvents()
    party = window.party_mode_window
    assert party is not None
    assert party.isVisible() is True
    assert party.isFullScreen() is True
    assert window.party_mode_active is True
    assert window.party_audio_thread is not None

    window.close()
    qapp.processEvents()
    assert party.isVisible() is False
    assert party.rendering_active is False
    assert window.party_mode_active is False
    assert window.party_audio_thread is None


def test_actual_party_settings_persist_unrelated_values_and_apply_live(
    isolated_music_vault_window,
    monkeypatch,
    qapp,
) -> None:
    window, data = isolated_music_vault_window
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: QMessageBox.Ok)
    window.open_party_mode()
    qapp.processEvents()
    window.settings_party_preset.setCurrentText("Aurora")
    window.settings_party_quality.setCurrentText("High")
    window.settings_party_frame_rate.setCurrentText("30 FPS")
    window.settings_party_reduced_motion.setChecked(True)
    window.settings_party_show_artwork.setChecked(False)
    window.settings_party_auto_hide.setChecked(False)
    window.settings_party_overlay_timeout.setValue(7)
    window.save_settings_from_ui()

    saved = json.loads(
        (data / "music_vault_config.json").read_text(encoding="utf-8")
    )
    assert saved["unrelated_setting"] == "preserved"
    assert saved["party_mode_preset"] == "aurora"
    assert saved["party_mode_quality"] == "high"
    assert saved["party_mode_frame_rate"] == "30"
    assert saved["party_mode_reduced_motion"] is True
    assert saved["party_mode_show_artwork"] is False
    assert saved["party_mode_auto_hide_overlay"] is False
    assert saved["party_mode_overlay_timeout_seconds"] == 7
    assert not any("api" in key.casefold() for key in saved)
    assert window.party_mode_window.current_preset == "aurora"
    assert window.party_mode_window.canvas.quality == "high"

    class VisibleWindow:
        def __init__(self) -> None:
            self.close_count = 0

        @staticmethod
        def isVisible() -> bool:
            return True

        def close(self) -> None:
            self.close_count += 1

    class ToggleHarness:
        def __init__(self) -> None:
            self.party_mode_active = False
            self.party_mode_window = None
            self.open_count = 0

        def open_party_mode(self) -> None:
            self.open_count += 1

    harness = ToggleHarness()
    MusicVaultWindow.toggle_party_mode(harness)
    assert harness.open_count == 1
    window = VisibleWindow()
    harness.party_mode_active = True
    harness.party_mode_window = window
    MusicVaultWindow.toggle_party_mode(harness)
    assert window.close_count == 1
