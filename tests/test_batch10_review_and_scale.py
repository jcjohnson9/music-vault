from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

import pytest

from PySide6.QtWidgets import QLabel, QTableWidget, QVBoxLayout, QWidget

from music_vault.ui import review
from music_vault.ui.review import (
    MULTI_SOURCE_REVIEW_SCENES,
    REVIEW_SCHEMA_VERSION,
    ReviewPlan,
    SCENE_LABELS,
    load_review_plan,
    multi_source_review_metrics,
    prepare_review_scene,
    review_scene_ready,
)
from music_vault.ui.sync_center import SyncCenterWidget


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_tool(name: str):
    path = PROJECT_ROOT / "tools" / "dev" / name
    spec = importlib.util.spec_from_file_location(f"batch10_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch10_review_matrix_is_exact_bounded_and_unique(tmp_path: Path) -> None:
    assert len(MULTI_SOURCE_REVIEW_SCENES) == 9
    assert len(set(MULTI_SOURCE_REVIEW_SCENES)) == 9
    assert all(scene in SCENE_LABELS for scene in MULTI_SOURCE_REVIEW_SCENES)

    runtime = tmp_path / "runtime"
    output = tmp_path / "output"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    request = runtime / "plan.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "runtime_root": str(runtime),
                "output_dir": str(output),
                "sizes": [{"width": 1440, "height": 900}],
                "scenes": list(MULTI_SOURCE_REVIEW_SCENES),
                "settle_ms": 100,
                "expected_capture_count": 9,
            }
        ),
        encoding="utf-8",
    )
    plan: ReviewPlan = load_review_plan(request)
    assert plan.scenes == MULTI_SOURCE_REVIEW_SCENES
    assert plan.capture_count == 9


class _Pages:
    def __init__(self) -> None:
        self.current = None

    def setCurrentWidget(self, page) -> None:
        self.current = page


class _SyncReviewWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1440, 900)
        self.pages = _Pages()
        self.sync_page = QWidget(self)
        layout = QVBoxLayout(self.sync_page)
        self.sync_center = SyncCenterWidget()
        layout.addWidget(self.sync_center)
        outer = QVBoxLayout(self)
        outer.addWidget(self.sync_page)


def test_sync_center_review_states_use_display_only_hooks(qapp) -> None:
    window = _SyncReviewWindow()
    window.show()
    scenes = tuple(
        scene for scene in MULTI_SOURCE_REVIEW_SCENES if scene != "sync_managed_playlist"
    )
    try:
        for scene in scenes:
            prepare_review_scene(window, scene)
            qapp.processEvents()
            assert review_scene_ready(window, scene)
            metrics = multi_source_review_metrics(window, scene)
            assert metrics is not None
            assert metrics["per_source_widget_count"] == 0
            assert metrics["private_path_visible"] is False
            assert metrics["api_key_field_visible"] is False
            assert metrics["clipped_action_count"] == 0
            if scene == "sync_sources_empty":
                assert metrics["source_row_count"] == 0
            else:
                assert metrics["source_row_count"] == 3
                assert metrics["selected_source_count"] >= 1
                assert metrics["disabled_source_count"] == 1
        review._close_review_sync_dialog(window)
    finally:
        window.close()


def test_managed_playlist_review_metrics_require_badge_and_explanation(qapp) -> None:
    window = QWidget()
    layout = QVBoxLayout(window)
    window.playlist_managed_badge = QLabel("Source Managed")
    explanation = QLabel(
        "Managed by a saved source. Manual additions remain after synchronized tracks."
    )
    window.library_table = QTableWidget(2, 1)
    layout.addWidget(window.playlist_managed_badge)
    layout.addWidget(explanation)
    layout.addWidget(window.library_table)
    window.show()
    try:
        qapp.processEvents()
        metrics = multi_source_review_metrics(window, "sync_managed_playlist")
        assert metrics is not None
        assert metrics["managed_badge_visible"] is True
        assert metrics["managed_explanation_present"] is True
        assert metrics["playlist_track_count"] == 2
    finally:
        window.close()


def test_batch10_runner_has_nine_states_three_sizes_and_one_scaled_capture() -> None:
    tool = _load_tool("run_batch10_review.py")
    scenes = [scene for group in tool.CAPTURE_GROUPS for scene in group["scenes"]]
    sizes = {tuple(group["size"]) for group in tool.CAPTURE_GROUPS}
    scaled = [group for group in tool.CAPTURE_GROUPS if group["scale"] == 1.5]
    assert len(scenes) == 9
    assert set(scenes) == set(MULTI_SOURCE_REVIEW_SCENES)
    assert sizes == {(1280, 720), (1440, 900), (1920, 1080)}
    assert len(scaled) == 1
    assert len(scaled[0]["scenes"]) == 1


def test_batch10_runner_owns_cleanup_root_and_rejects_nonempty_output() -> None:
    tool = _load_tool("run_batch10_review.py")
    unsafe = Path(tempfile.mkdtemp(prefix=tool.OUTPUT_PREFIX))
    sentinel = unsafe / "belongs_to_user.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="non-empty"):
            tool._output_directory(unsafe)
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
    finally:
        sentinel.unlink()
        unsafe.rmdir()

    owned = Path(tempfile.mkdtemp(prefix=tool.OUTPUT_PREFIX))
    output, token = tool._output_directory(owned)
    assert tool._verify_owned_output(output, token) == output.resolve()
    tool._safe_delete_output(output, token)
    assert not output.exists()


def test_multiple_source_profiler_small_case_is_indexed_and_widget_free(
    tmp_path: Path,
    qapp,
) -> None:
    tool = _load_tool("profile_multiple_sources.py")
    result = tool.profile_case(
        qapp,
        tmp_path,
        name="test_small",
        source_count=1,
        unique_video_count=100,
        membership_count=100,
    )
    assert result["schema_version"] == 6
    assert result["integrity"] == "ok"
    assert result["membership_row_count"] == 100
    assert result["unique_video_count"] == 100
    assert result["indexed_source_query"] is True
    assert result["required_indexes_present"] is True
    assert result["per_source_widget_count"] == 0
    assert result["bounded_activity_line_count"] <= 100
    assert all(tool._global_scan_structure().values())
