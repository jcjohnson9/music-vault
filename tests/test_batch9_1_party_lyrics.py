from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QMainWindow, QSlider

import music_vault.app as app_module
from music_vault.app import MusicVaultWindow
from music_vault.core.audio_analysis import AudioFeatures
from music_vault.lyrics.models import (
    LyricLine,
    LyricsResult,
    LyricsSource,
    LyricsStatus,
    TrackLyricsIdentity,
)
from music_vault.ui.icons import icon_path
from music_vault.ui.party_lyrics import (
    LYRICS_CONSENT_VERSION,
    LYRICS_DEFAULTS,
    LyricsTimeline,
    PartyLyricsController,
    PartyLyricsPanel,
    normalize_lyrics_settings,
)
from music_vault.ui.party_mode import (
    PARTY_MODE_CONFIG_VERSION,
    PARTY_MODE_DEFAULTS,
    PARTY_PRESETS,
    PartyModeWindow,
    normalize_party_mode_settings,
    party_preset_label,
    party_preset_value,
)
from music_vault.ui.party_visuals import center_content_bottom, is_safe_firework_position


def identity(tmp_path: Path, track_id: int = 1) -> TrackLyricsIdentity:
    return TrackLyricsIdentity(
        track_id,
        f"Synthetic title {track_id}",
        "Synthetic artist",
        "Synthetic album",
        180_000,
        tmp_path / f"synthetic-{track_id}.wav",
    )


def synced_result(track: TrackLyricsIdentity) -> LyricsResult:
    return LyricsResult(
        LyricsStatus.AVAILABLE,
        track,
        LyricsSource.CACHE_SYNCED,
        (
            LyricLine(1_000, "Previous synthetic line"),
            LyricLine(2_000, "Current <b>literal</b> line"),
            LyricLine(3_000, "Next synthetic line"),
        ),
        attribution="Lyrics via LRCLIB",
        from_cache=True,
    )


class FakeLyricsService:
    def __init__(self, local_factory=None) -> None:
        self.local_factory = local_factory
        self.resolve_calls = []
        self.request_calls = []
        self.callbacks = {}
        self.generation = 0
        self.pending_count = 0
        self.import_calls = []
        self.clear_calls = []
        self.closed = False

    def resolve(self, track, *, online_enabled=False, force_refresh=False):
        self.resolve_calls.append((track.track_id, online_enabled, force_refresh))
        if self.local_factory is not None:
            result = self.local_factory(track)
            if result is not None:
                return result
        return LyricsResult(LyricsStatus.DISABLED, track)

    def request(
        self,
        track,
        callback,
        *,
        online_enabled=False,
        force_refresh=False,
    ):
        self.generation += 1
        self.pending_count = 1
        self.request_calls.append(
            (self.generation, track.track_id, online_enabled, force_refresh)
        )
        self.callbacks[self.generation] = callback
        return self.generation

    def deliver(self, generation: int, result: LyricsResult) -> None:
        self.pending_count = 0
        self.callbacks[generation](generation, result)

    def cancel(self):
        self.generation += 1
        self.pending_count = 0
        return self.generation

    def import_manual(self, track, path):
        self.import_calls.append((track.track_id, Path(path)))
        return synced_result(track)

    def clear_automatic(self, track=None):
        self.clear_calls.append(None if track is None else track.track_id)

    def close(self):
        self.closed = True


def test_config_migration_defaults_and_friendly_preset_round_trip() -> None:
    assert PARTY_PRESETS == (
        "static",
        "starfield",
        "aurora",
        "orb_cluster",
        "fireworks",
        "pulse",
    )
    assert PARTY_MODE_DEFAULTS["party_mode_preset"] == "static"
    assert normalize_party_mode_settings({"party_mode_preset": "pulse"})[
        "party_mode_preset"
    ] == "static"
    assert normalize_party_mode_settings({"party_mode_preset": "starfield"})[
        "party_mode_preset"
    ] == "starfield"
    assert normalize_party_mode_settings(
        {
            "party_mode_config_version": PARTY_MODE_CONFIG_VERSION,
            "party_mode_preset": "pulse",
        }
    )["party_mode_preset"] == "pulse"
    assert party_preset_label("orb_cluster") == "Orb Cluster"
    assert party_preset_value("Orb Cluster") == "orb_cluster"
    assert party_preset_value("unknown") == "static"

    assert normalize_lyrics_settings(None) == LYRICS_DEFAULTS
    unsafe = normalize_lyrics_settings(
        {
            "party_mode_lyrics_enabled": "true",
            "lyrics_online_lookup_enabled": 1,
            "lyrics_lookup_consent_version": -1,
        }
    )
    assert unsafe["party_mode_lyrics_enabled"] is False
    assert unsafe["lyrics_online_lookup_enabled"] is False
    assert unsafe["lyrics_lookup_consent_version"] == 0
    assert normalize_lyrics_settings(
        {
            "lyrics_online_lookup_enabled": True,
            "lyrics_lookup_consent_version": 0,
        }
    )["lyrics_online_lookup_enabled"] is False
    assert normalize_lyrics_settings(
        {
            "lyrics_online_lookup_enabled": True,
            "lyrics_lookup_consent_version": LYRICS_CONSENT_VERSION,
        }
    )["lyrics_online_lookup_enabled"] is True


def test_load_config_persists_one_time_pulse_migration_without_losing_values(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "party_mode_preset": "pulse",
                "unrelated_setting": "preserved",
            }
        ),
        encoding="utf-8",
    )

    class Harness:
        def default_config(self):
            return MusicVaultWindow.default_config(self)

        @staticmethod
        def config_file_path() -> Path:
            return config_path

    loaded = MusicVaultWindow.load_config(Harness())
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert loaded["party_mode_preset"] == "static"
    assert loaded["party_mode_config_version"] == PARTY_MODE_CONFIG_VERSION
    assert loaded["unrelated_setting"] == "preserved"
    assert persisted["party_mode_preset"] == "static"
    assert persisted["party_mode_config_version"] == PARTY_MODE_CONFIG_VERSION
    assert persisted["party_mode_lyrics_enabled"] is False
    assert persisted["lyrics_online_lookup_enabled"] is False
    assert not any("api_key" in key.casefold() for key in persisted)


def test_timeline_binary_search_and_synced_panel_render_literal_text(
    qapp,
    tmp_path: Path,
) -> None:
    track = identity(tmp_path)
    result = synced_result(track)
    timeline = LyricsTimeline(result.synced_lines)
    assert timeline.index_at(0) == 0
    assert timeline.index_at(2_500) == 1
    assert timeline.context_at(2_500)[:3] == (
        "Previous synthetic line",
        "Current <b>literal</b> line",
        "Next synthetic line",
    )

    panel = PartyLyricsPanel()
    panel.show_result(result)
    assert panel.presentation_mode == "synchronized"
    assert panel.lyrics_available is True
    assert panel.lyrics_synchronized is True
    assert panel.set_position(2_500, force=True) == 1
    assert panel.current_label.text() == "Current <b>literal</b> line"
    assert panel.current_label.textFormat() == Qt.TextFormat.PlainText
    assert panel.attribution_label.text() == "Lyrics via LRCLIB"
    # A pause is represented by an unchanged media position and cannot advance.
    assert panel.set_position(2_500) == 1
    assert panel.current_label.text() == "Current <b>literal</b> line"
    assert panel.set_position(3_500) == 2
    panel.deleteLater()
    qapp.processEvents()


def test_plain_and_honest_state_presentations(qapp, tmp_path: Path) -> None:
    track = identity(tmp_path)
    panel = PartyLyricsPanel()
    plain = LyricsResult(
        LyricsStatus.AVAILABLE,
        track,
        LyricsSource.CACHE_PLAIN,
        (),
        "Literal <script> line\n\nSecond synthetic line\n" * 12,
    )
    panel.show_result(plain)
    assert panel.presentation_mode == "plain"
    assert panel.unsynced_label.text() == "Unsynced Lyrics"
    assert panel.plain_view.toPlainText().startswith("Literal <script> line")
    assert panel.lyrics_synchronized is False
    before = panel.plain_view.verticalScrollBar().value()
    assert panel.page_scroll(1) is True
    assert panel.plain_view.verticalScrollBar().value() >= before

    for state in (
        "Finding lyrics…",
        "No local lyrics available",
        "No lyrics available",
        "Lyrics temporarily unavailable",
    ):
        panel.show_state(state)
        assert panel.state_label.text() == state
        assert panel.lyrics_available is False
    panel.show_result(LyricsResult(LyricsStatus.INSTRUMENTAL, track))
    assert panel.state_label.text() == "Instrumental"
    assert panel.lyrics_available is True
    panel.deleteLater()
    qapp.processEvents()


def test_controller_uses_immediate_local_result_and_never_requests_network(
    qapp,
    tmp_path: Path,
) -> None:
    track = identity(tmp_path)
    service = FakeLyricsService(lambda current: synced_result(current))
    panel = PartyLyricsPanel()
    controller = PartyLyricsController(panel, service=service)
    controller.apply_settings(
        {
            "party_mode_lyrics_enabled": True,
            "lyrics_online_lookup_enabled": False,
        }
    )
    controller.set_track(track)
    assert panel.presentation_mode == "synchronized"
    assert service.resolve_calls == [(1, False, False)]
    assert service.request_calls == []
    controller.set_position(2_500)
    assert panel.current_label.text() == "Current <b>literal</b> line"
    controller.close()
    assert service.closed is True
    panel.deleteLater()
    qapp.processEvents()


def test_controller_consent_online_lookup_and_stale_generation_suppression(
    qapp,
    tmp_path: Path,
) -> None:
    first = identity(tmp_path, 1)
    second = identity(tmp_path, 2)
    service = FakeLyricsService()
    panel = PartyLyricsPanel()
    controller = PartyLyricsController(panel, service=service)
    consent = QSignalSpy(controller.consent_required)
    controller.apply_settings(
        {
            "party_mode_lyrics_enabled": True,
            "lyrics_online_lookup_enabled": False,
            "lyrics_lookup_consent_version": 0,
        }
    )
    controller.set_track(first)
    assert panel.state_label.text() == "No local lyrics available"
    assert consent.count() == 1
    assert service.request_calls == []

    controller.apply_settings(
        {
            "party_mode_lyrics_enabled": True,
            "lyrics_online_lookup_enabled": True,
            "lyrics_lookup_consent_version": LYRICS_CONSENT_VERSION,
        }
    )
    old_generation = service.request_calls[-1][0]
    assert panel.state_label.text() == "Finding lyrics…"
    controller.set_track(second)
    new_generation = service.request_calls[-1][0]
    assert new_generation > old_generation
    service.deliver(old_generation, synced_result(first))
    assert panel.presentation_mode == "state"
    service.deliver(new_generation, synced_result(second))
    assert panel.presentation_mode == "synchronized"
    assert controller.lyrics_available is True
    controller.close()
    panel.deleteLater()
    qapp.processEvents()


def test_controller_resumes_a_lookup_canceled_while_party_mode_is_hidden(
    qapp,
    tmp_path: Path,
) -> None:
    track = identity(tmp_path)
    service = FakeLyricsService()
    panel = PartyLyricsPanel()
    controller = PartyLyricsController(panel, service=service)
    controller.apply_settings(
        {
            "party_mode_lyrics_enabled": True,
            "lyrics_online_lookup_enabled": True,
            "lyrics_lookup_consent_version": LYRICS_CONSENT_VERSION,
        }
    )
    controller.set_track(track)
    first_generation = service.request_calls[-1][0]
    assert panel.state_label.text() == "Finding lyrics\u2026"
    controller.suspend()
    controller.resume()
    second_generation = service.request_calls[-1][0]
    assert second_generation > first_generation
    service.deliver(first_generation, synced_result(track))
    assert panel.presentation_mode == "state"
    service.deliver(second_generation, synced_result(track))
    assert panel.presentation_mode == "synchronized"
    controller.close()
    panel.deleteLater()
    qapp.processEvents()


def test_controller_manual_import_and_clear_cancel_automatic_work(
    qapp,
    tmp_path: Path,
) -> None:
    track = identity(tmp_path)
    service = FakeLyricsService()
    panel = PartyLyricsPanel()
    controller = PartyLyricsController(panel, service=service)
    controller.apply_settings({"party_mode_lyrics_enabled": True})
    controller.set_track(track)
    selected = tmp_path / "synthetic-selection.lrc"
    selected.write_text("[00:01]Synthetic", encoding="utf-8")
    assert controller.import_manual(selected).synchronized
    assert service.import_calls == [(1, selected)]
    controller.clear_automatic()
    assert service.clear_calls == [1]
    controller.close()
    panel.deleteLater()
    qapp.processEvents()


def test_clear_automatic_does_not_immediately_refetch_online_lyrics(
    qapp,
    tmp_path: Path,
) -> None:
    track = identity(tmp_path)
    service = FakeLyricsService()
    panel = PartyLyricsPanel()
    controller = PartyLyricsController(panel, service=service)
    controller.apply_settings(
        {
            "party_mode_lyrics_enabled": True,
            "lyrics_online_lookup_enabled": True,
            "lyrics_lookup_consent_version": LYRICS_CONSENT_VERSION,
        }
    )
    controller.set_track(track)
    assert len(service.request_calls) == 1
    controller.clear_automatic()
    assert service.clear_calls == [1]
    assert len(service.request_calls) == 1
    assert panel.state_label.text() == "No local lyrics available"
    controller.close()
    panel.deleteLater()
    qapp.processEvents()


class FakePlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._position = 2_500
        self._duration = 180_000
        self._state = QMediaPlayer.PlaybackState.PausedState

    def position(self):
        return self._position

    def duration(self):
        return self._duration

    def playbackState(self):
        return self._state

    def isSeekable(self):
        return True

    def setPosition(self, value):
        self._position = int(value)
        self.positionChanged.emit(self._position)


class FakeAudioOutput:
    def __init__(self):
        self.muted = False

    def isMuted(self):
        return self.muted

    def setMuted(self, muted):
        self.muted = bool(muted)


class FakeDB:
    def __init__(self, row):
        self.row = row

    def get_track(self, track_id):
        return self.row if track_id == 1 else None


class PartyHost(QMainWindow):
    def __init__(self, row):
        super().__init__()
        self.config = {
            **PARTY_MODE_DEFAULTS,
            **LYRICS_DEFAULTS,
            "party_mode_reduced_motion": True,
            "party_mode_auto_hide_overlay": False,
        }
        self.current_track_id = 1
        self.db = FakeDB(row)
        self.player = FakePlayer(self)
        self.audio_output = FakeAudioOutput()
        self.volume_percent = 70
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.autoplay_enabled = True
        self.shuffle_enabled = False
        self.repeat_mode = "off"
        self.manual_queue = [3, 4]
        self.save_count = 0

    def save_config(self):
        self.save_count += 1

    def toggle_play(self):
        pass

    def play_previous(self):
        pass

    def play_next(self):
        pass

    def toggle_autoplay(self):
        self.autoplay_enabled = not self.autoplay_enabled

    def toggle_shuffle(self):
        self.shuffle_enabled = not self.shuffle_enabled

    def cycle_repeat(self):
        self.repeat_mode = "all"


def key(value: Qt.Key, text: str = "") -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, value, Qt.KeyboardModifier.NoModifier, text)


def test_party_surface_lyrics_are_independent_fixed_and_keyboard_accessible(
    qapp,
    tmp_path: Path,
) -> None:
    media = tmp_path / "synthetic.wav"
    media.write_bytes(b"synthetic")
    row = {
        "path": str(media),
        "title": "Synthetic title",
        "artist": "Synthetic artist",
        "album": "Synthetic album",
        "cover_path": "",
    }
    host = PartyHost(row)
    window = PartyModeWindow(host)
    service = FakeLyricsService(lambda current: synced_result(current))
    window.lyrics_controller._service = service
    try:
        window.resize(1280, 720)
        window.show()
        qapp.processEvents()
        window.refresh_from_host(force=True)
        assert window.current_preset == "static"
        window.canvas.start_rendering()
        assert window.rendering_active is False
        assert window.lyrics_button.toolTip() == "Lyrics (L)"
        assert window.lyrics_button.accessibleName() == "Toggle Lyrics"
        assert window.lyrics_button.icon().isNull() is False
        assert icon_path("lyrics").is_file()

        artwork_transform = window.canvas._frame.album_transform
        assert window._route_key(key(Qt.Key.Key_L, "l")) is True
        qapp.processEvents()
        assert host.config["party_mode_lyrics_enabled"] is True
        assert host.save_count == 1
        assert window.lyrics_panel.isVisible()
        assert window.lyrics_panel.presentation_mode == "synchronized"
        assert window.lyrics_panel.property("compact") is True
        assert window.lyrics_panel.previous_label.isVisible()
        assert window.lyrics_panel.next_label.isVisible()
        assert window.lyrics_panel.current_label.isVisible()
        assert window.lyrics_panel.current_label.font().pixelSize() == 15
        assert window.lyrics_panel.previous_label.font().pixelSize() == 11
        assert window.canvas._frame.album_transform == artwork_transform
        root = window.centralWidget()
        panel_bottom = window.lyrics_panel.geometry().bottom()
        controls_top = window.controls_panel.mapTo(root, window.controls_panel.rect().topLeft()).y()
        assert panel_bottom < controls_top
        assert window.lyrics_panel.geometry().top() > center_content_bottom(
            root.width(), root.height()
        )
        assert window.lyrics_panel.geometry().height() == 24

        calls = []
        original_set_features = window.canvas.set_features
        window.canvas.set_features = lambda features: calls.append(features)
        host.player._state = QMediaPlayer.PlaybackState.PlayingState
        features = AudioFeatures(
            rms=0.4,
            bass=0.5,
            sample_rate=44_100,
            sample_count=1_024,
            is_silent=False,
        )
        window.on_audio_features(features)
        assert calls == []
        window.canvas.set_features = original_set_features

        window.hide_overlay()
        qapp.processEvents()
        assert window.overlay_visible is False
        assert window.lyrics_panel.isVisible() is True
        start = window.current_preset
        assert start == "static"
        assert window._route_key(key(Qt.Key.Key_V, "v")) is True
        assert window.current_preset == "starfield"
        assert window.preset_button.text() == "Starfield"
        assert window.rendering_active is True
        assert window._route_key(key(Qt.Key.Key_L, "l")) is True
        assert window.lyrics_panel.isHidden()
    finally:
        window.shutdown()
        host.close()
        window.deleteLater()
        host.deleteLater()
        qapp.processEvents()


def test_visible_lyrics_geometry_protects_fireworks_and_clears_when_hidden(
    qapp,
    tmp_path: Path,
) -> None:
    media = tmp_path / "synthetic.wav"
    media.write_bytes(b"synthetic")
    row = {
        "path": str(media),
        "title": "Synthetic title",
        "artist": "Synthetic artist",
        "album": "Synthetic album",
        "cover_path": "",
    }
    host = PartyHost(row)
    window = PartyModeWindow(host)
    track = identity(tmp_path)
    plain = LyricsResult(
        LyricsStatus.AVAILABLE,
        track,
        LyricsSource.CACHE_PLAIN,
        (),
        "Synthetic unsynchronized lyrics\n" * 8,
    )
    try:
        window.resize(1280, 720)
        window.show()
        qapp.processEvents()
        window._lyrics_settings["party_mode_lyrics_enabled"] = True
        window.lyrics_panel.show_result(plain)
        window._position_lyrics_panel()
        qapp.processEvents()

        protected = window.canvas.firework_protected_rects
        assert len(protected) == 1
        left, top, right, bottom = protected[0]
        panel_top_left = window.lyrics_panel.mapTo(
            window.canvas, window.lyrics_panel.rect().topLeft()
        )
        assert left < panel_top_left.x() / window.canvas.width() < right
        assert top < panel_top_left.y() / window.canvas.height() < bottom
        assert right > (
            panel_top_left.x() + window.lyrics_panel.width()
        ) / window.canvas.width()
        assert bottom > (
            panel_top_left.y() + window.lyrics_panel.height()
        ) / window.canvas.height()
        assert is_safe_firework_position(0.18, 0.20) is True
        assert is_safe_firework_position(0.18, 0.20, protected) is False

        window._lyrics_settings["party_mode_lyrics_enabled"] = False
        window._position_lyrics_panel()
        assert window.lyrics_panel.isHidden()
        assert window.canvas.firework_protected_rects == ()

        window._lyrics_settings["party_mode_lyrics_enabled"] = True
        window._position_lyrics_panel()
        assert window.canvas.firework_protected_rects
        window.toggle_help()
        assert window.lyrics_panel.isHidden()
        assert window.canvas.firework_protected_rects == ()
    finally:
        window.shutdown()
        host.close()
        window.deleteLater()
        host.deleteLater()
        qapp.processEvents()


def test_app_status_adds_only_boolean_lyrics_state(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        app_module,
        "export_app_status",
        lambda db, config, extra: captured.update(extra=extra),
    )

    class DB:
        @staticmethod
        def get_track(_track_id):
            return None

    class Player:
        @staticmethod
        def playbackState():
            return QMediaPlayer.PlaybackState.StoppedState

    class Harness:
        db = DB()
        current_track_id = None
        player = Player()
        shuffle_enabled = False
        autoplay_enabled = True
        repeat_mode = "off"
        manual_queue = []
        party_mode_active = True
        party_audio_reactivity_available = False
        party_lyrics_available = True
        party_lyrics_synchronized = True
        config = {
            "party_mode_preset": "orb_cluster",
            "party_mode_lyrics_enabled": True,
        }
        app_sync_status = None

        @staticmethod
        def read_saved_api_key():
            return ""

        @staticmethod
        def find_ffmpeg_bin():
            return None

    MusicVaultWindow.write_app_status(Harness())
    extra = captured["extra"]
    assert extra["party_mode_lyrics_enabled"] is True
    assert extra["lyrics_available"] is True
    assert extra["lyrics_synchronized"] is True
    serialized = json.dumps(extra).casefold()
    assert "synthetic line" not in serialized
    assert "provider_result" not in serialized


def test_lyrics_enabled_only_change_refreshes_app_status() -> None:
    class Harness:
        config = {"party_mode_lyrics_enabled": False}
        party_lyrics_available = False
        party_lyrics_synchronized = False
        writes = 0

        def write_app_status(self):
            self.writes += 1

    harness = Harness()
    MusicVaultWindow.on_party_lyrics_status_changed(
        harness,
        True,
        False,
        False,
    )
    assert harness.config["party_mode_lyrics_enabled"] is True
    assert harness.writes == 1


def test_global_cache_clear_recovers_party_controller_after_io_failure(
    monkeypatch,
) -> None:
    events = []

    class Party:
        @staticmethod
        def prepare_lyrics_cache_clear():
            events.append("suspend")

        @staticmethod
        def lyrics_cache_cleared():
            events.append("reload_local")

    class BrokenCache:
        def __init__(self, _root):
            pass

        @staticmethod
        def clear_automatic():
            raise OSError("synthetic failure")

    class Harness:
        party_mode_window = Party()

        @staticmethod
        def refresh_lyrics_cache_status():
            events.append("refresh")

    monkeypatch.setattr(app_module.QMessageBox, "question", lambda *_args: app_module.QMessageBox.Yes)
    monkeypatch.setattr(app_module.QMessageBox, "warning", lambda *_args: events.append("warning"))
    monkeypatch.setattr(app_module, "LyricsCache", BrokenCache)
    monkeypatch.setattr(app_module, "data_dir", lambda: Path("synthetic-data"))

    MusicVaultWindow.clear_lyrics_cache(Harness())
    assert events == ["suspend", "warning", "reload_local"]


def test_batch9_1_review_scene_matrix_is_exact_and_synthetic() -> None:
    from tools.dev.run_party_mode_9_1_review import LYRIC_SCENES, MOTION_SCENES

    names = [scene.name for scene in MOTION_SCENES]
    names.extend(scene[0] for scene in LYRIC_SCENES)
    assert len(names) == 22
    assert len(set(names)) == 22
    assert names == [
        "01_static",
        "02_starfield_fixed_album",
        "03_aurora_low",
        "04_aurora_high",
        "05_orb_compressed",
        "06_orb_mid_expansion",
        "07_orb_full_expansion",
        "08_orb_mid_contraction",
        "09_orb_accent_subset",
        "10_firework_initial",
        "11_firework_expanded",
        "12_firework_falling",
        "13_firework_fading",
        "14_pulse_minimum",
        "15_pulse_maximum",
        "22_reduced_orb_cluster",
        "16_synced_previous_current_next",
        "17_synced_controls_hidden",
        "18_unsynced_lyrics",
        "19_no_lyrics",
        "20_instrumental",
        "21_long_lyric_line",
    ]
