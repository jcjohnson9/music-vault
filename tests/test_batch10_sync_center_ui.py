from __future__ import annotations

import json
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QWidget

from music_vault.app import MusicVaultWindow
from music_vault.core import app_status
from music_vault.core.db import MusicVaultDB
from music_vault.core.sync_sources import normalize_youtube_playlist_source
from music_vault.ui import sync_center as sync_center_module
from music_vault.ui.sync_center import (
    SOURCE_ID_ROLE,
    RemoveSourceDialog,
    SourceEditorDialog,
    SyncCenterController,
    SyncCenterWidget,
    explain_source_managed_removal,
)


def _source(
    source_id: int,
    *,
    enabled: bool = True,
    destination_kind: str = "library",
    destination_playlist_id: int | None = None,
    destination_playlist_name: str | None = None,
    status: str | None = "complete",
    failures: int = 0,
    sort_order: int | None = None,
) -> dict[str, object]:
    external_id = f"PL_SYNTHETIC_PLAYLIST_{source_id:04d}"
    return {
        "id": source_id,
        "source_kind": "youtube_playlist",
        "external_id": external_id,
        "source_url": f"https://www.youtube.com/playlist?list={external_id}",
        "label": f"Synthetic Source {source_id}",
        "remote_title": f"Remote Synthetic Title {source_id}",
        "enabled": enabled,
        "sort_order": source_id - 1 if sort_order is None else sort_order,
        "destination_kind": destination_kind,
        "destination_playlist_id": destination_playlist_id,
        "destination_playlist_name": destination_playlist_name,
        "storage_key": f"youtube_synthetic_{source_id:04d}",
        "last_sync_at": "2026-07-15T12:00:00Z",
        "last_sync_status": status,
        "last_downloaded_count": source_id,
        "last_imported_count": source_id + 1,
        "last_existing_count": source_id + 2,
        "last_failed_count": failures,
        "unresolved_failure_count": failures,
        "last_error": "Synthetic item needs attention" if failures else None,
    }


def _three_sources() -> list[dict[str, object]]:
    return [
        _source(1),
        _source(
            2,
            destination_kind="playlist",
            destination_playlist_id=42,
            destination_playlist_name="Managed Synthetic Mix",
            status="complete_with_issues",
            failures=2,
        ),
        _source(
            3,
            enabled=False,
            destination_kind="playlist",
            destination_playlist_id=43,
            destination_playlist_name="Disabled Synthetic Mix",
            status="failed",
            failures=1,
        ),
    ]


def test_sync_center_empty_and_three_source_states_are_lightweight(qapp) -> None:
    widget = SyncCenterWidget()
    widget.resize(1440, 900)
    widget.show()
    qapp.processEvents()

    assert widget.source_list.count() == 0
    assert widget.detail_stack.currentWidget() is widget.empty_state
    assert not widget.sync_all_button.isEnabled()

    widget.set_sources(_three_sources())
    widget.set_summary(
        {
            "enabled_sources": 2,
            "completed_sources": 1,
            "issue_sources": 1,
            "failed_sources": 1,
            "downloaded": 6,
            "existing": 12,
            "failed_items": 3,
        }
    )
    widget.source_list.setCurrentRow(1)
    widget.set_source_detail(
        _three_sources()[1],
        runs=[
            {
                "status": "complete_with_issues",
                "finished_at": "2026-07-15T12:00:00Z",
                "downloaded_count": 2,
                "existing_count": 4,
                "failed_count": 2,
            }
        ],
        failures=[{"title": "Synthetic unavailable item", "reason": "Unavailable"}],
        activity=["Synthetic source enumeration complete."],
    )
    qapp.processEvents()

    assert widget.source_list.count() == 3
    assert all(
        widget.source_list.indexWidget(widget.source_list.model().index(row, 0))
        is None
        for row in range(widget.source_list.count())
    )
    assert widget.detail_name.text() == "Synthetic Source 2"
    assert "Managed Synthetic Mix" in widget.detail_destination.text()
    assert "sources" in widget.detail_folder.text()
    assert "Synthetic unavailable item" in widget.failure_history.item(0).text()
    assert widget.summary_cards["enabled_sources"].value_label.text() == "2"
    assert widget.summary_cards["failed_items"].value_label.text() == "3"
    assert widget.source_list.item(2).checkState() == Qt.CheckState.Unchecked
    assert "disabled" in str(
        widget.source_list.item(2).data(Qt.ItemDataRole.AccessibleDescriptionRole)
    )
    assert all(
        widget.source_list.indexWidget(widget.source_list.model().index(row, 0))
        is None
        for row in range(widget.source_list.count())
    )
    widget.close()


def test_sync_center_activity_documents_are_bounded(qapp) -> None:
    widget = SyncCenterWidget()
    for index in range(650):
        widget.append_activity(f"Synthetic activity {index}")
    widget.source_activity.setPlainText(
        "\n".join(f"Synthetic source activity {index}" for index in range(150))
    )
    qapp.processEvents()

    assert widget.activity_log.document().blockCount() == 500
    assert widget.source_activity.document().blockCount() == 100
    widget.close()


def test_sync_selected_filters_disabled_sources_and_batch_locks_mutation(qapp) -> None:
    widget = SyncCenterWidget()
    widget.set_sources(_three_sources())
    widget.show()
    qapp.processEvents()

    widget.source_list.clearSelection()
    widget.source_list.item(0).setSelected(True)
    widget.source_list.item(2).setSelected(True)
    emitted: list[tuple[int, ...]] = []
    widget.sync_selected_requested.connect(lambda values: emitted.append(tuple(values)))
    widget._emit_sync_selected()
    assert emitted == [(1,)]

    widget.source_list.setCurrentRow(2)
    assert not widget.move_down_button.isEnabled()
    widget.source_list.setCurrentRow(0)
    assert not widget.move_up_button.isEnabled()
    assert widget.move_down_button.isEnabled()

    first = widget.source_list.item(0)
    widget.set_batch_state(
        "syncing",
        source_index=1,
        source_count=3,
        message="Synchronizing source 1 of 3",
    )
    assert widget.stop_button.isEnabled()
    assert not widget.add_button.isEnabled()
    assert not widget.edit_button.isEnabled()
    assert not widget.remove_button.isEnabled()
    assert not widget.sync_selected_button.isEnabled()
    assert not widget.sync_all_button.isEnabled()
    first.setCheckState(Qt.CheckState.Unchecked)
    assert first.checkState() == Qt.CheckState.Checked

    widget.set_batch_state("complete_with_issues", progress=100)
    assert not widget.stop_button.isEnabled()
    assert widget.add_button.isEnabled()
    widget.close()


def test_add_and_edit_source_dialogs_validate_locally_without_starting_sync(qapp) -> None:
    playlists = [
        {"id": 42, "name": "Eligible Playlist", "managing_source_id": None},
        {"id": 43, "name": "Already Managed", "managing_source_id": 99},
    ]
    dialog = SourceEditorDialog(
        playlists=playlists,
        normalize_source=normalize_youtube_playlist_source,
    )
    dialog.show()
    qapp.processEvents()
    assert not dialog.source_value.isReadOnly()
    assert not dialog.save_button.isEnabled()
    assert dialog.playlist_mode.isHidden()

    external_id = "PL_SYNTHETIC_DIALOG_0001"
    dialog.source_value.setText(external_id)
    assert dialog.save_button.isEnabled()
    assert external_id in dialog.normalized_id.text()
    dialog.destination.setCurrentIndex(1)
    dialog.playlist_mode.setCurrentIndex(1)
    assert dialog.existing_playlist.count() == 1
    assert dialog.existing_playlist.currentData() == 42
    dialog._accept_if_valid()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.values().external_id == external_id
    dialog.close()

    edit_source = _source(
        7,
        destination_kind="playlist",
        destination_playlist_id=42,
        destination_playlist_name="Eligible Playlist",
    )
    edit_dialog = SourceEditorDialog(
        source=edit_source,
        playlists=playlists,
        normalize_source=normalize_youtube_playlist_source,
    )
    edit_dialog.show()
    qapp.processEvents()
    original_id = str(edit_source["external_id"])
    assert edit_dialog.source_value.isReadOnly()
    assert edit_dialog.values().external_id == original_id
    edit_dialog.source_value.setText("PL_PROGRAMMATIC_CHANGE_BLOCKED")
    assert edit_dialog.values().external_id == original_id
    assert edit_dialog.values().source_value == original_id
    edit_dialog.close()


def test_add_source_playlist_name_suggestion_tracks_label_until_customized(qapp) -> None:
    dialog = SourceEditorDialog(
        normalize_source=normalize_youtube_playlist_source,
    )
    dialog.show()
    qapp.processEvents()

    assert dialog.new_playlist_name.text() == "YouTube Playlist"

    dialog.label.setText("Road Trip Favorites")
    assert dialog.new_playlist_name.text() == "Road Trip Favorites"

    dialog.label.clear()
    assert dialog.new_playlist_name.text() == "YouTube Playlist"

    dialog.new_playlist_name.setFocus()
    dialog.new_playlist_name.selectAll()
    QTest.keyClicks(dialog.new_playlist_name, "My Custom Local Mix")
    assert dialog.new_playlist_name.text() == "My Custom Local Mix"

    dialog.label.setText("A Later Source Label")
    assert dialog.new_playlist_name.text() == "My Custom Local Mix"
    dialog.close()


def test_remove_source_and_managed_track_messages_explain_preservation(
    qapp,
    monkeypatch,
) -> None:
    dialog = RemoveSourceDialog(_source(1))
    dialog.show()
    qapp.processEvents()
    copy = " ".join(label.text() for label in dialog.findChildren(QLabel))
    actions = [button.text() for button in dialog.findChildren(QPushButton)]
    assert "library tracks" in copy
    assert "media" in copy
    assert "linked local playlist remain" in copy
    assert "never deleted" in copy
    assert actions == ["Remove Source", "Cancel"]
    assert not any("media" in action.casefold() for action in actions)
    dialog.close()

    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sync_center_module.QMessageBox,
        "information",
        lambda _parent, title, body: messages.append((title, body)),
    )
    explain_source_managed_removal(QWidget(), manual_origin_removed=True)
    explain_source_managed_removal(QWidget(), manual_origin_removed=False)
    assert "remains visible" in messages[0][1]
    assert "managed by the linked saved source" in messages[1][1]


def test_managed_playlist_badge_and_subtitle_are_presented(qapp) -> None:
    badge = QLabel("Managed Source")
    subtitle = QLabel("Ordinary playlist")
    harness = SimpleNamespace(
        current_view_kind="custom",
        current_playlist_id=42,
        playlist_managed_badge=badge,
        page_subtitle=subtitle,
        db=SimpleNamespace(
            list_playlists=lambda: [
                {"id": 42, "name": "Synthetic Mix", "source_managed": True}
            ]
        ),
    )

    MusicVaultWindow.update_managed_playlist_presentation(harness)
    assert not badge.isHidden()
    assert "Managed from a saved YouTube source" in subtitle.text()
    assert "Manual additions appear after source tracks" in subtitle.text()


def test_worker_preserves_stop_request_made_before_batch_becomes_active(qapp) -> None:
    progress_callback = None

    class Orchestrator:
        def __init__(self) -> None:
            self.active = False
            self.stop_requested = False

        def request_stop_after_current(self) -> bool:
            if not self.active:
                return False
            self.stop_requested = True
            return True

        def sync_all_enabled(self) -> str:
            self.active = True
            progress_callback({"phase": "batch_started"})
            assert self.stop_requested is True
            self.active = False
            return "stopped"

    orchestrator = Orchestrator()

    def factory(progress, _transition):
        nonlocal progress_callback
        progress_callback = progress
        return orchestrator

    worker = sync_center_module.MultiSourceSyncWorker(factory, None)
    worker.request_stop_after_current()
    worker.run()

    assert orchestrator.stop_requested is True


def test_app_status_exports_aggregate_source_state_and_rejects_identity_injection(
    tmp_path,
    monkeypatch,
) -> None:
    data = tmp_path / "data"
    monkeypatch.setattr(app_status, "project_root", lambda: tmp_path)
    monkeypatch.setattr(app_status, "data_dir", lambda: data)
    monkeypatch.setattr(
        app_status,
        "app_status_path",
        lambda: data / "music_vault_status.json",
    )
    monkeypatch.setattr(app_status, "config_path", lambda: data / "config.json")
    monkeypatch.setattr(app_status, "path_resolution_source", lambda: "synthetic")
    monkeypatch.setattr(app_status, "_api_ready", lambda: False)
    monkeypatch.setattr(app_status, "_ffmpeg_ready", lambda _config=None: False)
    db = MusicVaultDB(tmp_path / "music-vault.sqlite3")
    private_markers = {
        "PRIVATE_SOURCE_URL",
        "PRIVATE_SOURCE_LABEL",
        "PRIVATE_REMOTE_TITLE",
        "PRIVATE_PLAYLIST_ID",
        "PRIVATE_SOURCE_ITEM_ID",
        "PRIVATE_VIDEO_ID",
        "PRIVATE_SOURCE_FOLDER",
        "PRIVATE_ITEM_ERROR",
    }
    try:
        path = app_status.write_app_status(
            db,
            {"download_folder": str(tmp_path / "downloads")},
            {
                "sync": {
                    "sync_source_count": 3,
                    "enabled_sync_source_count": 2,
                    "last_sync_batch_status": "complete_with_issues",
                    "last_sync_batch_source_count": 3,
                    "last_sync_batch_item_failure_count": 1,
                    "source_url": "PRIVATE_SOURCE_URL",
                    "source_label": "PRIVATE_SOURCE_LABEL",
                    "remote_title": "PRIVATE_REMOTE_TITLE",
                    "last_sync_playlist_title": "PRIVATE_REMOTE_TITLE",
                    "last_sync_playlist_id": "PRIVATE_PLAYLIST_ID",
                    "source_item_id": "PRIVATE_SOURCE_ITEM_ID",
                    "video_id": "PRIVATE_VIDEO_ID",
                    "source_folder": "PRIVATE_SOURCE_FOLDER",
                    "last_sync_error": "PRIVATE_ITEM_ERROR",
                    "last_sync_failures": [
                        {
                            "title": "PRIVATE_SOURCE_ITEM_ID",
                            "reason": "PRIVATE_ITEM_ERROR",
                        }
                    ],
                }
            },
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload)
        assert payload["schema_version"] == 1
        assert payload["sync"]["sync_source_count"] == 3
        assert payload["sync"]["enabled_sync_source_count"] == 2
        assert payload["sync"]["last_sync_batch_status"] == "complete_with_issues"
        assert payload["sync"]["last_sync_playlist_title"] is None
        assert payload["sync"]["last_sync_playlist_id"] is None
        assert payload["sync"]["last_sync_error"] is None
        assert payload["sync"]["last_sync_failures"] == []
        assert not any(marker in serialized for marker in private_markers)
    finally:
        db.close()


def test_controller_rejects_new_batch_while_legacy_worker_is_running(
    qapp,
    monkeypatch,
) -> None:
    class RunningWorker:
        @staticmethod
        def isRunning() -> bool:
            return True

    parent = QWidget()
    parent.sync_worker = RunningWorker()
    widget = SyncCenterWidget(parent)
    factory_calls: list[object] = []
    notices: list[str] = []
    monkeypatch.setattr(
        sync_center_module.QMessageBox,
        "information",
        lambda _parent, _title, body: notices.append(body),
    )
    controller = SyncCenterController(
        widget,
        SimpleNamespace(),
        normalize_source=normalize_youtube_playlist_source,
        orchestrator_factory=lambda *_args: factory_calls.append(object()),
        playlist_provider=lambda: [],
        playlist_creator=lambda _name: 1,
        dialog_parent=parent,
    )

    controller._start_batch(None)
    assert controller.worker is None
    assert factory_calls == []
    assert notices and "already running" in notices[0]
    parent.close()


def test_source_specific_failure_clear_requires_confirmation_and_refreshes_counts(
    qapp,
    monkeypatch,
) -> None:
    source = SimpleNamespace(**_source(1, failures=2))

    class FakeDatabase:
        @staticmethod
        def list_sync_failures(_status, *, sync_source_id):
            assert sync_source_id == 1
            return []

    class FakeService:
        db = FakeDatabase()

        def __init__(self):
            self.cleared: list[int] = []

        def list_active(self, *, enabled_only=False):
            return [source] if not enabled_only or source.enabled else []

        @staticmethod
        def get(source_id):
            assert source_id == 1
            return source

        @staticmethod
        def recent_runs(source_id):
            assert source_id == 1
            return []

        @staticmethod
        def unresolved_failure_count(source_id):
            assert source_id == 1
            return 2

        @staticmethod
        def list_unresolved_failures(source_id):
            assert source_id == 1
            return [
                {
                    "title": "Persisted redacted occurrence",
                    "reason": "Playlist item has no usable video ID.",
                    "source_item_id": "synthetic-redacted-item",
                },
                {
                    "title": "Persisted video failure",
                    "reason": "Unavailable",
                    "source_item_id": "synthetic-video-item",
                },
            ]

        def clear_failure_history(self, source_id):
            self.cleared.append(source_id)

    service = FakeService()
    parent = QWidget()
    widget = SyncCenterWidget(parent)
    controller = SyncCenterController(
        widget,
        service,
        normalize_source=normalize_youtube_playlist_source,
        orchestrator_factory=lambda *_args: None,
        playlist_provider=lambda: [],
        playlist_creator=lambda _name: 1,
        dialog_parent=parent,
    )
    controller.refresh(preserve_detail=False)
    assert "Persisted redacted occurrence" in widget.failure_history.item(0).text()
    assert widget.failure_history.count() == 2
    changed: list[bool] = []
    controller.sources_changed.connect(lambda: changed.append(True))
    monkeypatch.setattr(
        sync_center_module.QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    controller.clear_source_failure_history(1)
    assert service.cleared == [1]
    assert changed == [True]
    parent.close()
