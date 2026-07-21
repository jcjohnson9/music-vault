from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from music_vault.metadata.artwork import (
    ArtworkError,
    CoverArtArchiveProvider,
    PreparedArtwork,
    prepare_local_artwork,
    store_prepared_artwork,
)
from music_vault.core.audio_quality import (
    BEST_ORIGINAL_PROFILE,
    MP3_320_COMPATIBILITY_PROFILE,
    compare_source_and_stored,
    normalize_codec,
)
from music_vault.core.media_quality_schema import get_track_media_quality
from music_vault.metadata.musicbrainz_enricher import (
    LOW_CONFIDENCE_SCORE,
    MetadataCandidate,
    MusicBrainzProvider,
)
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.schema import (
    VERSION_TYPES,
    normalize_release_date,
)
from music_vault.metadata.service import (
    EffectiveMetadataSnapshot,
    MetadataAction,
    MetadataChangeResult,
    MetadataFieldState,
    MetadataService,
)
from music_vault.ui.icons import ui_icon
from music_vault.ui.artist_credit_editor import ArtistCreditEditor
from music_vault.ui.metadata_tasks import MetadataTaskResult, MetadataTaskRunner


FIELD_LABELS = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "album_artist": "Album Artist",
    "release_date": "Release Date",
    "original_release_date": "Original Song Release Date",
    "version_type": "Version Type",
    "version_label": "Version Label",
    "artwork": "Artwork",
}


LEGACY_TEXT_FIELDS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "release_date",
)

QUALITY_PROFILE_LABELS = {
    BEST_ORIGINAL_PROFILE: "Best Original",
    MP3_320_COMPATIBILITY_PROFILE: "MP3 320 Compatibility",
    "legacy_youtube_mp3": "Legacy YouTube MP3",
    "local_import": "Local Original",
    "unknown": "Unknown",
}

QUALITY_CODEC_LABELS = {
    "aac": "AAC",
    "alac": "ALAC",
    "flac": "FLAC",
    "mp3": "MP3",
    "opus": "Opus",
    "vorbis": "Vorbis",
}


def _display_value(value: str | None) -> str:
    return value or "Not set"


def _quality_value(row: object, key: str) -> object | None:
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return getattr(row, key, None)


def _known_positive_int(value: object) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized > 0 else None


def _quality_codec_label(value: object) -> str | None:
    normalized = normalize_codec(value)
    return QUALITY_CODEC_LABELS.get(normalized) if normalized else None


def _quality_format_label(extension: object, container: object) -> str | None:
    suffix = str(extension or "").strip().casefold()
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    container_text = str(container or "").strip()
    if suffix:
        return suffix
    return container_text or None


def _format_quality_bitrate(value: object) -> str | None:
    bitrate = _known_positive_int(value)
    return f"Approximately {bitrate:,} kbps" if bitrate is not None else None


def _format_quality_sample_rate(value: object) -> str | None:
    sample_rate = _known_positive_int(value)
    return f"{sample_rate:,} Hz" if sample_rate is not None else None


def _format_quality_channels(value: object) -> str | None:
    channels = _known_positive_int(value)
    if channels == 1:
        return "1 (mono)"
    if channels == 2:
        return "2 (stereo)"
    return str(channels) if channels is not None else None


def _format_quality_file_size(value: object) -> str | None:
    size = _known_positive_int(value)
    if size is None:
        return None
    units = ("bytes", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for candidate in units[1:]:
        if amount < 1024:
            break
        amount /= 1024
        unit = candidate
    if unit == "bytes":
        return f"{size:,} bytes"
    return f"{amount:.1f} {unit} ({size:,} bytes)"


def _quality_transformation_text(row: object) -> str | None:
    profile = str(_quality_value(row, "acquisition_profile") or "").casefold()
    kind = str(_quality_value(row, "transformation_kind") or "").casefold()
    source_codec = _quality_value(row, "source_codec")
    stored_codec = _quality_value(row, "stored_codec")
    if profile in {BEST_ORIGINAL_PROFILE, MP3_320_COMPATIBILITY_PROFILE}:
        comparison = compare_source_and_stored(
            profile=profile,
            source_codec=source_codec,
            stored_codec=stored_codec,
            source_bitrate_kbps=_quality_value(row, "source_bitrate_kbps"),
            stored_bitrate_kbps=_quality_value(row, "stored_bitrate_kbps"),
            transformation_kind=kind or None,
        )
        return comparison.transformation_text
    if kind == "legacy_inferred_transcode":
        return "Legacy YouTube MP3; source quality was not recorded"
    if kind == "local_original":
        return "Local original; no automatic conversion recorded"
    if kind == "lossy_transcode":
        return "Lossy transcode recorded; not a fidelity upgrade"
    if kind == "source_preserved_remux":
        return "Container-only remux recorded; source codec comparison unavailable"
    if kind == "none":
        return "No lossy re-encoding recorded"
    return None


def _quality_display_rows(row: object | None) -> tuple[tuple[str, str, str], ...]:
    if row is None:
        return ()
    profile = str(_quality_value(row, "acquisition_profile") or "").casefold()
    sample_rate = _quality_value(row, "stored_sample_rate_hz") or _quality_value(
        row, "source_sample_rate_hz"
    )
    channels = _quality_value(row, "stored_channels") or _quality_value(
        row, "source_channels"
    )
    file_size = _quality_value(row, "stored_filesize_bytes") or _quality_value(
        row, "source_filesize_bytes"
    )
    candidates = (
        ("acquisition_profile", "Acquisition profile", QUALITY_PROFILE_LABELS.get(profile)),
        (
            "source_format",
            "Source format",
            _quality_format_label(
                _quality_value(row, "source_extension"),
                _quality_value(row, "source_container"),
            ),
        ),
        ("source_codec", "Source codec", _quality_codec_label(_quality_value(row, "source_codec"))),
        (
            "source_bitrate",
            "Source bitrate",
            _format_quality_bitrate(_quality_value(row, "source_bitrate_kbps")),
        ),
        (
            "stored_format",
            "Stored format",
            _quality_format_label(
                _quality_value(row, "stored_extension"),
                _quality_value(row, "stored_container"),
            ),
        ),
        ("stored_codec", "Stored codec", _quality_codec_label(_quality_value(row, "stored_codec"))),
        (
            "stored_bitrate",
            "Stored bitrate",
            _format_quality_bitrate(_quality_value(row, "stored_bitrate_kbps")),
        ),
        ("sample_rate", "Sample rate", _format_quality_sample_rate(sample_rate)),
        ("channels", "Channels", _format_quality_channels(channels)),
        ("file_size", "File size", _format_quality_file_size(file_size)),
        ("transformation", "Transformation", _quality_transformation_text(row)),
    )
    return tuple((key, label, value) for key, label, value in candidates if value)


class MetadataFieldEditor(QFrame):
    def __init__(self, state: MetadataFieldState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MetadataFieldCard")
        self.field_name = state.field_name
        self.initial_value = state.value
        self.pending_action: MetadataAction | None = None

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(6)

        label = QLabel(FIELD_LABELS[state.field_name])
        label.setObjectName("MetadataFieldLabel")
        self.value_edit = QLineEdit(state.value or "")
        self.value_edit.setObjectName(f"MetadataValue_{state.field_name}")
        self.value_edit.setAccessibleName(FIELD_LABELS[state.field_name])
        self.value_edit.textEdited.connect(self._manual_text_edited)
        if state.field_name in {"release_date", "original_release_date"}:
            self.value_edit.setPlaceholderText("YYYY, YYYY-MM, or YYYY-MM-DD")

        provenance = state.provenance.replace("_", " ").title()
        self.provenance_badge = QLabel(provenance)
        self.provenance_badge.setObjectName("MetadataBadge")
        self.provenance_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.lock_badge = QLabel("Locked" if state.is_locked else "Unlocked")
        self.lock_badge.setObjectName("MetadataLockBadgeLocked" if state.is_locked else "MetadataBadge")
        self.lock_badge.setTextFormat(Qt.TextFormat.PlainText)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.setEnabled(state.field_name != "title")
        self.clear_button.clicked.connect(self._clear)
        self.unlock_button = QPushButton("Unlock")
        self.unlock_button.setObjectName("GhostButton")
        self.unlock_button.setEnabled(state.is_locked)
        self.unlock_button.clicked.connect(self._unlock)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setObjectName("GhostButton")
        self.reset_button.clicked.connect(self._reset)

        layout.addWidget(label, 0, 0)
        layout.addWidget(self.provenance_badge, 0, 1)
        layout.addWidget(self.lock_badge, 0, 2)
        layout.addWidget(self.value_edit, 1, 0, 1, 3)
        button_row = QHBoxLayout()
        button_row.addWidget(self.clear_button)
        button_row.addWidget(self.unlock_button)
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        layout.addLayout(button_row, 2, 0, 1, 3)

    def load_state(self, state: MetadataFieldState) -> None:
        """Refresh the editor after an in-dialog apply or undo."""

        self.initial_value = state.value
        self.pending_action = None
        previous = self.value_edit.blockSignals(True)
        try:
            self.value_edit.setText(state.value or "")
        finally:
            self.value_edit.blockSignals(previous)
        self.provenance_badge.setText(state.provenance.replace("_", " ").title())
        self.lock_badge.setText("Locked" if state.is_locked else "Unlocked")
        self.lock_badge.setObjectName(
            "MetadataLockBadgeLocked" if state.is_locked else "MetadataBadge"
        )
        self.unlock_button.setEnabled(state.is_locked)
        self.lock_badge.style().unpolish(self.lock_badge)
        self.lock_badge.style().polish(self.lock_badge)

    def _clear(self) -> None:
        self.value_edit.clear()
        self.pending_action = MetadataAction.clear()
        self.lock_badge.setText("Manual clear • Locked")
        self.lock_badge.setObjectName("MetadataLockBadgeLocked")

    def _manual_text_edited(self, _text: str) -> None:
        self.pending_action = None
        self.lock_badge.setText("Manual edit • Locked")
        self.lock_badge.setObjectName("MetadataLockBadgeLocked")

    def _unlock(self) -> None:
        self.pending_action = MetadataAction.unlock()
        self.lock_badge.setText("Will unlock")
        self.lock_badge.setObjectName("MetadataBadge")

    def _reset(self) -> None:
        self.pending_action = MetadataAction.reset()
        self.lock_badge.setText("Reset to automatic")
        self.lock_badge.setObjectName("MetadataBadge")

    def action_for_save(self) -> MetadataAction | None:
        if self.pending_action is not None:
            return self.pending_action
        raw_value = self.value_edit.text()
        if raw_value == (self.initial_value or ""):
            return None
        value = raw_value.strip() or None
        if value is None:
            return MetadataAction.clear()
        return MetadataAction.set(value)


class VersionTypeFieldEditor(QFrame):
    """Constrained metadata editor for the normalized version taxonomy."""

    field_name = "version_type"

    def __init__(self, state: MetadataFieldState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MetadataFieldCard")
        self.initial_value = state.value
        self.pending_action: MetadataAction | None = None

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(6)
        label = QLabel(FIELD_LABELS[self.field_name])
        label.setObjectName("MetadataFieldLabel")
        self.value_combo = QComboBox()
        self.value_combo.setObjectName("MetadataValue_version_type")
        self.value_combo.setAccessibleName(FIELD_LABELS[self.field_name])
        self.value_combo.addItem("Not set", "")
        for value in VERSION_TYPES:
            self.value_combo.addItem(value.replace("_", " ").title(), value)
        self.value_combo.currentIndexChanged.connect(self._manual_value_changed)

        self.provenance_badge = QLabel("")
        self.provenance_badge.setObjectName("MetadataBadge")
        self.lock_badge = QLabel("")
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.clicked.connect(self._clear)
        self.unlock_button = QPushButton("Unlock")
        self.unlock_button.setObjectName("GhostButton")
        self.unlock_button.clicked.connect(self._unlock)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setObjectName("GhostButton")
        self.reset_button.clicked.connect(self._reset)

        layout.addWidget(label, 0, 0)
        layout.addWidget(self.provenance_badge, 0, 1)
        layout.addWidget(self.lock_badge, 0, 2)
        layout.addWidget(self.value_combo, 1, 0, 1, 3)
        buttons = QHBoxLayout()
        for button in (self.clear_button, self.unlock_button, self.reset_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons, 2, 0, 1, 3)
        self.load_state(state)

    def _select_value(self, value: str | None) -> None:
        index = self.value_combo.findData(value or "")
        self.value_combo.setCurrentIndex(index if index >= 0 else 0)

    def load_state(self, state: MetadataFieldState) -> None:
        self.initial_value = state.value
        self.pending_action = None
        previous = self.value_combo.blockSignals(True)
        try:
            self._select_value(state.value)
        finally:
            self.value_combo.blockSignals(previous)
        self.provenance_badge.setText(state.provenance.replace("_", " ").title())
        self.lock_badge.setText("Locked" if state.is_locked else "Unlocked")
        self.lock_badge.setObjectName(
            "MetadataLockBadgeLocked" if state.is_locked else "MetadataBadge"
        )
        self.unlock_button.setEnabled(state.is_locked)
        self.lock_badge.style().unpolish(self.lock_badge)
        self.lock_badge.style().polish(self.lock_badge)

    def _manual_value_changed(self, _index: int) -> None:
        self.pending_action = None
        self.lock_badge.setText("Manual edit • Locked")
        self.lock_badge.setObjectName("MetadataLockBadgeLocked")

    def _clear(self) -> None:
        previous = self.value_combo.blockSignals(True)
        try:
            self._select_value(None)
        finally:
            self.value_combo.blockSignals(previous)
        self.pending_action = MetadataAction.clear()
        self.lock_badge.setText("Manual clear • Locked")
        self.lock_badge.setObjectName("MetadataLockBadgeLocked")

    def _unlock(self) -> None:
        self.pending_action = MetadataAction.unlock()
        self.lock_badge.setText("Will unlock")
        self.lock_badge.setObjectName("MetadataBadge")

    def _reset(self) -> None:
        self.pending_action = MetadataAction.reset()
        self.lock_badge.setText("Reset to automatic")
        self.lock_badge.setObjectName("MetadataBadge")

    def action_for_save(self) -> MetadataAction | None:
        if self.pending_action is not None:
            return self.pending_action
        value = str(self.value_combo.currentData() or "")
        if value == (self.initial_value or ""):
            return None
        return MetadataAction.set(value) if value else MetadataAction.clear()


class ArtworkFieldEditor(QFrame):
    def __init__(self, state: MetadataFieldState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MetadataFieldCard")
        self.state = state
        self.pending_action: MetadataAction | None = None
        self.prepared_artwork: PreparedArtwork | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        self.preview = QLabel()
        self.preview.setObjectName("MetadataArtworkPreview")
        self.preview.setFixedSize(112, 112)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_path_preview(state.value)

        details = QVBoxLayout()
        title = QLabel("Artwork")
        title.setObjectName("MetadataFieldLabel")
        self.status = QLabel(
            f"{state.provenance.replace('_', ' ').title()} • "
            f"{'Locked' if state.is_locked else 'Unlocked'}"
        )
        self.status.setObjectName("MutedLabel")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        choose = QPushButton("Choose Local Image")
        choose.setIcon(ui_icon("folder", 16))
        choose.clicked.connect(self.choose_local)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.clicked.connect(self.clear_artwork)
        self.reset_button = QPushButton("Reset to Automatic")
        self.reset_button.setObjectName("GhostButton")
        self.reset_button.clicked.connect(self.reset_artwork)
        self.unlock_button = QPushButton("Unlock")
        self.unlock_button.setObjectName("GhostButton")
        self.unlock_button.setEnabled(state.is_locked)
        self.unlock_button.clicked.connect(self.unlock_artwork)
        details.addWidget(title)
        details.addWidget(self.status)
        details.addWidget(choose)
        buttons = QHBoxLayout()
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.unlock_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        details.addLayout(buttons)
        details.addStretch(1)
        layout.addWidget(self.preview)
        layout.addLayout(details, 1)

    def load_state(self, state: MetadataFieldState) -> None:
        """Refresh artwork and its field state without exposing its path."""

        self.state = state
        self.pending_action = None
        self.prepared_artwork = None
        self.status.setText(
            f"{state.provenance.replace('_', ' ').title()} • "
            f"{'Locked' if state.is_locked else 'Unlocked'}"
        )
        self.unlock_button.setEnabled(state.is_locked)
        self._set_path_preview(state.value)

    def _set_path_preview(self, path: str | None) -> None:
        pixmap = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        if pixmap.isNull():
            self.preview.setPixmap(ui_icon("metadata", 36).pixmap(36, 36))
        else:
            self.preview.setPixmap(
                pixmap.scaled(
                    108,
                    108,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def _set_prepared_preview(self, prepared: PreparedArtwork) -> None:
        pixmap = QPixmap()
        pixmap.loadFromData(QByteArray(prepared.data))
        self.preview.setPixmap(
            pixmap.scaled(
                108,
                108,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def choose_local(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose artwork",
            "",
            "Images (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        try:
            prepared = prepare_local_artwork(path)
        except ArtworkError:
            QMessageBox.warning(self, "Artwork rejected", "Choose a valid PNG, JPEG, or WebP image within the safe size limits.")
            return
        self.prepared_artwork = prepared
        self.pending_action = MetadataAction("prepared_artwork")
        self.status.setText("Manual image ready • saved only when you confirm")
        self._set_prepared_preview(prepared)

    def clear_artwork(self) -> None:
        self.prepared_artwork = None
        self.pending_action = MetadataAction.clear()
        self.status.setText("Manual clear • Locked")
        self.preview.setPixmap(ui_icon("metadata", 36).pixmap(36, 36))

    def unlock_artwork(self) -> None:
        self.pending_action = MetadataAction.unlock()
        self.status.setText("Current artwork will remain, unlocked")

    def reset_artwork(self) -> None:
        self.prepared_artwork = None
        self.pending_action = MetadataAction.reset()
        self.status.setText("Will reset to best automatic artwork")


@dataclass
class _PendingCandidateApply:
    candidate: MetadataCandidate
    values: dict[str, str]
    include_artwork: bool


class MetadataEditorDialog(QDialog):
    metadata_changed = Signal(object)

    def __init__(
        self,
        service: MetadataService,
        track_id: int,
        parent: QWidget | None = None,
        *,
        musicbrainz_provider: MusicBrainzProvider | None = None,
        cover_provider: CoverArtArchiveProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MetadataEditorDialog")
        self.setWindowTitle("Edit Metadata • Music Vault")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(940, 720)
        self.setMinimumSize(760, 620)
        self.service = service
        self.track_id = int(track_id)
        self.snapshot = service.snapshot(track_id)
        self.artist_credit_service = ArtistCreditService(service.conn)
        self.musicbrainz_provider = musicbrainz_provider or MusicBrainzProvider()
        self.cover_provider = cover_provider or CoverArtArchiveProvider()
        self.task_runner = MetadataTaskRunner(self)
        self.task_runner.completed.connect(self._task_completed)
        self._active_search_id: int | None = None
        self._active_artwork_id: int | None = None
        self._pending_candidate: _PendingCandidateApply | None = None
        self.candidates: list[MetadataCandidate] = []
        self._closed = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        title = QLabel("Trusted Metadata")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Correct what Music Vault displays, inspect its source, and protect approved values."
        )
        subtitle.setObjectName("MutedLabel")
        root.addWidget(title)
        root.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("MetadataTabs")
        self.edit_tab = self._build_edit_tab()
        self.sources_tab = self._build_sources_tab()
        self.quality_tab = self._build_quality_tab()
        self.musicbrainz_tab = self._build_musicbrainz_tab()
        self.history_tab = self._build_history_tab()
        self.tabs.addTab(self.edit_tab, "Edit")
        self.tabs.addTab(self.sources_tab, "Sources")
        self.tabs.addTab(self.quality_tab, "Quality")
        self.tabs.addTab(self.musicbrainz_tab, "MusicBrainz")
        self.tabs.addTab(self.history_tab, "History")
        root.addWidget(self.tabs, 1)

        self.validation_label = QLabel("")
        self.validation_label.setObjectName("ErrorLabel")
        self.validation_label.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_label.setWordWrap(True)
        root.addWidget(self.validation_label)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("GhostButton")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("Save to Music Vault")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self.save_manual_changes)
        footer.addWidget(cancel)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

    def _build_edit_tab(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        scroll = QScrollArea()
        self.edit_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(10)
        self.field_editors: dict[str, MetadataFieldEditor] = {}
        for field_name in LEGACY_TEXT_FIELDS:
            editor = MetadataFieldEditor(self.snapshot.fields[field_name])
            self.field_editors[field_name] = editor
            layout.addWidget(editor)

        version_group = QGroupBox("Release date and version identity")
        version_layout = QVBoxLayout(version_group)
        version_note = QLabel(
            "The version-specific release date stays separate from the original song date."
        )
        version_note.setObjectName("MutedLabel")
        version_note.setWordWrap(True)
        version_layout.addWidget(version_note)
        self.original_release_date_editor = MetadataFieldEditor(
            self.snapshot.fields["original_release_date"]
        )
        self.version_type_editor = VersionTypeFieldEditor(
            self.snapshot.fields["version_type"]
        )
        self.version_label_editor = MetadataFieldEditor(
            self.snapshot.fields["version_label"]
        )
        self.intelligence_field_editors = {
            "original_release_date": self.original_release_date_editor,
            "version_type": self.version_type_editor,
            "version_label": self.version_label_editor,
        }
        version_layout.addWidget(self.original_release_date_editor)
        version_layout.addWidget(self.version_type_editor)
        version_layout.addWidget(self.version_label_editor)
        layout.addWidget(version_group)

        self.artist_credit_editor = ArtistCreditEditor(
            self.artist_credit_service,
            self.track_id,
        )
        self.artist_credit_editor.credits_changed.connect(
            self._sync_artist_display_from_credits
        )
        layout.addWidget(self.artist_credit_editor)
        self.artwork_editor = ArtworkFieldEditor(self.snapshot.fields["artwork"])
        layout.addWidget(self.artwork_editor)
        self.file_writeback_note = QLabel(
            "Changes are saved to your Music Vault library. Writing approved changes back "
            "into audio files will be handled by the audited Batch 7 remediation workflow."
        )
        self.file_writeback_note.setObjectName("MetadataInfoBanner")
        self.file_writeback_note.setWordWrap(True)
        layout.addWidget(self.file_writeback_note)
        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        return container

    def _build_sources_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        context = QGroupBox("Source, release, and provider context • read only")
        context_layout = QGridLayout(context)
        self.source_context_labels: dict[str, QLabel] = {}
        provider_context = self._provider_context()
        source_rows = (
            ("source_kind", "Source kind", self.snapshot.source_kind or "Local / unknown"),
            (
                "source_upload_date",
                "Source upload date",
                self.snapshot.source_upload_date or "Not available",
            ),
            ("source_video_id", "Source video ID", self.snapshot.source_video_id or "Not available"),
            (
                "musicbrainz_recording_id",
                "MusicBrainz recording",
                self.snapshot.musicbrainz_recording_id or "Not confirmed",
            ),
            (
                "musicbrainz_release_id",
                "MusicBrainz release",
                self.snapshot.musicbrainz_release_id or "Not confirmed",
            ),
            (
                "discogs_release_id",
                "Discogs release",
                provider_context["discogs_release_id"],
            ),
            (
                "discogs_master_id",
                "Discogs master",
                provider_context["discogs_master_id"],
            ),
            (
                "discogs_track_position",
                "Discogs track position",
                provider_context["discogs_track_position"],
            ),
            ("label_context", "Label context", provider_context["label_context"]),
            ("release_context", "Release context", provider_context["release_context"]),
            (
                "provider_agreement",
                "Provider agreement",
                provider_context["provider_agreement"],
            ),
        )
        for row, (key, label, value) in enumerate(source_rows):
            key_label = QLabel(label)
            key_label.setObjectName("MutedLabel")
            value_label = QLabel(value)
            value_label.setTextFormat(Qt.TextFormat.PlainText)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            context_layout.addWidget(key_label, row, 0)
            context_layout.addWidget(value_label, row, 1)
            self.source_context_labels[key] = value_label
        self.source_upload_date_label = self.source_context_labels["source_upload_date"]
        self.discogs_attribution_label = QLabel()
        self.discogs_attribution_label.setObjectName("DiscogsAttributionLink")
        self.discogs_attribution_label.setTextFormat(Qt.TextFormat.RichText)
        self.discogs_attribution_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.discogs_attribution_label.setOpenExternalLinks(True)
        self.discogs_attribution_label.setAccessibleName("Data provided by Discogs")
        self._refresh_discogs_attribution(provider_context)
        context_layout.addWidget(
            self.discogs_attribution_label,
            len(source_rows),
            0,
            1,
            2,
        )
        upload_note = QLabel(
            "A source upload date describes the source publication. It is never treated as the canonical music release date."
        )
        upload_note.setObjectName("MetadataInfoBanner")
        upload_note.setWordWrap(True)

        self.observations_table = QTableWidget(0, 4)
        self.observations_table.setHorizontalHeaderLabels(
            ["Field", "Observed value", "Source", "Confidence"]
        )
        self.observations_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.observations_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.observations_table.horizontalHeader().setStretchLastSection(True)
        self._refresh_sources_tab()
        layout.addWidget(context)
        layout.addWidget(upload_note)
        layout.addWidget(self.observations_table, 1)
        return tab

    def _provider_context(self) -> dict[str, str]:
        not_available = "Not available"
        values = {
            "discogs_release_id": not_available,
            "discogs_master_id": not_available,
            "discogs_track_position": not_available,
            "label_context": not_available,
            "release_context": not_available,
            "provider_agreement": "Not analyzed",
        }
        conn = self.service.conn
        track = conn.execute(
            "SELECT discogs_release_id,discogs_master_id,discogs_track_position "
            "FROM tracks WHERE id=?",
            (self.track_id,),
        ).fetchone()
        if track is not None:
            values["discogs_release_id"] = str(track[0] or not_available)
            values["discogs_master_id"] = str(track[1] or not_available)
            values["discogs_track_position"] = str(track[2] or not_available)

        release_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_release_context'"
        ).fetchone()
        if release_table is not None:
            release = conn.execute(
                "SELECT * FROM track_release_context WHERE track_id=?",
                (self.track_id,),
            ).fetchone()
            if release is not None:
                keys = set(release.keys())
                release_id = release["discogs_release_id"] if "discogs_release_id" in keys else None
                master_id = release["discogs_master_id"] if "discogs_master_id" in keys else None
                if release_id:
                    values["discogs_release_id"] = str(release_id)
                if master_id:
                    values["discogs_master_id"] = str(master_id)
                label_parts = [
                    str(release[key]).strip()
                    for key in ("label_name", "catalog_number")
                    if key in keys and release[key] not in (None, "")
                ]
                if label_parts:
                    values["label_context"] = " • ".join(label_parts)
                release_parts = [
                    str(release[key]).strip()
                    for key in (
                        "release_title",
                        "release_format",
                        "release_country",
                        "release_date",
                        "original_release_date",
                    )
                    if key in keys and release[key] not in (None, "")
                ]
                if release_parts:
                    values["release_context"] = " • ".join(release_parts)

        item_table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='metadata_intelligence_items'"
        ).fetchone()
        if item_table is not None:
            item = conn.execute(
                "SELECT provider_agreement FROM metadata_intelligence_items "
                "WHERE track_id=? ORDER BY updated_at DESC,id DESC LIMIT 1",
                (self.track_id,),
            ).fetchone()
            if item is not None and item[0]:
                values["provider_agreement"] = str(item[0]).replace("_", " ").title()
        return values

    def _refresh_discogs_attribution(self, context: dict[str, str]) -> None:
        release_id = context.get("discogs_release_id", "")
        master_id = context.get("discogs_master_id", "")
        if release_id.isdigit():
            url = f"https://www.discogs.com/release/{release_id}"
        elif master_id.isdigit():
            url = f"https://www.discogs.com/master/{master_id}"
        else:
            self.discogs_attribution_label.clear()
            self.discogs_attribution_label.setHidden(True)
            return
        self.discogs_attribution_label.setText(
            f'<a href="{url}">Data provided by Discogs</a>'
        )
        self.discogs_attribution_label.setHidden(False)

    def _refresh_sources_tab(self) -> None:
        provider_context = self._provider_context()
        values = {
            "source_kind": self.snapshot.source_kind or "Local / unknown",
            "source_upload_date": self.snapshot.source_upload_date or "Not available",
            "source_video_id": self.snapshot.source_video_id or "Not available",
            "musicbrainz_recording_id": self.snapshot.musicbrainz_recording_id or "Not confirmed",
            "musicbrainz_release_id": self.snapshot.musicbrainz_release_id or "Not confirmed",
            **provider_context,
        }
        for key, label in getattr(self, "source_context_labels", {}).items():
            label.setText(values[key])
        self._refresh_discogs_attribution(provider_context)

        observations = self.service.observations(self.track_id)
        self.observations_table.setRowCount(len(observations))
        for row, observation in enumerate(observations):
            row_values = (
                FIELD_LABELS.get(
                    observation.field_name,
                    observation.field_name.replace("_", " ").title(),
                ),
                (
                    "Artwork available"
                    if observation.field_name == "artwork" and observation.value
                    else _display_value(observation.value)
                ),
                observation.provider.replace("_", " ").title(),
                "—" if observation.confidence is None else f"{observation.confidence:.0f}",
            )
            for column, value in enumerate(row_values):
                self.observations_table.setItem(row, column, QTableWidgetItem(value))

    def _build_quality_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        note = QLabel(
            "Audio quality details are read only. Music Vault shows only recorded or "
            "observed facts; opening this tab performs no network lookup and does not "
            "modify the media file or its embedded tags."
        )
        note.setObjectName("MetadataInfoBanner")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)

        try:
            quality_row = get_track_media_quality(self.service.conn, self.track_id)
        except Exception:
            quality_row = None
        rows = _quality_display_rows(quality_row)
        self.quality_context_labels: dict[str, QLabel] = {}

        if rows:
            context = QGroupBox("Recorded audio quality facts")
            context_layout = QGridLayout(context)
            for row_index, (key, label, value) in enumerate(rows):
                key_label = QLabel(label)
                key_label.setObjectName("MutedLabel")
                value_label = QLabel(value)
                value_label.setTextFormat(Qt.TextFormat.PlainText)
                value_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                value_label.setWordWrap(True)
                context_layout.addWidget(key_label, row_index, 0)
                context_layout.addWidget(value_label, row_index, 1)
                self.quality_context_labels[key] = value_label
            layout.addWidget(context)

        inspection_state = str(
            _quality_value(quality_row, "inspection_state") or "uninspected"
        ).casefold()
        state_text = {
            "legacy_inferred": (
                "Legacy classification is conservative. Source codec and bitrate remain "
                "unknown unless they were recorded during acquisition."
            ),
            "uninspected": (
                "Detailed local quality inspection has not been recorded. Only known "
                "inventory facts are shown."
            ),
            "failed": (
                "The last read-only quality inspection did not complete. Only previously "
                "recorded facts are shown."
            ),
        }.get(inspection_state)
        if not rows:
            state_text = (
                "Quality details have not been recorded for this track. Unknown values are "
                "left undisclosed rather than displayed as zero."
            )
        self.quality_state_label = QLabel(state_text or "Recorded quality inspection complete.")
        self.quality_state_label.setObjectName("MutedLabel")
        self.quality_state_label.setWordWrap(True)
        self.quality_state_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.quality_state_label)
        layout.addStretch(1)
        return tab

    def _build_musicbrainz_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        privacy = QLabel(
            "Search is optional. When you click Search MusicBrainz, the title and artist below "
            "are sent to MusicBrainz. No API key or browser cookie is used."
        )
        privacy.setObjectName("MetadataInfoBanner")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        query_row = QGridLayout()
        query_row.addWidget(QLabel("Title"), 0, 0)
        query_row.addWidget(QLabel("Artist"), 1, 0)
        self.search_title = QLineEdit(self.snapshot.value("title") or "")
        self.search_artist = QLineEdit(self.snapshot.value("artist") or "")
        query_row.addWidget(self.search_title, 0, 1)
        query_row.addWidget(self.search_artist, 1, 1)
        self.search_button = QPushButton("Search MusicBrainz")
        self.search_button.setIcon(ui_icon("metadata", 16))
        self.search_button.clicked.connect(self.start_musicbrainz_search)
        query_row.addWidget(self.search_button, 0, 2, 2, 1)
        layout.addLayout(query_row)
        self.search_status = QLabel("No search has been run.")
        self.search_status.setObjectName("MutedLabel")
        self.search_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.search_status)

        self.candidate_table = QTableWidget(0, 7)
        self.candidate_table.setHorizontalHeaderLabels(
            ["Score", "Title", "Artist", "Release", "Date", "Provider", "Artwork"]
        )
        self.candidate_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidate_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.candidate_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidate_table.horizontalHeader().setStretchLastSection(True)
        self.candidate_table.itemSelectionChanged.connect(self._update_candidate_preview)
        layout.addWidget(self.candidate_table, 1)

        self.candidate_preview = QLabel("Select a candidate to preview proposed changes.")
        self.candidate_preview.setObjectName("MetadataInfoBanner")
        self.candidate_preview.setTextFormat(Qt.TextFormat.PlainText)
        self.candidate_preview.setWordWrap(True)
        layout.addWidget(self.candidate_preview)

        fields = QHBoxLayout()
        self.candidate_field_checks: dict[str, QCheckBox] = {}
        for field_name in ("title", "artist", "album", "release_date", "artwork"):
            checkbox = QCheckBox(FIELD_LABELS[field_name])
            checkbox.setChecked(field_name != "artwork")
            checkbox.toggled.connect(lambda _checked=False: self._update_candidate_preview())
            self.candidate_field_checks[field_name] = checkbox
            fields.addWidget(checkbox)
        fields.addStretch(1)
        self.apply_candidate_button = QPushButton("Apply Selected Fields")
        self.apply_candidate_button.setObjectName("PrimaryButton")
        self.apply_candidate_button.setEnabled(False)
        self.apply_candidate_button.clicked.connect(self.apply_selected_candidate)
        fields.addWidget(self.apply_candidate_button)
        layout.addLayout(fields)
        return tab

    def _build_history_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        note = QLabel(
            "History covers Music Vault library metadata only. Undo does not rewrite audio-file tags or delete artwork files."
        )
        note.setObjectName("MetadataInfoBanner")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["When", "Actor", "Reason", "Fields"])
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history_table, 1)
        self.undo_button = QPushButton("Undo Last Metadata Change")
        self.undo_button.setIcon(ui_icon("refresh", 16))
        self.undo_button.clicked.connect(self.undo_last_change)
        layout.addWidget(self.undo_button, 0, Qt.AlignmentFlag.AlignRight)
        self.refresh_history()
        return tab

    def _all_field_editors(self) -> dict[str, object]:
        return {**self.field_editors, **self.intelligence_field_editors}

    def _sync_artist_display_from_credits(self) -> None:
        try:
            display = self.artist_credit_editor.display_artist(allow_incomplete=True)
        except (RuntimeError, ValueError):
            return
        if display:
            self.field_editors["artist"].value_edit.setText(display)

    def _validate_manual(self) -> dict[str, MetadataAction]:
        actions: dict[str, MetadataAction] = {}
        for field_name, editor in self._all_field_editors().items():
            action = editor.action_for_save()
            if action is not None:
                actions[field_name] = action
        title_action = actions.get("title")
        current_title = self.snapshot.value("title")
        resulting_title = (
            title_action.value
            if title_action is not None and title_action.action == "set"
            else None
            if title_action is not None and title_action.action == "clear"
            else current_title
        )
        if not str(resulting_title or "").strip():
            raise ValueError("Title cannot be empty.")
        for field_name in ("release_date", "original_release_date"):
            release_action = actions.get(field_name)
            if release_action is not None and release_action.action == "set":
                normalize_release_date(release_action.value)
        return actions

    def save_manual_changes(self) -> None:
        self.validation_label.clear()
        try:
            actions = self._validate_manual()
            credit_inputs: tuple[ArtistCreditInput, ...] | None = None
            credit_dirty = self.artist_credit_editor.is_dirty()
            artist_action = actions.get("artist")
            if credit_dirty:
                credit_inputs = self.artist_credit_editor.credit_inputs()
                actions["artist"] = MetadataAction.set(
                    self.artist_credit_editor.display_artist()
                )
            elif artist_action is not None and artist_action.action == "set":
                artist_name = str(artist_action.value or "").strip()
                if not artist_name:
                    raise ValueError("At least one primary artist credit is required.")
                credit_inputs = (ArtistCreditInput(artist_name, role="primary"),)
            elif artist_action is not None and artist_action.action == "clear":
                raise ValueError("At least one primary artist credit is required.")
            art_action = self.artwork_editor.pending_action
            if art_action is not None:
                if art_action.action == "prepared_artwork":
                    prepared = self.artwork_editor.prepared_artwork
                    if prepared is None:
                        raise ValueError("The selected artwork is unavailable.")
                    stored = store_prepared_artwork(prepared, provider="manual")
                    actions["artwork"] = MetadataAction.set(str(stored))
                else:
                    actions["artwork"] = art_action
            before = self.snapshot
            with self.service.conn:
                result = self.service.apply_actions(
                    self.track_id,
                    actions,
                    commit=False,
                )
                if credit_inputs is not None:
                    self.artist_credit_service.replace_track_credits(
                        self.track_id,
                        credit_inputs,
                        provenance="manual",
                        is_manual=True,
                        is_locked=True,
                        actor="user",
                        reason="manual_artist_credit_edit",
                        commit=False,
                    )
            after = self.service.snapshot(self.track_id)
            changed_fields = set(result.changed_fields)
            if credit_inputs is not None:
                changed_fields.add("artist")
            result = MetadataChangeResult(
                self.track_id,
                result.change_group_id,
                frozenset(changed_fields),
                before,
                after,
            )
        except (ValueError, ArtworkError) as exc:
            self.validation_label.setText(str(exc))
            return
        if result.changed:
            self.metadata_changed.emit(result)
        self.accept()

    def _refresh_editor_state(
        self,
        snapshot: EffectiveMetadataSnapshot | None = None,
    ) -> None:
        """Synchronize every editor surface after an in-dialog mutation."""

        self.snapshot = snapshot or self.service.snapshot(self.track_id)
        for field_name, editor in self.field_editors.items():
            editor.load_state(self.snapshot.fields[field_name])
        for field_name, editor in self.intelligence_field_editors.items():
            editor.load_state(self.snapshot.fields[field_name])
        self.artist_credit_editor.load_credits()
        self.artwork_editor.load_state(self.snapshot.fields["artwork"])
        self._refresh_sources_tab()
        self.refresh_history()
        self._update_candidate_preview()

    def start_musicbrainz_search(self) -> None:
        title = self.search_title.text().strip()
        artist = self.search_artist.text().strip() or None
        if not title:
            self.search_status.setText("Enter a title before searching.")
            return
        if self._active_search_id is not None:
            self.task_runner.cancel(self._active_search_id)
        self.candidates = []
        self.candidate_table.setRowCount(0)
        self._update_candidate_preview()
        self.search_status.setText("Searching MusicBrainz…")
        self.search_button.setEnabled(False)
        self.apply_candidate_button.setEnabled(False)
        self._active_search_id = self.task_runner.submit(
            "musicbrainz_search",
            lambda cancel: self.musicbrainz_provider.search(
                title,
                artist,
                cancel_event=cancel,
            ),
        )

    def _task_completed(self, result: MetadataTaskResult) -> None:
        if self._closed:
            return
        if result.kind == "musicbrainz_search":
            if result.request_id != self._active_search_id:
                return
            self._active_search_id = None
            self.search_button.setEnabled(True)
            if result.error:
                self.search_status.setText("MusicBrainz search is unavailable. Try again later.")
                return
            self.set_candidates(list(result.value or []))
        elif result.kind == "candidate_artwork":
            if result.request_id != self._active_artwork_id:
                return
            self._active_artwork_id = None
            pending = self._pending_candidate
            self._pending_candidate = None
            self.apply_candidate_button.setEnabled(bool(self.candidates))
            self.search_button.setEnabled(True)
            if pending is None:
                return
            artwork_path = None if result.error else result.value
            self._commit_candidate(
                pending,
                str(artwork_path) if artwork_path else None,
                artwork_unavailable=bool(result.error or not artwork_path),
            )

    def set_candidates(self, candidates: list[MetadataCandidate]) -> None:
        self.candidates = sorted(candidates, key=lambda item: (-item.score, item.provider_order))
        self.candidate_table.setRowCount(len(self.candidates))
        for row, candidate in enumerate(self.candidates):
            values = (
                str(candidate.score),
                candidate.title,
                candidate.artist,
                candidate.album or "—",
                candidate.release_date or "—",
                candidate.provider,
                "Available" if candidate.artwork_available else "Unknown / none",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                if column == 6:
                    item.setIcon(
                        ui_icon("albums" if candidate.artwork_available else "metadata", 16)
                    )
                self.candidate_table.setItem(row, column, item)
        self.candidate_table.clearSelection()
        self._update_candidate_preview()
        self.apply_candidate_button.setEnabled(bool(self.candidates))
        self.search_status.setText(
            f"{len(self.candidates)} candidate{'s' if len(self.candidates) != 1 else ''}. Select one to review."
            if self.candidates
            else "No matching candidates were found."
        )

    def _selected_candidate(self) -> MetadataCandidate | None:
        rows = self.candidate_table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        row = rows[0].row()
        return self.candidates[row] if 0 <= row < len(self.candidates) else None

    def _update_candidate_preview(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self.candidate_preview.setText("Select a candidate to preview proposed changes.")
            return
        confidence = "Low confidence • explicit confirmation required. " if candidate.low_confidence else ""
        candidate_values = {
            "title": candidate.title,
            "artist": candidate.artist,
            "album": candidate.album,
            "release_date": candidate.release_date,
        }
        changes = []
        for field_name, value in candidate_values.items():
            checkbox = self.candidate_field_checks.get(field_name)
            if checkbox is None or not checkbox.isChecked() or value in (None, ""):
                continue
            changes.append(
                f"{FIELD_LABELS[field_name]}: {_display_value(self.snapshot.value(field_name))} → {value}"
            )
        artwork_check = self.candidate_field_checks.get("artwork")
        if artwork_check is not None and artwork_check.isChecked() and candidate.release_id:
            changes.append("Artwork: current image retained until the selected candidate image validates")
        self.candidate_preview.setText(
            confidence + ("\n".join(changes) if changes else "No populated candidate fields are selected.")
        )

    def apply_selected_candidate(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self.search_status.setText("Select exactly one candidate first.")
            return
        selected = {
            name
            for name, checkbox in self.candidate_field_checks.items()
            if checkbox.isChecked()
        }
        values = {
            "title": candidate.title,
            "artist": candidate.artist,
            "album": candidate.album,
            "release_date": candidate.release_date,
        }
        patch = {
            name: str(value)
            for name, value in values.items()
            if name in selected and value not in (None, "")
        }
        include_artwork = "artwork" in selected and bool(candidate.release_id)
        if not patch and not include_artwork:
            self.search_status.setText("Select at least one populated field.")
            return
        warning = (
            "This is a low-confidence match. Review every selected field carefully.\n\n"
            if candidate.score < LOW_CONFIDENCE_SCORE
            else ""
        )
        fields = ", ".join(FIELD_LABELS[name] for name in patch)
        if include_artwork:
            fields = f"{fields}, Artwork" if fields else "Artwork"
        answer = QMessageBox.question(
            self,
            "Apply low-confidence candidate?" if candidate.low_confidence else "Apply MusicBrainz candidate?",
            f"{warning}Apply and lock these fields in Music Vault?\n{fields}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        pending = _PendingCandidateApply(candidate, patch, include_artwork)
        if include_artwork and candidate.release_id:
            self._pending_candidate = pending
            self.apply_candidate_button.setEnabled(False)
            self.search_button.setEnabled(False)
            self.search_status.setText("Retrieving selected candidate artwork…")
            self._active_artwork_id = self.task_runner.submit(
                "candidate_artwork",
                lambda _cancel: self.cover_provider.fetch_and_store(candidate.release_id),
            )
            return
        self._commit_candidate(pending, None)

    def _commit_candidate(
        self,
        pending: _PendingCandidateApply,
        artwork_path: str | None,
        *,
        artwork_unavailable: bool = False,
    ) -> None:
        candidate = pending.candidate
        result = self.service.apply_confirmed_candidate(
            self.track_id,
            pending.values,
            recording_id=candidate.recording_id,
            release_id=candidate.release_id,
            confidence=float(candidate.score),
            release_group_id=candidate.release_group_id,
            artwork_path=artwork_path,
        )
        if result.changed:
            self.metadata_changed.emit(result)
        self._refresh_editor_state(result.after)
        if pending.include_artwork and artwork_unavailable:
            self.search_status.setText(
                "Selected metadata was applied and locked. Candidate artwork was unavailable; "
                "existing artwork was retained."
                if result.changed
                else "Candidate artwork was unavailable; existing artwork was retained."
            )
        else:
            self.search_status.setText(
                "Selected candidate fields were applied and locked."
                if result.changed
                else "The selected candidate made no effective changes."
            )

    def refresh_history(self) -> None:
        groups = self.service.history_groups(self.track_id)
        self.history_table.setRowCount(len(groups))
        for row, group in enumerate(groups):
            fields = ", ".join(FIELD_LABELS[entry.field_name] for entry in group.entries)
            values = (group.changed_at, group.actor, group.reason.replace("_", " "), fields)
            for column, value in enumerate(values):
                self.history_table.setItem(row, column, QTableWidgetItem(value))
        self.undo_button.setEnabled(self.service.preview_undo(self.track_id) is not None)

    def undo_last_change(self) -> None:
        group = self.service.preview_undo(self.track_id)
        if group is None:
            return
        fields = ", ".join(FIELD_LABELS[entry.field_name] for entry in group.entries)
        answer = QMessageBox.question(
            self,
            "Undo last metadata change?",
            f"Restore the previous Music Vault values for: {fields}?\n\nAudio-file tags and artwork files are not changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self.service.undo_last_change(self.track_id)
        if result.changed:
            self.metadata_changed.emit(result)
            self._refresh_editor_state(result.after)

    def _close_pending_tasks(self) -> None:
        """Invalidate queued results before hiding or destroying the dialog."""

        self._closed = True
        self._active_search_id = None
        self._active_artwork_id = None
        self._pending_candidate = None
        self.task_runner.close()

    def reject(self) -> None:
        self._close_pending_tasks()
        super().reject()

    def accept(self) -> None:
        self._close_pending_tasks()
        super().accept()

    def closeEvent(self, event) -> None:
        self._close_pending_tasks()
        super().closeEvent(event)
