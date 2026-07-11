from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton

from music_vault.app import MusicVaultWindow
from music_vault.core.library_browser import BrowserInvalidationReason
from music_vault.metadata.remediation import ApplyEstimate, candidate_review_token
from music_vault.ui import metadata_remediation as remediation_ui
from music_vault.ui.metadata_remediation import MetadataRemediationDialog


@dataclass(frozen=True)
class _Summary:
    id: str = "synthetic-job"
    status: str = "ready"
    total: int = 3
    analyzed: int = 3
    high_confidence: int = 1
    needs_review: int = 1
    ambiguous: int = 1
    no_match: int = 0
    skipped: int = 0
    failed: int = 0
    applied: int = 0
    file_written: int = 0
    rolled_back: int = 0


def _snapshot(title: str, artist: str, private_path: str) -> dict:
    return {
        "path": private_path,
        "fields": {
            "title": {"value": title},
            "artist": {"value": artist},
            "album": {"value": "Synthetic Current Album"},
            "album_artist": {"value": artist},
            "release_date": {"value": None},
            "artwork": {"value": None},
        },
    }


class _FakeService:
    def __init__(self) -> None:
        self.summary = _Summary()
        self.calls: list[tuple] = []
        self.worker_thread_ids: list[int] = []
        private = r"C:\private\library\must-not-render.mp3"
        self.items = [
            {
                "id": 1,
                "track_id": 101,
                "status": "needs_review",
                "confidence_class": "needs_review",
                "confidence_score": 88.0,
                "current_snapshot": _snapshot("Current Synthetic", "Current Artist", private),
                "candidate_snapshot": {
                    "title": "Candidate Synthetic",
                    "artist": "Candidate Artist",
                    "album": "Candidate Album",
                },
                "proposed_patch": {"title": "Candidate Synthetic"},
                "match_reasons": ["duration_unavailable"],
            },
            {
                "id": 2,
                "track_id": 102,
                "status": "high_confidence",
                "confidence_class": "high_confidence",
                "confidence_score": 98.0,
                "current_snapshot": _snapshot("Trusted Current", "Trusted Artist", private),
                "candidate_snapshot": {
                    "title": "Trusted Candidate",
                    "artist": "Trusted Artist",
                },
                "proposed_patch": {"title": "Trusted Candidate"},
                "match_reasons": ["strict_high_confidence_match"],
            },
            {
                "id": 3,
                "track_id": 103,
                "status": "ambiguous",
                "confidence_class": "ambiguous",
                "confidence_score": 72.0,
                "current_snapshot": _snapshot("Ambiguous Current", "Current Artist", private),
                "candidate_snapshot": {
                    "title": "Ambiguous Candidate",
                    "artist": "Possible Artist",
                },
                "proposed_patch": {},
                "match_reasons": ["candidate_not_unique"],
            },
        ]

    def status(self, _job_id=None):
        return self.summary

    def list_items(self, _job_id, **_kwargs):
        return [dict(item) for item in self.items]

    def estimate_apply(self, _job_id):
        return ApplyEstimate(1, 1, 1, 4096, 4096, 9830, 2, 2)

    def analyze(self, _job_id=None, *, progress=None):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("analyze", _job_id))
        analyzing = replace(self.summary, status="analyzing", analyzed=1)
        if progress:
            progress(analyzing)
        self.summary = replace(self.summary, status="ready", analyzed=3)
        return self.summary, SimpleNamespace(provider_requests=0)

    def pause(self, _job_id):
        self.calls.append(("pause",))
        self.summary = replace(self.summary, status="paused")
        return self.summary

    def resume(self, _job_id, *, progress=None):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("resume",))
        self.summary = replace(self.summary, status="ready")
        if progress:
            progress(self.summary)
        return self.summary, SimpleNamespace(provider_requests=0)

    def cancel(self, _job_id):
        self.calls.append(("cancel",))
        self.summary = replace(self.summary, status="cancelled")
        return self.summary

    def retry_failed(self, _job_id, *, progress=None):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("retry",))
        self.summary = replace(self.summary, status="ready", failed=0)
        if progress:
            progress(self.summary)
        return self.summary, SimpleNamespace(provider_requests=0)

    def retry_item_with_query(self, _job_id, item_id, title, artist):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("retry_query", item_id, title, artist))
        return self.summary, SimpleNamespace(provider_requests=1)

    def prepare_review_artwork(self, _job_id, item_id):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("artwork_preview", item_id))
        item = next(value for value in self.items if value["id"] == item_id)
        return {
            "item_id": item_id,
            "artwork_path": "",
            "candidate_token": candidate_review_token(item["candidate_snapshot"]),
        }

    def skip_items(self, _job_id, item_ids):
        selected = set(item_ids)
        self.calls.append(("skip", tuple(sorted(selected))))
        for item in self.items:
            if item["id"] in selected:
                item["status"] = "skipped"
                item["confidence_class"] = "skipped"
        return self.summary

    def reject_candidates(self, _job_id, item_ids):
        selected = set(item_ids)
        self.calls.append(("reject", tuple(sorted(selected))))
        for item in self.items:
            if item["id"] in selected:
                item["status"] = "skipped"
                item["confidence_class"] = "skipped"
        return self.summary

    def keep_current_items(self, _job_id, item_ids):
        selected = set(item_ids)
        self.calls.append(("keep_current", tuple(sorted(selected))))
        for item in self.items:
            if item["id"] in selected:
                item["status"] = "skipped"
                item["confidence_class"] = "skipped"
        return self.summary

    def approve_review_item(
        self,
        _job_id,
        item_id,
        fields,
        *,
        confirmed=False,
        write_files=False,
        expected_candidate_token=None,
    ):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(
            (
                "approve",
                item_id,
                tuple(fields),
                confirmed,
                write_files,
                expected_candidate_token,
            )
        )
        for item in self.items:
            if item["id"] == item_id:
                item["status"] = "applied"
        self.summary = replace(self.summary, applied=self.summary.applied + 1)
        return self.summary

    def apply_high_confidence(
        self,
        _job_id,
        *,
        confirmed=False,
        write_files=False,
        progress=None,
    ):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("apply", confirmed, write_files))
        for item in self.items:
            if item["confidence_class"] == "high_confidence":
                item["status"] = "applied"
        self.summary = replace(
            self.summary,
            status="complete",
            applied=1,
            file_written=1 if write_files else 0,
        )
        if progress:
            progress(self.summary)
        return self.summary, self.estimate_apply(_job_id)

    def rollback(self, _job_id, *, confirmed=False, progress=None):
        self.worker_thread_ids.append(threading.get_ident())
        self.calls.append(("rollback", confirmed))
        for item in self.items:
            if item["status"] == "applied":
                item["status"] = "rolled_back"
        self.summary = replace(
            self.summary,
            status="rolled_back",
            applied=0,
            rolled_back=1,
        )
        if progress:
            progress(self.summary)
        return self.summary

    def clear_completed_job(self, _job_id):
        self.calls.append(("clear",))
        self.summary = None


def _dialog(tmp_path: Path, qapp):
    service = _FakeService()
    database = SimpleNamespace(
        db_path=tmp_path / "synthetic.sqlite3",
        backup_dir=tmp_path / "backups",
    )
    opened: list[Path] = []
    dialog = MetadataRemediationDialog(
        database,
        service=service,
        service_factory=lambda: service,
        open_folder=opened.append,
    )
    dialog.show()
    qapp.processEvents()
    return dialog, service, opened


def _wait(qapp, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Timed out waiting for remediation UI task.")


def test_dashboard_has_required_controls_aggregates_and_path_free_table(tmp_path, qapp):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    try:
        labels = {button.text() for button in dialog.findChildren(QPushButton)}
        assert {
            "Analyze Library",
            "Pause",
            "Resume",
            "Cancel Analysis",
            "Apply High Confidence",
            "Review Selected",
            "Skip Selected",
            "Reject Candidate",
            "Keep Current",
            "Approve Selected",
            "Retry Failed",
            "Undo Applied Job",
            "Open Private Report Folder",
            "Clear Completed Job",
        } <= labels
        assert dialog.metric_cards["total"].value_label.text() == "3"
        assert dialog.metric_cards["high_confidence"].value_label.text() == "1"
        rendered = "\n".join(
            dialog.items_table.item(row, column).text()
            for row in range(dialog.items_table.rowCount())
            for column in range(dialog.items_table.columnCount())
        )
        assert "must-not-render" not in rendered
        assert "C:\\private" not in rendered
        assert service.calls == []
    finally:
        dialog.close()


def test_analysis_runs_off_gui_thread_and_does_not_start_automatically(tmp_path, qapp):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    main_thread = threading.get_ident()
    try:
        assert service.calls == []
        dialog.analyze_library()
        _wait(qapp, lambda: dialog.task_runner.pending_count == 0)
        assert service.calls == [("analyze", None)]
        assert service.worker_thread_ids and service.worker_thread_ids[-1] != main_thread
        assert dialog.progress_bar.value() == 3
    finally:
        dialog.close()


def test_resume_is_available_for_all_interrupted_lifecycle_states(tmp_path, qapp):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    try:
        for status in ("analyzing", "applying", "rolling_back"):
            service.summary = replace(service.summary, status=status)
            dialog.refresh_dashboard()
            assert dialog.resume_button.isEnabled()
        service.summary = replace(service.summary, status="ready", failed=1)
        dialog.refresh_dashboard()
        assert dialog.resume_button.isEnabled()
    finally:
        dialog.close()


def test_apply_confirmation_includes_disk_estimate_and_emits_changed_tracks(
    tmp_path,
    qapp,
    monkeypatch,
):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    prompts: list[tuple[str, str]] = []
    changed: list[tuple[int, ...]] = []
    dialog.tracks_changed.connect(lambda values: changed.append(tuple(values)))

    def accept(_parent, title, message, *_args, **_kwargs):
        prompts.append((title, message))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", accept)
    try:
        dialog.write_files_checkbox.setChecked(True)
        dialog.apply_high_confidence()
        _wait(qapp, lambda: dialog.task_runner.pending_count == 0)
        assert ("apply", True, True) in service.calls
        assert prompts and "Backup bytes" in prompts[0][1]
        assert "Temporary disk requirement" in prompts[0][1]
        assert changed == [(102,)]
    finally:
        dialog.close()


def test_review_field_approval_skip_filter_and_rollback_are_explicit(
    tmp_path,
    qapp,
    monkeypatch,
):
    dialog, service, opened = _dialog(tmp_path, qapp)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    try:
        dialog.items_table.selectRow(0)
        dialog._selection_changed()
        assert not dialog.field_checks["title"].isChecked()
        assert not dialog.field_checks["artist"].isChecked()
        assert "Album:" in dialog.review_detail.text()
        assert "Duration:" in dialog.review_detail.text()
        dialog.field_checks["title"].setChecked(True)
        dialog.approve_selected()
        _wait(qapp, lambda: dialog.task_runner.pending_count == 0)
        assert any(call[:2] == ("approve", 1) for call in service.calls)
        approve_call = next(call for call in service.calls if call[:2] == ("approve", 1))
        assert approve_call[-1] == candidate_review_token(
            service.items[0]["candidate_snapshot"]
        )

        dialog.filter_combo.setCurrentIndex(dialog.filter_combo.findData("ambiguous"))
        qapp.processEvents()
        assert dialog.items_table.rowCount() == 1
        dialog.items_table.selectRow(0)
        dialog.skip_selected()
        assert any(call[0] == "skip" for call in service.calls)

        service.summary = replace(service.summary, status="complete", applied=1)
        for item in service.items:
            if item["id"] == 2:
                item["status"] = "applied"
        dialog.filter_combo.setCurrentIndex(dialog.filter_combo.findData("all"))
        dialog.refresh_dashboard()
        dialog.rollback_job()
        _wait(qapp, lambda: dialog.task_runner.pending_count == 0)
        assert ("rollback", True) in service.calls

        monkeypatch.setattr(remediation_ui, "metadata_reports_dir", lambda: tmp_path / "reports")
        dialog.open_private_report()
        assert opened == [tmp_path / "reports" / "synthetic-job"]
    finally:
        dialog.close()


def test_reject_skip_and_keep_current_are_distinct_review_decisions(tmp_path, qapp):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    try:
        dialog.items_table.selectRow(0)
        dialog.reject_selected()
        assert ("reject", (1,)) in service.calls

        dialog.items_table.selectRow(1)
        dialog.skip_selected()
        assert ("skip", (2,)) in service.calls

        dialog.items_table.selectRow(2)
        dialog.keep_current_selected()
        assert ("keep_current", (3,)) in service.calls
    finally:
        dialog.close()


def test_review_renders_all_fields_real_artwork_and_batch6_edit_action(tmp_path, qapp):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    current_art = tmp_path / "current.png"
    candidate_art = tmp_path / "candidate.png"
    for path, color in ((current_art, "#2255AA"), (candidate_art, "#22AA55")):
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(color))
        assert pixmap.save(str(path), "PNG")
    item = service.items[0]
    item["current_snapshot"]["fields"]["artwork"]["value"] = str(current_art)
    item["current_snapshot"]["duration_seconds"] = 200.0
    item["current_snapshot"]["source_kind"] = "youtube"
    item["candidate_snapshot"].update(
        {
            "album_artist": "Candidate Album Artist",
            "release_date": "2001-02-03",
            "duration_seconds": 203.0,
            "artwork_available": True,
            "alternatives": [
                {
                    "album": "Synthetic Candidate Release Choice",
                    "album_artist": "Candidate Album Artist",
                    "release_date": "2001-02-03",
                    "release_status": "Official",
                    "release_id": "synthetic-release-choice",
                    "recording_id": "synthetic-recording-choice",
                }
            ],
        }
    )
    item["artwork_candidate"] = {
        "preview_path": str(candidate_art),
        "candidate_token": candidate_review_token(item["candidate_snapshot"]),
    }
    edited: list[int] = []
    dialog.edit_track_requested.connect(edited.append)
    try:
        dialog.refresh_dashboard()
        dialog.items_table.selectRow(0)
        dialog._selection_changed()

        detail = dialog.review_detail.text()
        assert "Album:" in detail
        assert "Album Artist:" in detail
        assert "Release Date:" in detail
        assert "Duration: 200.0s -> 203.0s" in detail
        assert "Source observations: youtube" in detail
        assert "Release choices: 1" in detail
        release_choices = "\n".join(
            dialog.release_choices_combo.itemText(index)
            for index in range(dialog.release_choices_combo.count())
        )
        assert "Synthetic Candidate Release Choice" in release_choices
        assert "synthetic-release-choice" in release_choices
        assert "synthetic-recording-choice" in release_choices
        assert dialog.current_art_preview.pixmap() is not None
        assert not dialog.current_art_preview.pixmap().isNull()
        assert dialog.candidate_art_preview.pixmap() is not None
        assert not dialog.candidate_art_preview.pixmap().isNull()
        assert all(not checkbox.isChecked() for checkbox in dialog.field_checks.values())

        dialog.edit_selected()
        assert edited == [101]
    finally:
        dialog.close()


def test_retry_search_uses_edited_query_without_metadata_write(tmp_path, qapp, monkeypatch):
    dialog, service, _opened = _dialog(tmp_path, qapp)
    responses = iter((("Edited Search Title", True), ("Edited Search Artist", True)))
    monkeypatch.setattr(
        remediation_ui.QInputDialog,
        "getText",
        lambda *_args, **_kwargs: next(responses),
    )
    try:
        dialog.items_table.selectRow(0)
        dialog._artwork_previews[1] = (
            candidate_review_token(service.items[0]["candidate_snapshot"]),
            str(tmp_path / "old-candidate.png"),
        )
        dialog.retry_search()
        _wait(qapp, lambda: dialog.task_runner.pending_count == 0)
        assert (
            "retry_query",
            1,
            "Edited Search Title",
            "Edited Search Artist",
        ) in service.calls
        assert 1 not in dialog._artwork_previews
    finally:
        dialog.close()


def test_app_refresh_hook_preserves_queue_and_updates_current_track():
    class _Label:
        def __init__(self):
            self.text = None

        def setText(self, value):
            self.text = value

    queue = [7, 8]
    context = {"track_ids": [1, 2, 3], "current_track_id": 1}
    calls: list[tuple] = []
    fake = SimpleNamespace(
        manual_queue=queue,
        base_playback_context=context,
        current_track_id=2,
        current_view_kind="library",
        db=SimpleNamespace(
            get_track=lambda track_id: {
                "title": "Remediated Title",
                "artist": "Remediated Artist",
                "path": "synthetic.mp3",
                "cover_path": None,
            }
            if track_id == 2
            else None
        ),
        now_title=_Label(),
        now_artist=_Label(),
        invalidate_browser_data=lambda reason: calls.append(("invalidate", reason)),
        refresh_visible_track_metadata=lambda track_id: calls.append(("refresh", track_id)),
        set_cover_art=lambda path: calls.append(("cover", path)),
        write_app_status=lambda: calls.append(("status",)),
    )

    MusicVaultWindow.remediation_tracks_changed(fake, (2, 3))

    assert queue == [7, 8]
    assert context == {"track_ids": [1, 2, 3], "current_track_id": 1}
    assert ("invalidate", BrowserInvalidationReason.FUTURE_METADATA) in calls
    assert ("refresh", 2) in calls and ("refresh", 3) in calls
    assert fake.now_title.text == "Remediated Title"
    assert fake.now_artist.text == "Remediated Artist"
