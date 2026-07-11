from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from music_vault.core.db import MusicVaultDB
from music_vault.core.paths import metadata_reports_dir
from music_vault.metadata.remediation import (
    ApplyEstimate,
    RemediationService,
    candidate_review_token,
)
from music_vault.ui.theme import COLORS


_SAFE_ERROR_RE = re.compile(r"^[a-z0-9_.:-]{1,100}$")
_FIELDS = (
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("album_artist", "Album Artist"),
    ("release_date", "Release Date"),
    ("artwork", "Artwork"),
)
_FILTERS = (
    ("All", "all"),
    ("High confidence", "high_confidence"),
    ("Needs review", "needs_review"),
    ("Ambiguous", "ambiguous"),
    ("No match", "no_match"),
    ("Skipped", "skipped"),
    ("Failed", "failed"),
    ("Applied", "applied"),
)


def _safe_error(exc: BaseException) -> str:
    text = str(exc or "").strip().casefold()
    return text if _SAFE_ERROR_RE.fullmatch(text) else "remediation_operation_failed"


def _value(source: object, name: str, default: object = 0) -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _result_summary(value: object) -> object:
    if isinstance(value, tuple) and value:
        return value[0]
    return value


def _format_bytes(value: object) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


@dataclass(frozen=True)
class RemediationTaskResult:
    kind: str
    request_id: int
    value: object = None
    error: str | None = None


class _WorkerSignals(QObject):
    progress = Signal(object)
    completed = Signal(object)


class _RemediationTask(QRunnable):
    def __init__(
        self,
        *,
        kind: str,
        request_id: int,
        factory: Callable[[], object],
        operation: Callable[[object, Callable[[object], None]], object],
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.request_id = request_id
        self.factory = factory
        self.operation = operation
        self.cancel_event = cancel_event
        self.signals = _WorkerSignals()

    def run(self) -> None:
        cleanup: Callable[[], object] | None = None
        try:
            resource = self.factory()
            if isinstance(resource, tuple) and len(resource) == 2:
                service, cleanup = resource
            else:
                service = resource

            def report(summary: object) -> None:
                if not self.cancel_event.is_set():
                    self.signals.progress.emit(summary)

            if self.cancel_event.is_set():
                return
            value = self.operation(service, report)
            result = RemediationTaskResult(self.kind, self.request_id, value=value)
        except Exception as exc:
            result = RemediationTaskResult(
                self.kind,
                self.request_id,
                error=_safe_error(exc),
            )
        finally:
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass
        if not self.cancel_event.is_set():
            self.signals.completed.emit(result)


class RemediationTaskRunner(QObject):
    progress = Signal(object)
    completed = Signal(object)

    def __init__(
        self,
        factory: Callable[[], object],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.factory = factory
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._next_id = 0
        self._active: dict[int, tuple[_RemediationTask, threading.Event]] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._active)

    def submit(
        self,
        kind: str,
        operation: Callable[[object, Callable[[object], None]], object],
    ) -> int:
        if self._closed:
            raise RuntimeError("Remediation task runner is closed.")
        self._next_id += 1
        request_id = self._next_id
        cancel_event = threading.Event()
        task = _RemediationTask(
            kind=kind,
            request_id=request_id,
            factory=self.factory,
            operation=operation,
            cancel_event=cancel_event,
        )
        task.signals.progress.connect(self.progress.emit)
        task.signals.completed.connect(self._finished)
        self._active[request_id] = (task, cancel_event)
        self.pool.start(task)
        return request_id

    def _finished(self, result: RemediationTaskResult) -> None:
        self._active.pop(int(result.request_id), None)
        self.completed.emit(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _task, event in self._active.values():
            event.set()
        self.pool.clear()


class _MetricCard(QFrame):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(2)
        self.value_label = QLabel("0")
        self.value_label.setObjectName("StatValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption = QLabel(label)
        caption.setObjectName("StatLabel")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setWordWrap(True)
        layout.addWidget(self.value_label)
        layout.addWidget(caption)


class MetadataRemediationDialog(QDialog):
    """Responsive, resumable Batch 7 remediation dashboard."""

    tracks_changed = Signal(object)
    edit_track_requested = Signal(int)

    def __init__(
        self,
        database: object,
        parent: QWidget | None = None,
        *,
        service: object | None = None,
        service_factory: Callable[[], object] | None = None,
        open_folder: Callable[[Path], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Library Metadata")
        self.setObjectName("MetadataRemediationDialog")
        self.setModal(False)
        self.setMinimumSize(980, 680)
        target_width = 1180
        target_height = 780
        if parent is not None:
            target_width = max(980, min(target_width, parent.width() - 40))
            target_height = max(680, min(target_height, parent.height() - 30))
        self.resize(target_width, target_height)
        self.database = database
        self.service = service or RemediationService(database)
        self._open_folder = open_folder or self._open_report_folder
        self._job_id: str | None = None
        self._busy_kind: str | None = None
        self._pending_changed_track_ids: tuple[int, ...] = ()
        self._artwork_previews: dict[int, tuple[str, str]] = {}
        self._build_ui()

        if service_factory is None:
            db_path = Path(getattr(database, "db_path"))
            backup_dir = Path(getattr(database, "backup_dir", db_path.parent / "backups"))

            def service_factory() -> tuple[RemediationService, Callable[[], None]]:
                owned = MusicVaultDB(db_path, backup_dir=backup_dir)
                return RemediationService(owned), owned.close

        self.task_runner = RemediationTaskRunner(service_factory, self)
        self.task_runner.progress.connect(self._task_progress)
        self.task_runner.completed.connect(self._task_completed)
        self.filter_combo.currentIndexChanged.connect(self.refresh_items)
        self.items_table.itemSelectionChanged.connect(self._selection_changed)
        self.refresh_dashboard()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(6)

        heading_row = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("Library metadata remediation")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Analysis is non-destructive. Only explicit approval can update the library or file tags."
        )
        subtitle.setObjectName("MutedLabel")
        subtitle.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        heading_row.addLayout(heading, 1)
        self.filter_combo = QComboBox()
        self.filter_combo.setAccessibleName("Filter remediation items")
        for label, value in _FILTERS:
            self.filter_combo.addItem(label, value)
        heading_row.addWidget(self.filter_combo)
        root.addLayout(heading_row)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(6)
        metrics.setVerticalSpacing(0)
        metric_names = (
            ("total", "Total"),
            ("analyzed", "Analyzed"),
            ("high_confidence", "High confidence"),
            ("needs_review", "Needs review"),
            ("ambiguous", "Ambiguous"),
            ("no_match", "No match"),
            ("skipped", "Skipped"),
            ("failed", "Failed"),
            ("applied", "Applied"),
            ("file_written", "File tags written"),
        )
        self.metric_cards: dict[str, _MetricCard] = {}
        for index, (name, label) in enumerate(metric_names):
            card = _MetricCard(label)
            self.metric_cards[name] = card
            metrics.addWidget(card, 0, index)
        root.addLayout(metrics)

        progress_row = QHBoxLayout()
        self.job_status = QLabel("No remediation job has been created.")
        self.job_status.setObjectName("MutedLabel")
        self.job_status.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0 / 0 analyzed")
        progress_row.addWidget(self.job_status, 1)
        progress_row.addWidget(self.progress_bar, 2)
        root.addLayout(progress_row)

        primary_controls = QHBoxLayout()
        self.analyze_button = self._button("Analyze Library", self.analyze_library, primary=True)
        self.pause_button = self._button("Pause", self.pause_analysis)
        self.resume_button = self._button("Resume", self.resume_analysis)
        self.cancel_button = self._button("Cancel Analysis", self.cancel_analysis, danger=True)
        self.retry_button = self._button("Retry Failed", self.retry_failed)
        for button in (
            self.analyze_button,
            self.pause_button,
            self.resume_button,
            self.cancel_button,
            self.retry_button,
        ):
            primary_controls.addWidget(button)
        primary_controls.addStretch(1)
        root.addLayout(primary_controls)

        self.items_table = QTableWidget(0, 6)
        # Reuse the premium Batch 4 table treatment without expanding theme.py.
        self.items_table.setObjectName("LibraryTable")
        self.items_table.setAccessibleName("Metadata remediation review items")
        self.items_table.setHorizontalHeaderLabels(
            ["Class", "Score", "Current", "Candidate", "Reasons", "State"]
        )
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.items_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.items_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.items_table.setWordWrap(False)
        self.items_table.verticalHeader().setVisible(False)
        header = self.items_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.items_table, 1)

        self.review_group = QGroupBox("Selected candidate fields")
        review_layout = QVBoxLayout(self.review_group)
        review_layout.setSpacing(4)
        self.review_detail = QLabel("Select one review item to compare its current and candidate metadata.")
        self.review_detail.setObjectName("MutedLabel")
        self.review_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.review_detail.setWordWrap(True)
        comparison_row = QHBoxLayout()
        comparison_row.addWidget(self.review_detail, 1)
        self.current_art_preview = self._art_preview("Current artwork")
        self.candidate_art_preview = self._art_preview("Candidate artwork")
        comparison_row.addWidget(self.current_art_preview)
        comparison_row.addWidget(self.candidate_art_preview)
        review_layout.addLayout(comparison_row)
        release_row = QHBoxLayout()
        release_label = QLabel("Provider release choices (review only)")
        release_label.setObjectName("MutedLabel")
        self.release_choices_combo = QComboBox()
        self.release_choices_combo.setAccessibleName(
            "Provider release choices for the selected remediation candidate"
        )
        self.release_choices_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.release_choices_combo.setMinimumContentsLength(28)
        self.release_choices_combo.addItem("No item selected")
        self.release_choices_combo.setEnabled(False)
        release_row.addWidget(release_label)
        release_row.addWidget(self.release_choices_combo, 1)
        review_layout.addLayout(release_row)
        field_row = QHBoxLayout()
        self.field_checks: dict[str, QCheckBox] = {}
        for name, label in _FIELDS:
            checkbox = QCheckBox(label)
            checkbox.setEnabled(False)
            self.field_checks[name] = checkbox
            field_row.addWidget(checkbox)
        field_row.addStretch(1)
        review_layout.addLayout(field_row)

        review_controls = QHBoxLayout()
        self.review_button = self._button("Review Selected", self.review_selected)
        self.skip_button = self._button("Skip Selected", self.skip_selected)
        self.reject_button = self._button("Reject Candidate", self.reject_selected)
        self.keep_button = self._button("Keep Current", self.keep_current_selected)
        self.edit_button = self._button("Edit Current…", self.edit_selected)
        self.retry_query_button = self._button("Retry Search…", self.retry_search)
        self.approve_button = self._button("Approve Selected", self.approve_selected, primary=True)
        self.write_files_checkbox = QCheckBox("Write supported file tags after verified backup")
        self.write_files_checkbox.setChecked(False)
        self.write_files_checkbox.setToolTip(
            "Off applies approved metadata to the Music Vault database only."
        )
        review_controls.addWidget(self.review_button)
        review_controls.addWidget(self.skip_button)
        review_controls.addWidget(self.reject_button)
        review_controls.addWidget(self.keep_button)
        review_controls.addWidget(self.edit_button)
        review_controls.addWidget(self.retry_query_button)
        review_controls.addStretch(1)
        review_layout.addLayout(review_controls)
        approval_controls = QHBoxLayout()
        approval_controls.addWidget(self.approve_button)
        approval_controls.addWidget(self.write_files_checkbox)
        approval_controls.addStretch(1)
        review_layout.addLayout(approval_controls)
        root.addWidget(self.review_group)

        footer = QHBoxLayout()
        self.disk_estimate = QLabel("Disk estimate: available after analysis")
        self.disk_estimate.setObjectName("MutedLabel")
        self.disk_estimate.setTextFormat(Qt.TextFormat.PlainText)
        self.apply_button = self._button(
            "Apply High Confidence", self.apply_high_confidence, primary=True
        )
        self.rollback_button = self._button(
            "Undo Applied Job", self.rollback_job, danger=True
        )
        self.report_button = self._button(
            "Open Private Report Folder", self.open_private_report
        )
        self.clear_button = self._button(
            "Clear Completed Job", self.clear_completed_job, danger=True
        )
        footer.addWidget(self.disk_estimate, 1)
        footer.addWidget(self.apply_button)
        footer.addWidget(self.rollback_button)
        footer.addWidget(self.report_button)
        footer.addWidget(self.clear_button)
        root.addLayout(footer)

    @staticmethod
    def _art_preview(accessible_name: str) -> QLabel:
        preview = QLabel("Not loaded")
        preview.setAccessibleName(accessible_name)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setFixedSize(92, 92)
        preview.setWordWrap(True)
        preview.setStyleSheet(
            f"border: 1px solid {COLORS['border']}; border-radius: 8px; "
            f"background: {COLORS['subtle_surface']}; color: {COLORS['text_muted']};"
        )
        return preview

    @staticmethod
    def _button(
        text: str,
        callback: Callable[[], object],
        *,
        primary: bool = False,
        danger: bool = False,
    ) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("PrimaryButton")
        elif danger:
            button.setObjectName("DangerButton")
        else:
            button.setObjectName("SecondaryButton")
        button.setAccessibleName(text)
        button.clicked.connect(callback)
        return button

    def _default_worker_factory(self) -> object:
        raise AssertionError("Worker factory was not configured.")

    @staticmethod
    def _open_report_folder(path: Path) -> bool:
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def refresh_dashboard(self) -> None:
        try:
            summary = self.service.status(self._job_id)
        except Exception as exc:
            self._show_error(exc)
            return
        if summary is None:
            self._job_id = None
            self._apply_summary(None)
            self.refresh_items()
            return
        self._job_id = str(_value(summary, "id", "")) or None
        self._apply_summary(summary)
        self.refresh_items()
        self._refresh_estimate()

    def _apply_summary(self, summary: object | None) -> None:
        aliases = {
            "high_confidence": "high_confidence",
            "needs_review": "needs_review",
            "file_written": "file_written",
        }
        for name, card in self.metric_cards.items():
            key = aliases.get(name, name)
            card.value_label.setText(str(int(_value(summary, key, 0) or 0)))
        total = int(_value(summary, "total", 0) or 0)
        analyzed = int(_value(summary, "analyzed", 0) or 0)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(min(analyzed, max(1, total)))
        self.progress_bar.setFormat(f"{analyzed} / {total} analyzed")
        status = str(_value(summary, "status", "none") or "none")
        self.job_status.setText(
            "No remediation job has been created."
            if summary is None
            else f"Job status: {status.replace('_', ' ')}"
        )
        busy = self.task_runner.pending_count > 0 if hasattr(self, "task_runner") else False
        analysis_busy = busy and self._busy_kind in {
            "analysis",
            "resume",
            "retry_failed",
        }
        self.analyze_button.setEnabled(
            not busy and status not in {"analyzing", "applying", "rolling_back"}
        )
        self.pause_button.setEnabled(
            (
                analysis_busy
                and self._job_id is not None
                and status in {"created", "ready", "analyzing"}
            )
            or (not busy and status == "analyzing")
        )
        self.resume_button.setEnabled(
            not busy
            and (
                status in {"created", "analyzing", "paused", "applying", "rolling_back"}
                or (
                    status in {"ready", "failed", "complete_with_issues"}
                    and int(_value(summary, "failed", 0) or 0) > 0
                )
            )
        )
        self.cancel_button.setEnabled(
            (analysis_busy and self._job_id is not None)
            or (not busy and status in {"created", "analyzing", "paused"})
        )
        self.retry_button.setEnabled(not busy and int(_value(summary, "failed", 0) or 0) > 0)
        self.apply_button.setEnabled(
            not busy
            and status in {"ready", "complete_with_issues"}
            and int(_value(summary, "high_confidence", 0) or 0) > 0
        )
        self.rollback_button.setEnabled(
            not busy
            and status in {"ready", "complete", "complete_with_issues"}
            and int(_value(summary, "applied", 0) or 0) > 0
        )
        self.report_button.setEnabled(self._job_id is not None)
        self.clear_button.setEnabled(
            not busy
            and status in {"cancelled", "rolled_back", "complete"}
            and not (status == "complete" and int(_value(summary, "applied", 0) or 0))
        )

    def _refresh_estimate(self) -> None:
        if not self._job_id:
            self.disk_estimate.setText("Disk estimate: available after analysis")
            return
        try:
            estimate = self.service.estimate_apply(self._job_id)
        except Exception:
            self.disk_estimate.setText("Disk estimate: available when the job is ready")
            return
        self.disk_estimate.setText(
            "Disk estimate: "
            f"{_format_bytes(_value(estimate, 'required_with_headroom', 0))} required "
            f"• {_format_bytes(_value(estimate, 'backup_bytes', 0))} backups"
        )

    @staticmethod
    def _field_value(snapshot: object, field_name: str) -> str | None:
        if not isinstance(snapshot, Mapping):
            return None
        fields = snapshot.get("fields")
        if not isinstance(fields, Mapping):
            return None
        state = fields.get(field_name)
        if isinstance(state, Mapping):
            value = state.get("value")
            return str(value) if value not in (None, "") else None
        return None

    @staticmethod
    def _identity_text(snapshot: object, *, candidate: bool = False) -> str:
        if not isinstance(snapshot, Mapping):
            return "—"
        if candidate:
            title = snapshot.get("title")
            artist = snapshot.get("artist")
        else:
            title = MetadataRemediationDialog._field_value(snapshot, "title")
            artist = MetadataRemediationDialog._field_value(snapshot, "artist")
        parts = [str(value) for value in (title, artist) if value not in (None, "")]
        return " — ".join(parts) or "—"

    def refresh_items(self, _index: int | None = None) -> None:
        self.items_table.setRowCount(0)
        if not self._job_id:
            self._selection_changed()
            return
        try:
            items = list(self.service.list_items(self._job_id, limit=5000))
        except Exception as exc:
            self._show_error(exc)
            return
        active_filter = str(self.filter_combo.currentData() or "all")
        filtered = [item for item in items if self._matches_filter(item, active_filter)]
        self.items_table.setRowCount(len(filtered))
        for row, item in enumerate(filtered):
            confidence_class = str(item.get("confidence_class") or item.get("status") or "pending")
            score = item.get("confidence_score")
            reasons = item.get("match_reasons")
            if isinstance(reasons, Iterable) and not isinstance(reasons, (str, bytes, Mapping)):
                reason_text = ", ".join(str(value).replace("_", " ") for value in reasons)
            else:
                reason_text = str(reasons or "")
            values = (
                confidence_class.replace("_", " "),
                "—" if score is None else f"{float(score):.1f}",
                self._identity_text(item.get("current_snapshot")),
                self._identity_text(item.get("candidate_snapshot"), candidate=True),
                reason_text or "—",
                str(item.get("status") or "pending").replace("_", " "),
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignCenter
                    if column in {0, 1, 5}
                    else Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                )
                if confidence_class == "high_confidence" and column == 0:
                    cell.setForeground(QColor(COLORS["accent"]))
                elif confidence_class in {"failed", "ambiguous"} and column == 0:
                    cell.setForeground(QColor(COLORS["danger"] if confidence_class == "failed" else COLORS["warning"]))
                self.items_table.setItem(row, column, cell)
            self.items_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, dict(item))
        self._selection_changed()

    @staticmethod
    def _matches_filter(item: Mapping[str, object], selected: str) -> bool:
        if selected == "all":
            return True
        status = str(item.get("status") or "")
        confidence = str(item.get("confidence_class") or "")
        if selected == "applied":
            return status == "applied"
        if selected == "failed":
            return status in {"failed", "apply_failed", "conflict"} or confidence == "failed"
        return confidence == selected or status == selected

    def _selected_items(self) -> list[dict[str, object]]:
        selection = self.items_table.selectionModel()
        if selection is None:
            return []
        result: list[dict[str, object]] = []
        for index in selection.selectedRows(0):
            item = self.items_table.item(index.row(), 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(value, Mapping):
                result.append(dict(value))
        return result

    @staticmethod
    def _short_value(value: object, *, limit: int = 72) -> str:
        text = " ".join(str(value or "").split()) or "—"
        return text if len(text) <= limit else f"{text[: limit - 1]}…"

    @staticmethod
    def _candidate_value(candidate: object, field_name: str) -> object:
        return candidate.get(field_name) if isinstance(candidate, Mapping) else None

    def _release_choice_labels(self, candidate: object) -> list[str]:
        if not isinstance(candidate, Mapping):
            return []
        alternatives = candidate.get("alternatives")
        raw_choices = (
            alternatives
            if isinstance(alternatives, list)
            else ([candidate] if candidate.get("release_id") else [])
        )
        choices: list[Mapping[str, object]] = []
        seen: set[tuple[str, ...]] = set()
        for value in raw_choices:
            if not isinstance(value, Mapping):
                continue
            identity = (
                str(value.get("release_id") or ""),
                str(value.get("recording_id") or ""),
                str(value.get("album") or ""),
                str(value.get("release_date") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            choices.append(value)
        if not choices:
            return []

        labels = []
        for index, choice in enumerate(choices[:10], start=1):
            album = self._short_value(
                choice.get("album") or "Unknown release", limit=48
            )
            artist = self._short_value(
                choice.get("album_artist")
                or choice.get("artist")
                or "Unknown artist",
                limit=42,
            )
            release_date = self._short_value(
                choice.get("release_date") or "date unknown", limit=16
            )
            release_status = self._short_value(
                choice.get("release_status") or "status unknown", limit=18
            )
            release_id = self._short_value(
                choice.get("release_id") or "no release ID", limit=40
            )
            recording_id = self._short_value(
                choice.get("recording_id") or "no recording ID", limit=40
            )
            labels.append(
                f"{index}. {album} | {artist} | {release_date} | {release_status} | "
                f"release {release_id} | recording {recording_id}"
            )
        return labels

    def _set_release_choices(self, candidate: object) -> int:
        labels = self._release_choice_labels(candidate)
        self.release_choices_combo.clear()
        if labels:
            self.release_choices_combo.addItems(labels)
            self.release_choices_combo.setEnabled(True)
            self.release_choices_combo.setToolTip("\n".join(labels))
        else:
            self.release_choices_combo.addItem("No provider release choices recorded")
            self.release_choices_combo.setEnabled(False)
            self.release_choices_combo.setToolTip("")
        return len(labels)

    def _bound_artwork_preview(self, item: Mapping[str, object]) -> str | None:
        item_id = int(item.get("id") or 0)
        candidate = item.get("candidate_snapshot")
        token = candidate_review_token(candidate)
        cached = self._artwork_previews.get(item_id)
        if cached is not None:
            cached_token, cached_path = cached
            if cached_token == token:
                return cached_path
            self._artwork_previews.pop(item_id, None)
        artwork_candidate = item.get("artwork_candidate")
        if isinstance(artwork_candidate, Mapping) and (
            str(artwork_candidate.get("candidate_token") or "") == token
        ):
            path = str(artwork_candidate.get("preview_path") or "")
            return path or None
        return None

    def _set_art_preview(self, label: QLabel, path_value: object, empty_text: str) -> None:
        label.setPixmap(QPixmap())
        path = Path(str(path_value or ""))
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                label.setText("")
                label.setPixmap(
                    pixmap.scaled(
                        label.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                return
        label.setText(empty_text)

    def _selection_changed(self) -> None:
        selected = self._selected_items()
        one_selected = len(selected) == 1
        reviewable = one_selected and str(selected[0].get("status")) in {
            "needs_review",
            "ambiguous",
            "approved",
        }
        idle = self._busy_kind is None
        self.review_button.setEnabled(one_selected and idle)
        self.skip_button.setEnabled(bool(selected) and self._busy_kind is None)
        candidate_present = one_selected and bool(selected[0].get("candidate_snapshot"))
        self.reject_button.setEnabled(candidate_present and idle)
        self.keep_button.setEnabled(bool(selected) and idle)
        self.edit_button.setEnabled(one_selected and idle)
        self.retry_query_button.setEnabled(one_selected and idle)
        self.approve_button.setEnabled(reviewable and idle)
        for checkbox in self.field_checks.values():
            checkbox.setEnabled(reviewable and idle)
            checkbox.setChecked(False)
        if not selected:
            self.review_detail.setText(
                "Select one review item to compare its current and candidate metadata."
            )
            self._set_release_choices(None)
            self._set_art_preview(self.current_art_preview, None, "No selection")
            self._set_art_preview(self.candidate_art_preview, None, "No selection")
            return
        item = selected[0]
        current = item.get("current_snapshot")
        candidate = item.get("candidate_snapshot")
        comparisons = []
        for name, label in _FIELDS:
            if name == "artwork":
                current_value = "present" if self._field_value(current, name) else "none"
                candidate_value = (
                    "available"
                    if bool(self._candidate_value(candidate, "artwork_available"))
                    else "none"
                )
            else:
                current_value = self._field_value(current, name)
                candidate_value = self._candidate_value(candidate, name)
            comparisons.append(
                f"{label}: {self._short_value(current_value)} -> "
                f"{self._short_value(candidate_value)}"
            )
        current_duration = _value(current, "duration_seconds", None)
        candidate_duration = self._candidate_value(candidate, "duration_seconds")
        duration_text = "unknown"
        if current_duration is not None and candidate_duration is not None:
            delta = abs(float(current_duration) - float(candidate_duration))
            duration_text = (
                f"{float(current_duration):.1f}s -> {float(candidate_duration):.1f}s "
                f"(delta {delta:.1f}s)"
            )
        reasons = item.get("match_reasons")
        reason_text = (
            ", ".join(str(value).replace("_", " ") for value in reasons)
            if isinstance(reasons, Iterable)
            and not isinstance(reasons, (str, bytes, Mapping))
            else str(reasons or "none")
        )
        source_kind = _value(current, "source_kind", None) or "local/unknown"
        source_upload_observed = bool(_value(current, "source_upload_date", None))
        release_choice_count = self._set_release_choices(candidate)
        detail_lines = [
            " | ".join(comparisons[:2]),
            " | ".join(comparisons[2:4]),
            " | ".join(comparisons[4:]),
            f"Duration: {duration_text}",
            f"Source observations: {source_kind}; upload date retained: "
            f"{'yes' if source_upload_observed else 'no'}",
            f"Reasons: {self._short_value(reason_text, limit=180)}",
            f"Release choices: {release_choice_count} shown in the review-only list below.",
        ]
        self.review_detail.setText("\n".join(detail_lines))
        current_art = self._field_value(current, "artwork")
        preview_path = self._bound_artwork_preview(item)
        self._set_art_preview(self.current_art_preview, current_art, "No current art")
        candidate_empty = (
            "Select Review to load"
            if bool(self._candidate_value(candidate, "artwork_available"))
            else "No candidate art"
        )
        self._set_art_preview(self.candidate_art_preview, preview_path, candidate_empty)

    def _submit(
        self,
        kind: str,
        operation: Callable[[object, Callable[[object], None]], object],
        *,
        changed_track_ids: Iterable[int] = (),
    ) -> None:
        if self.task_runner.pending_count:
            return
        self._busy_kind = kind
        self._pending_changed_track_ids = tuple(sorted({int(value) for value in changed_track_ids}))
        self.job_status.setText(f"{kind.replace('_', ' ').title()} in progress…")
        self.task_runner.submit(kind, operation)
        try:
            summary = self.service.status(self._job_id)
        except Exception:
            summary = None
        self._apply_summary(summary)
        self._selection_changed()

    def _task_progress(self, summary: object) -> None:
        job_id = str(_value(summary, "id", ""))
        if job_id:
            self._job_id = job_id
        self._apply_summary(summary)

    def _task_completed(self, result: RemediationTaskResult) -> None:
        kind = result.kind
        pending_tracks = self._pending_changed_track_ids
        self._busy_kind = None
        self._pending_changed_track_ids = ()
        if result.error:
            self.refresh_dashboard()
            self.job_status.setText(
                f"Operation could not complete: {result.error.replace('_', ' ')}"
            )
            return
        if kind == "artwork_preview":
            value = result.value
            if isinstance(value, Mapping):
                item_id = int(value.get("item_id") or 0)
                artwork_path = str(value.get("artwork_path") or "")
                candidate_token = str(value.get("candidate_token") or "")
                if item_id and artwork_path and candidate_token:
                    self._artwork_previews[item_id] = (
                        candidate_token,
                        artwork_path,
                    )
            self._selection_changed()
            self.job_status.setText("Candidate artwork loaded for private review.")
            return
        summary = _result_summary(result.value)
        job_id = str(_value(summary, "id", ""))
        if job_id:
            self._job_id = job_id
        self.refresh_dashboard()
        if kind in {"apply", "rollback"} and self._job_id:
            try:
                desired = "applied" if kind == "apply" else "rolled_back"
                pending_tracks = tuple(
                    sorted(
                        {
                            int(item["track_id"])
                            for item in self.service.list_items(self._job_id, limit=5000)
                            if str(item.get("status")) == desired
                        }
                    )
                )
            except Exception:
                pending_tracks = ()
        if pending_tracks:
            self.tracks_changed.emit(pending_tracks)
        # refresh_dashboard() already renders the persisted lifecycle state;
        # do not obscure paused/cancelled/issue outcomes with a generic success.

    def analyze_library(self) -> None:
        self._submit("analysis", lambda service, progress: service.analyze(None, progress=progress))

    def pause_analysis(self) -> None:
        if not self._job_id:
            return
        try:
            self.service.pause(self._job_id)
            self.refresh_dashboard()
        except Exception as exc:
            self._show_error(exc)

    def resume_analysis(self) -> None:
        if not self._job_id:
            return
        job_id = self._job_id
        self._submit("resume", lambda service, progress: service.resume(job_id, progress=progress))

    def cancel_analysis(self) -> None:
        if not self._job_id:
            return
        answer = QMessageBox.question(
            self,
            "Cancel metadata analysis?",
            "Completed analysis remains available in the private resumable job. Cancel pending work?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.cancel(self._job_id)
            self.refresh_dashboard()
        except Exception as exc:
            self._show_error(exc)

    def retry_failed(self) -> None:
        if not self._job_id:
            return
        job_id = self._job_id
        self._submit(
            "retry_failed",
            lambda service, progress: service.retry_failed(job_id, progress=progress),
        )

    def review_selected(self) -> None:
        selected = self._selected_items()
        if len(selected) != 1:
            QMessageBox.information(self, "Select one item", "Select exactly one item to review.")
            return
        self._selection_changed()
        self.review_detail.setFocus(Qt.FocusReason.OtherFocusReason)
        self.job_status.setText("Review current and candidate values, then approve only trusted fields.")
        item = selected[0]
        candidate = item.get("candidate_snapshot")
        item_id = int(item.get("id") or 0)
        if (
            self._job_id
            and item_id
            and bool(self._candidate_value(candidate, "artwork_available"))
            and not self._bound_artwork_preview(item)
        ):
            job_id = self._job_id
            self._submit(
                "artwork_preview",
                lambda service, _progress: service.prepare_review_artwork(
                    job_id, item_id
                ),
            )

    def edit_selected(self) -> None:
        selected = self._selected_items()
        if len(selected) != 1:
            QMessageBox.information(self, "Select one item", "Select exactly one item to edit.")
            return
        track_id = int(selected[0].get("track_id") or 0)
        if track_id:
            self.edit_track_requested.emit(track_id)
            self.job_status.setText(
                "Edit in the trusted metadata editor, then Analyze Library to search again."
            )

    def retry_search(self) -> None:
        if not self._job_id:
            return
        selected = self._selected_items()
        if len(selected) != 1:
            QMessageBox.information(
                self, "Select one item", "Select exactly one item to retry."
            )
            return
        item = selected[0]
        current = item.get("current_snapshot")
        title_default = self._field_value(current, "title") or ""
        artist_default = self._field_value(current, "artist") or ""
        title, accepted = QInputDialog.getText(
            self,
            "Retry metadata search",
            "Search title (used for this query only):",
            text=title_default,
        )
        if not accepted:
            return
        artist, accepted = QInputDialog.getText(
            self,
            "Retry metadata search",
            "Search artist (used for this query only):",
            text=artist_default,
        )
        if not accepted:
            return
        job_id = self._job_id
        item_id = int(item.get("id") or 0)
        self._artwork_previews.pop(item_id, None)
        self._submit(
            "retry_search",
            lambda service, _progress: service.retry_item_with_query(
                job_id, item_id, title, artist
            ),
        )

    def skip_selected(self) -> None:
        if not self._job_id:
            return
        selected = self._selected_items()
        item_ids = [int(item["id"]) for item in selected if item.get("id") is not None]
        if not item_ids:
            return
        try:
            self.service.skip_items(self._job_id, item_ids)
            for item_id in item_ids:
                self._artwork_previews.pop(item_id, None)
            self.refresh_dashboard()
        except Exception as exc:
            self._show_error(exc)

    def reject_selected(self) -> None:
        if not self._job_id:
            return
        selected = self._selected_items()
        item_ids = [int(item["id"]) for item in selected if item.get("id") is not None]
        if not item_ids:
            return
        try:
            self.service.reject_candidates(self._job_id, item_ids)
            for item_id in item_ids:
                self._artwork_previews.pop(item_id, None)
            self.refresh_dashboard()
        except Exception as exc:
            self._show_error(exc)

    def keep_current_selected(self) -> None:
        if not self._job_id:
            return
        selected = self._selected_items()
        item_ids = [int(item["id"]) for item in selected if item.get("id") is not None]
        if not item_ids:
            return
        try:
            self.service.keep_current_items(self._job_id, item_ids)
            for item_id in item_ids:
                self._artwork_previews.pop(item_id, None)
            self.refresh_dashboard()
        except Exception as exc:
            self._show_error(exc)

    def approve_selected(self) -> None:
        if not self._job_id:
            return
        selected = self._selected_items()
        if len(selected) != 1:
            QMessageBox.information(self, "Select one item", "Select exactly one review item.")
            return
        fields = [name for name, checkbox in self.field_checks.items() if checkbox.isChecked()]
        if not fields:
            QMessageBox.information(self, "Select fields", "Select at least one trusted field.")
            return
        write_files = self.write_files_checkbox.isChecked()
        item = selected[0]
        current = item.get("current_snapshot")
        candidate = item.get("candidate_snapshot")
        labels = dict(_FIELDS)
        field_lines = []
        for name in fields:
            current_value = self._field_value(current, name)
            if name == "artwork":
                current_value = "present" if current_value else "none"
            candidate_value = (
                "available"
                if name == "artwork"
                and bool(self._candidate_value(candidate, "artwork_available"))
                else self._candidate_value(candidate, name)
            )
            field_lines.append(
                f"{labels[name]}: {self._short_value(current_value)} -> "
                f"{self._short_value(candidate_value)}"
            )
        answer = QMessageBox.question(
            self,
            "Approve selected metadata?",
            "Apply the selected candidate fields?\n\n"
            f"Selected fields: {len(fields)}\n"
            + "\n".join(field_lines)
            + "\n"
            f"File-tag writeback: {'enabled with verified backup' if write_files else 'database only'}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        item_id = int(item["id"])
        track_id = int(item["track_id"])
        expected_candidate_token = candidate_review_token(candidate)
        job_id = self._job_id
        self._submit(
            "approve",
            lambda service, _progress: service.approve_review_item(
                job_id,
                item_id,
                fields,
                confirmed=True,
                write_files=write_files,
                expected_candidate_token=expected_candidate_token,
            ),
            changed_track_ids=(track_id,),
        )

    def apply_high_confidence(self) -> None:
        if not self._job_id:
            return
        try:
            estimate = self.service.estimate_apply(self._job_id)
        except Exception as exc:
            self._show_error(exc)
            return
        write_files = self.write_files_checkbox.isChecked()
        message = (
            "Apply only strict high-confidence results?\n\n"
            f"Database updates: {int(_value(estimate, 'database_updates', 0) or 0)}\n"
            f"Files to write: {int(_value(estimate, 'file_writes', 0) or 0) if write_files else 0}\n"
            f"Artwork replacements: {int(_value(estimate, 'artwork_replacements', 0) or 0)}\n"
            f"Backup bytes: {_format_bytes(_value(estimate, 'backup_bytes', 0) if write_files else 0)}\n"
            f"Temporary disk requirement: {_format_bytes(_value(estimate, 'required_with_headroom', 0) if write_files else 0)}\n"
            f"Left for review: {int(_value(estimate, 'review_items', 0) or 0)}\n"
            f"Unchanged: {int(_value(estimate, 'unchanged_items', 0) or 0)}\n\n"
            f"File-tag writeback is {'enabled' if write_files else 'disabled (database only)'}."
        )
        answer = QMessageBox.question(
            self,
            "Apply high-confidence metadata?",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        job_id = self._job_id
        self._submit(
            "apply",
            lambda service, progress: service.apply_high_confidence(
                job_id,
                confirmed=True,
                write_files=write_files,
                progress=progress,
            ),
        )

    def rollback_job(self) -> None:
        if not self._job_id:
            return
        applied = int(self.metric_cards["applied"].value_label.text() or 0)
        answer = QMessageBox.question(
            self,
            "Undo remediation job?",
            "Restore the exact verified media backups and previous Music Vault metadata?\n\n"
            f"Applied items to inspect: {applied}\n"
            "Files changed independently after remediation will be left as conflicts.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        job_id = self._job_id
        self._submit(
            "rollback",
            lambda service, progress: service.rollback(
                job_id, confirmed=True, progress=progress
            ),
        )

    def open_private_report(self) -> None:
        if not self._job_id:
            return
        path = metadata_reports_dir() / self._job_id
        try:
            path.mkdir(parents=True, exist_ok=True)
            self._open_folder(path)
        except Exception as exc:
            self._show_error(exc)

    def clear_completed_job(self) -> None:
        if not self._job_id:
            return
        answer = QMessageBox.question(
            self,
            "Clear completed job?",
            "Remove the completed job from the dashboard? Private backups are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.clear_completed_job(self._job_id)
            self._job_id = None
            self.refresh_dashboard()
        except Exception as exc:
            self._show_error(exc)

    def _show_error(self, exc: BaseException) -> None:
        self.job_status.setText(
            f"Operation unavailable: {_safe_error(exc).replace('_', ' ')}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API spelling
        self.task_runner.close()
        super().closeEvent(event)
