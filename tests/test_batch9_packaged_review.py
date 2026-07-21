from __future__ import annotations

import importlib.util
import json
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow

from music_vault.core.db import MusicVaultDB
from music_vault.lyrics.models import LyricsQuery, LyricsStatus, TrackLyricsIdentity
from music_vault.ui import review
from music_vault.ui.party_mode import normalize_party_mode_settings
from music_vault.ui.review import (
    PARTY_REVIEW_SCENES,
    REVIEW_SCHEMA_VERSION,
    ReviewPlan,
    ReviewSize,
    SCENE_LABELS,
    load_review_plan,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _capture_module():
    path = PROJECT_ROOT / "tools" / "dev" / "capture_ui_review.py"
    spec = importlib.util.spec_from_file_location("batch9_capture_ui_review", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_runtime(tmp_path: Path):
    tool = _capture_module()
    runtime = tmp_path / "synthetic_runtime"
    (runtime / "data").mkdir(parents=True)
    (runtime / "profile").mkdir()
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    result = tool.seed_synthetic_runtime(PROJECT_ROOT, runtime, include_party=True)
    return tool, runtime, result


def test_party_review_scene_is_explicit_and_plan_bounded(tmp_path: Path) -> None:
    assert PARTY_REVIEW_SCENES == ("party_mode_smoke",)
    assert PARTY_REVIEW_SCENES[0] in SCENE_LABELS
    runtime = tmp_path / "runtime"
    output = tmp_path / "output"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    request = runtime / "plan.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "runtime_root": str(runtime),
                "output_dir": str(output),
                "sizes": [{"width": 1280, "height": 720}],
                "scenes": list(PARTY_REVIEW_SCENES),
                "settle_ms": 100,
                "expected_capture_count": 1,
            }
        ),
        encoding="utf-8",
    )
    plan = load_review_plan(request)
    assert plan.scenes == PARTY_REVIEW_SCENES
    assert plan.capture_count == 1


def test_party_review_seed_contains_two_bounded_wavs_and_no_decoded_dump(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tool, runtime, result = _seed_runtime(tmp_path)
    assert result["track_count"] == 300
    assert result["synthetic_party_wav_count"] == 2
    assert result["synthetic_party_lrc_count"] == 1
    assert result["synthetic_party_txt_count"] == 1
    wavs = sorted((runtime / "data" / "synthetic_party_mode").glob("*.wav"))
    assert len(wavs) == 2
    for wav_path in wavs:
        assert wav_path.stat().st_size <= 5 * 1024 * 1024
        with wave.open(str(wav_path), "rb") as source:
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            assert source.getframerate() == tool.PARTY_REVIEW_SAMPLE_RATE
            assert source.getnframes() == (
                tool.PARTY_REVIEW_SAMPLE_RATE
                * tool.PARTY_REVIEW_DURATION_SECONDS
            )
    assert not list((runtime / "data").rglob("*.pcm"))
    assert not list((runtime / "data").rglob("*.raw"))
    assert not (runtime / "data" / "youtube_api_key.txt").exists()
    party_root = runtime / "data" / "synthetic_party_mode"
    lyric_files = sorted([*party_root.glob("*.lrc"), *party_root.glob("*.txt")])
    assert len(lyric_files) == 2
    assert all(
        path.resolve().is_relative_to(runtime.resolve())
        and 0 < path.stat().st_size <= 64 * 1024
        and not path.is_symlink()
        for path in lyric_files
    )
    seeded_config = json.loads(
        (runtime / "data" / "music_vault_config.json").read_text(encoding="utf-8")
    )
    assert "party_mode_config_version" not in seeded_config
    assert seeded_config["party_mode_preset"] == "pulse"
    normalized = normalize_party_mode_settings(seeded_config)
    assert normalized["party_mode_config_version"] == 2
    assert normalized["party_mode_preset"] == "static"

    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    db = MusicVaultDB(
        runtime / "data" / "music_vault.sqlite3",
        backup_dir=runtime / "data" / "backups",
    )
    plan = ReviewPlan(
        request_path=runtime / "plan.json",
        runtime_root=runtime.resolve(),
        output_dir=(tmp_path / "output").resolve(),
        sizes=(ReviewSize(1280, 720),),
        scenes=PARTY_REVIEW_SCENES,
        settle_ms=100,
    )
    try:
        fixture = review._party_review_fixture(SimpleNamespace(db=db), plan)
        assert len(fixture["track_ids"]) == 2
        assert fixture["queue_track_id"] not in fixture["track_ids"]
        assert all(
            Path(str(track["path"])).resolve().is_relative_to(runtime.resolve())
            for track in fixture["tracks"]
        )
        assert Path(fixture["synced_sidecar"]).suffix.casefold() == ".lrc"
        assert Path(fixture["plain_sidecar"]).suffix.casefold() == ".txt"
    finally:
        db.close()


def test_review_key_event_activates_real_application_f11_shortcut(qapp) -> None:
    window = QMainWindow()
    shortcut = QShortcut(QKeySequence(Qt.Key.Key_F11), window)
    shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
    hits: list[bool] = []
    shortcut.activated.connect(lambda: hits.append(True))
    window.show()
    try:
        qapp.processEvents()
        review._send_review_key(window, Qt.Key.Key_F11)
        assert hits == [True]
    finally:
        window.close()


def test_party_status_forbidden_field_scan_is_recursive() -> None:
    safe = {
        "party_mode_active": True,
        "audio_reactivity_available": False,
        "playback": {"queue_count": 1},
    }
    unsafe = {**safe, "debug": {"samples": [0.1, 0.2]}}
    assert not review._PARTY_REVIEW_FORBIDDEN_STATUS_FIELDS.intersection(
        review._status_field_names(safe)
    )
    assert review._PARTY_REVIEW_FORBIDDEN_STATUS_FIELDS.intersection(
        review._status_field_names(unsafe)
    ) == {"samples"}


def test_party_status_requires_redacted_identity_and_preserves_queue_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    status_path = runtime / "data" / "music_vault_status.json"
    status_path.parent.mkdir(parents=True)
    output = tmp_path / "output"
    plan = ReviewPlan(
        request_path=runtime / "plan.json",
        runtime_root=runtime.resolve(),
        output_dir=output.resolve(),
        sizes=(ReviewSize(1280, 720),),
        scenes=PARTY_REVIEW_SCENES,
        settle_ms=100,
    )
    monkeypatch.setattr("music_vault.core.paths.app_status_path", lambda: status_path)
    payload = {
        "party_mode_active": True,
        "party_mode_preset": "aurora",
        "party_mode_lyrics_enabled": True,
        "lyrics_available": True,
        "lyrics_synchronized": True,
        "playback": {
            "currently_playing": None,
            "current_title": None,
            "current_artist": None,
            "current_album": None,
            "queue_count": 1,
        },
        "paths": {},
    }
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    assert review._validate_party_status(plan, expected_queue_count=1)

    payload["playback"]["currently_playing"] = 42
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(review.ReviewPlanError, match="exposed playback identity"):
        review._validate_party_status(plan, expected_queue_count=1)


def test_review_only_lyrics_provider_is_bounded_and_offline(monkeypatch) -> None:
    network_calls: list[object] = []

    def network_forbidden(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("synthetic review provider attempted network access")

    monkeypatch.setattr("socket.socket.connect", network_forbidden)
    identity = TrackLyricsIdentity(
        42,
        "Synthetic Party Signal",
        "Music Vault Review",
        "Synthetic Party Mode",
        20_000,
    )
    provider = review._SyntheticReviewLyricsProvider(42)
    result = provider.lookup(LyricsQuery(identity), threading.Event())

    assert result.status is LyricsStatus.AVAILABLE
    assert result.synchronized
    assert result.identity is identity
    assert provider.call_count == 1
    assert result.provider == "synthetic_review"
    assert not network_calls
