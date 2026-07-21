from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy, QTest

from music_vault.core import paths
from music_vault.core.playback_state import (
    DEFAULT_VOLUME_PERCENT,
    config_for_persistence,
    normalize_volume_percent,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_VOLUME_PERCENT),
        (64, 64),
        (0, 0),
        (100, 100),
        (-4, 0),
        (104, 100),
        ("23", 23),
        (" 81 ", 81),
        ("bad", DEFAULT_VOLUME_PERCENT),
        ("", DEFAULT_VOLUME_PERCENT),
        (True, DEFAULT_VOLUME_PERCENT),
        (float("nan"), DEFAULT_VOLUME_PERCENT),
    ],
)
def test_normalize_volume_percent(value, expected):
    assert normalize_volume_percent(value) == expected


def test_config_persistence_preserves_unrelated_keys_and_removes_secrets():
    config = {
        "volume_percent": 23,
        "audio_quality": "320",
        "custom_setting": {"kept": True},
        "api_key": "synthetic-secret-one",
        "youtube_api_key": "synthetic-secret-two",
        "youtube_api_key_value": "synthetic-secret-three",
    }
    saved = config_for_persistence(config)
    assert saved["custom_setting"] == {"kept": True}
    assert saved["audio_quality"] == "320"
    assert saved["volume_percent"] == 23
    assert not {"api_key", "youtube_api_key", "youtube_api_key_value"} & saved.keys()
    assert "synthetic-secret" not in json.dumps(saved)


@pytest.fixture
def isolated_window_factory(tmp_path: Path, monkeypatch, qapp):
    root = tmp_path / "runtime"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(root))
    paths._resolved_project_root.cache_clear()

    from music_vault.app import MusicVaultWindow

    monkeypatch.setattr(MusicVaultWindow, "use_system_default_audio_output", lambda self: None)

    windows = []

    def create(config: dict | None = None):
        config_path = data / "music_vault_config.json"
        if config is not None:
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        window = MusicVaultWindow()
        windows.append(window)
        return window, config_path

    yield create

    for window in windows:
        try:
            window.volume_save_timer.stop()
            window.db.close()
            window.deleteLater()
        except Exception:
            pass
    qapp.processEvents()
    paths._resolved_project_root.cache_clear()


def test_missing_volume_uses_existing_default_without_startup_write(
    isolated_window_factory,
):
    window, config_path = isolated_window_factory({"custom_setting": "kept"})
    before = config_path.read_text(encoding="utf-8")
    assert window.volume_slider.value() == DEFAULT_VOLUME_PERCENT
    assert window.audio_output.volume() == pytest.approx(DEFAULT_VOLUME_PERCENT / 100)
    assert config_path.read_text(encoding="utf-8") == before
    assert not window.volume_save_timer.isActive()


def test_saved_volume_initializes_slider_and_audio_consistently(
    isolated_window_factory,
):
    window, _ = isolated_window_factory({"volume_percent": "23"})
    assert window.volume_slider.value() == 23
    assert window.audio_output.volume() == pytest.approx(0.23)
    assert window.config["volume_percent"] == 23


def test_volume_movement_updates_audio_immediately_and_debounces_final_write(
    isolated_window_factory,
):
    window, config_path = isolated_window_factory({"volume_percent": 75})
    original_save = window.save_config
    writes = []

    def counted_save():
        writes.append(window.config["volume_percent"])
        original_save()

    window.save_config = counted_save
    timeout_spy = QSignalSpy(window.volume_save_timer.timeout)
    window.volume_slider.setValue(10)
    window.volume_slider.setValue(20)
    window.volume_slider.setValue(30)

    assert window.audio_output.volume() == pytest.approx(0.30)
    assert writes == []
    assert window.volume_save_timer.isActive()

    assert timeout_spy.wait(2_000)
    assert writes == [30]
    assert json.loads(config_path.read_text(encoding="utf-8"))["volume_percent"] == 30


def test_close_flushes_latest_pending_volume_once(isolated_window_factory, qapp):
    window, config_path = isolated_window_factory(
        {"volume_percent": 50, "custom_setting": "survives"}
    )
    original_save = window.save_config
    writes = []

    def counted_save():
        writes.append(window.config["volume_percent"])
        original_save()

    window.save_config = counted_save
    window.show()
    window.volume_slider.setValue(41)
    assert writes == []
    window.close()
    qapp.processEvents()
    QTest.qWait(600)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert writes == [41]
    assert saved["volume_percent"] == 41
    assert saved["custom_setting"] == "survives"


def test_persisted_final_volume_restores_on_restart(isolated_window_factory, qapp):
    first, config_path = isolated_window_factory({"volume_percent": 12})
    first.show()
    first.volume_slider.setValue(67)
    first.close()
    qapp.processEvents()
    first.db.close()

    second, _ = isolated_window_factory(None)
    assert json.loads(config_path.read_text(encoding="utf-8"))["volume_percent"] == 67
    assert second.volume_slider.value() == 67
    assert second.audio_output.volume() == pytest.approx(0.67)


def test_volume_save_never_persists_synthetic_api_key_content(
    isolated_window_factory,
):
    window, config_path = isolated_window_factory(
        {
            "volume_percent": 22,
            "custom_setting": "kept",
            "api_key": "synthetic-api-key-content",
        }
    )
    window.on_volume_changed(24)
    assert window.flush_pending_volume_save()
    raw = config_path.read_text(encoding="utf-8")
    saved = json.loads(raw)
    assert saved["custom_setting"] == "kept"
    assert saved["volume_percent"] == 24
    assert "synthetic-api-key-content" not in raw
    assert "api_key" not in saved
