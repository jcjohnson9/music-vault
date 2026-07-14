from __future__ import annotations

import importlib.util
import json
import wave
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow

from music_vault.core.db import MusicVaultDB
from music_vault.ui import review
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
