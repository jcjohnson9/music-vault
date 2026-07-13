from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from music_vault.version import APP_VERSION, DISPLAY_VERSION
from music_vault.ui.theme import COLORS, application_stylesheet


SUPPORTED_AUDIO_QUALITIES = ("192", "256", "320")


@dataclass(frozen=True)
class RuntimeEvidence:
    established: bool
    config_exists: bool = False
    library_rows: int = 0
    api_key_exists: bool = False
    status_exists: bool = False


@dataclass(frozen=True)
class OnboardingResult:
    data_folder: Path
    download_folder: Path
    local_import_folder: Path | None
    api_key: str
    authorized_use_acknowledged: bool
    audio_quality: str
    ffmpeg_location: str | None
    create_shortcut: bool
    skipped: bool = False


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _configured_json(path: Path) -> bool:
    if not _nonempty_file(path):
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    return isinstance(value, dict) and bool(value)


def _library_row_count(database: Path) -> int:
    """Read aggregate evidence without opening the database for writes."""
    if not _nonempty_file(database):
        return 0
    try:
        uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            total = 0
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in ("tracks", "playlists"):
                if table in tables:
                    total += int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return total
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # An existing database that cannot be classified must never be treated
        # as a disposable blank runtime.
        return 1


def inspect_runtime_evidence(
    *,
    config_file: Path,
    database_file: Path,
    api_key_file: Path,
    status_file: Path,
) -> RuntimeEvidence:
    config_exists = _configured_json(config_file)
    library_rows = _library_row_count(database_file)
    api_key_exists = _nonempty_file(api_key_file)
    status_exists = _nonempty_file(status_file)
    return RuntimeEvidence(
        established=bool(config_exists or library_rows or api_key_exists or status_exists),
        config_exists=config_exists,
        library_rows=library_rows,
        api_key_exists=api_key_exists,
        status_exists=status_exists,
    )


def should_show_first_run(
    config: Mapping[str, object] | None,
    evidence: RuntimeEvidence,
) -> bool:
    if config and config.get("onboarding_completed") is True:
        return False
    return not evidence.established


def validate_writable_folder(folder: Path) -> tuple[bool, str | None]:
    """Create and remove a harmless probe file to verify actual writability."""
    try:
        folder = folder.expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".music-vault-write-test-{uuid.uuid4().hex}.tmp"
        probe.write_bytes(b"")
        probe.unlink()
        return True, None
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"Music Vault cannot write to the selected folder ({exc.__class__.__name__})."


def sanitized_onboarding_config(
    existing: Mapping[str, object] | None,
    result: OnboardingResult,
) -> dict[str, object]:
    config = dict(existing or {})
    for key in tuple(config):
        if "api_key" in str(key).casefold():
            config.pop(key, None)
    config.update(
        {
            "download_folder": str(result.download_folder.resolve()),
            "audio_quality": result.audio_quality,
            "onboarding_completed": True,
            "authorized_use_acknowledged": result.authorized_use_acknowledged,
        }
    )
    if result.ffmpeg_location:
        config["ffmpeg_location"] = result.ffmpeg_location
    return config


def _body(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("MutedLabel")
    label.setWordWrap(True)
    return label


def _page(title: str, subtitle: str) -> tuple[QWizardPage, QVBoxLayout]:
    page = QWizardPage()
    page.setTitle(title)
    page.setSubTitle(subtitle)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(12, 16, 12, 12)
    layout.setSpacing(12)
    return page, layout


class FirstRunWizard(QWizard):
    """Small first-run guide; it performs no persistence by itself."""

    def __init__(
        self,
        *,
        portable_folder: Path,
        data_folder: Path,
        download_folder: Path,
        config: Mapping[str, object] | None = None,
        ffmpeg_ready: bool = False,
        ffmpeg_location: str | None = None,
        ffmpeg_validator: Callable[[str | None], tuple[bool, str]] | None = None,
        setup_docs_url: str | None = None,
        allow_data_folder_change: bool = True,
        create_shortcut_default: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Windows defaults QWizard to its native Aero surface, which can keep a
        # white page behind the application's light-on-dark text.  ClassicStyle
        # consistently honors the shared Qt stylesheet across supported hosts.
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setObjectName("FirstRunWizard")
        self.setWindowTitle(f"Welcome to Music Vault {DISPLAY_VERSION}")
        self.setStyleSheet(
            application_stylesheet()
            + f"""
QWizard#FirstRunWizard, QWizard#FirstRunWizard QWizardPage {{
    background: {COLORS['app_background']};
}}
QWizard#FirstRunWizard QLabel#qt_wizard_title {{
    color: {COLORS['text_primary']};
}}
QWizard#FirstRunWizard QLabel#qt_wizard_subtitle {{
    color: {COLORS['text_secondary']};
}}
"""
        )
        self.setMinimumSize(760, 620)
        self.resize(820, 680)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.HaveCustomButton1, True)
        self.setButtonText(QWizard.WizardButton.CustomButton1, "Skip setup")
        self.customButtonClicked.connect(self._custom_button_clicked)
        self.currentIdChanged.connect(self._refresh_finish_summary)
        self._skipped = False
        self._setup_docs_url = setup_docs_url
        self._ffmpeg_validator = ffmpeg_validator
        self._allow_data_folder_change = allow_data_folder_change
        self._initial_data_folder = data_folder.expanduser().resolve()
        self._initial_download_folder = download_folder.expanduser().resolve()

        self._add_welcome_page()
        self._add_storage_page(portable_folder, data_folder, download_folder)
        self._add_local_library_page()
        self._add_youtube_page()
        self._add_quality_page(config or {})
        self._add_ffmpeg_page(ffmpeg_ready, ffmpeg_location)
        self._add_shortcut_page(create_shortcut_default)
        self._add_finish_page()
        self.authorized_ack.toggled.connect(self._update_sync_availability)
        self.configure_youtube.toggled.connect(self._update_api_key_availability)
        self._update_sync_availability(self.authorized_ack.isChecked())

    def _add_welcome_page(self) -> None:
        page, layout = _page(
            "Welcome",
            f"Music Vault {APP_VERSION} is a standalone, local-first music system.",
        )
        layout.addWidget(
            _body(
                "Music Vault creates a private library on this computer. You can import "
                "local files now, or optionally configure public/unlisted YouTube playlist "
                "synchronization later. You are responsible for using content you own or "
                "are authorized to download."
            )
        )
        self.authorized_ack = QCheckBox(
            "I understand that YouTube synchronization is only for authorized content."
        )
        self.authorized_ack.setAccessibleName("Acknowledge authorized-use notice")
        layout.addWidget(self.authorized_ack)
        layout.addWidget(
            _body(
                "This acknowledgement is required only to configure YouTube sync. "
                "It is never required to import or play local files."
            )
        )
        layout.addStretch(1)
        self.addPage(page)

    def _browse_directory(self, field: QLineEdit, title: str) -> None:
        selected = QFileDialog.getExistingDirectory(self, title, field.text().strip())
        if selected:
            field.setText(selected)

    def _add_storage_page(
        self, portable_folder: Path, data_folder: Path, download_folder: Path
    ) -> None:
        page, layout = _page(
            "Storage",
            "Choose writable locations. Music Vault never scans the whole computer.",
        )
        form = QFormLayout()
        self.portable_folder = QLineEdit(str(portable_folder.resolve()))
        self.portable_folder.setReadOnly(True)
        self.data_folder = QLineEdit(str(data_folder.resolve()))
        self.data_folder.setReadOnly(not self._allow_data_folder_change)
        self.download_folder = QLineEdit(str(download_folder.resolve()))
        form.addRow("Portable application", self.portable_folder)

        data_row = QHBoxLayout()
        data_row.addWidget(self.data_folder, 1)
        data_browse = QPushButton("Browse")
        data_browse.setEnabled(self._allow_data_folder_change)
        data_browse.clicked.connect(
            lambda: self._browse_directory(self.data_folder, "Choose runtime data folder")
        )
        data_row.addWidget(data_browse)
        form.addRow("Private runtime data", data_row)

        download_row = QHBoxLayout()
        download_row.addWidget(self.download_folder, 1)
        download_browse = QPushButton("Browse")
        download_browse.clicked.connect(
            lambda: self._browse_directory(self.download_folder, "Choose download folder")
        )
        download_row.addWidget(download_browse)
        form.addRow("Default downloads", download_row)
        layout.addLayout(form)
        layout.addWidget(
            _body("The portable package starts blank. Runtime files remain in the selected private data folder.")
        )
        layout.addStretch(1)
        self.addPage(page)

    def _add_local_library_page(self) -> None:
        page, layout = _page(
            "Local Library",
            "Import one folder now, or start with an empty library.",
        )
        self.local_import_folder = QLineEdit()
        self.local_import_folder.setPlaceholderText("Optional music folder")
        row = QHBoxLayout()
        row.addWidget(self.local_import_folder, 1)
        browse = QPushButton("Choose Folder")
        browse.clicked.connect(
            lambda: self._browse_directory(self.local_import_folder, "Choose music folder")
        )
        row.addWidget(browse)
        layout.addLayout(row)
        layout.addWidget(
            _body("Only the folder you explicitly choose will be imported. You can also import later from Library.")
        )
        layout.addStretch(1)
        self.addPage(page)

    def _add_youtube_page(self) -> None:
        page, layout = _page(
            "Optional YouTube Sync",
            "Configure authorized public or unlisted playlist synchronization, or skip it.",
        )
        self.configure_youtube = QCheckBox("Configure YouTube synchronization now")
        self.configure_youtube.setAccessibleName("Configure YouTube synchronization")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("YouTube Data API key (optional)")
        self.api_key.setAccessibleName("YouTube Data API key")
        layout.addWidget(self.configure_youtube)
        layout.addWidget(self.api_key)
        self.youtube_note = _body(
            "Private-playlist OAuth is not included. Music Vault does not start a sync automatically."
        )
        layout.addWidget(self.youtube_note)
        layout.addStretch(1)
        self.addPage(page)

    def _add_quality_page(self, config: Mapping[str, object]) -> None:
        page, layout = _page(
            "Audio Quality",
            "Choose the conversion target used by optional YouTube downloads.",
        )
        self.audio_quality = QComboBox()
        self.audio_quality.addItems(list(SUPPORTED_AUDIO_QUALITIES))
        quality = str(config.get("audio_quality", "320"))
        self.audio_quality.setCurrentText(
            quality if quality in SUPPORTED_AUDIO_QUALITIES else "320"
        )
        layout.addWidget(self.audio_quality)
        layout.addWidget(
            _body("This controls the conversion target; it cannot improve the quality of the source audio.")
        )
        layout.addStretch(1)
        self.addPage(page)

    def _add_ffmpeg_page(self, ready: bool, location: str | None) -> None:
        page, layout = _page(
            "FFmpeg Readiness",
            "FFmpeg and ffprobe are optional for basic local-library use.",
        )
        self._ffmpeg_ready = bool(ready)
        self.ffmpeg_location = QLineEdit(location or "")
        self.ffmpeg_location.setPlaceholderText("Optional folder or ffmpeg.exe location")
        row = QHBoxLayout()
        row.addWidget(self.ffmpeg_location, 1)
        browse = QPushButton("Choose")
        browse.clicked.connect(self._choose_ffmpeg)
        row.addWidget(browse)
        layout.addLayout(row)
        self.ffmpeg_readiness = QLabel(
            "FFmpeg and ffprobe: Ready" if ready else "FFmpeg and ffprobe: Not configured"
        )
        self.ffmpeg_readiness.setObjectName("StatusLine")
        self.ffmpeg_location.editingFinished.connect(self._refresh_ffmpeg_readiness)
        layout.addWidget(self.ffmpeg_readiness)
        layout.addWidget(
            _body(
                "Local playback and import may work without FFmpeg. YouTube conversion and some processing require both tools."
            )
        )
        docs = QPushButton("Open FFmpeg Setup Guide")
        docs.setEnabled(bool(self._setup_docs_url))
        docs.clicked.connect(self._open_setup_docs)
        layout.addWidget(docs)
        layout.addStretch(1)
        self.addPage(page)
        if self._ffmpeg_validator is not None:
            self._refresh_ffmpeg_readiness()

    def _choose_ffmpeg(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose ffmpeg.exe",
            self.ffmpeg_location.text().strip(),
            "FFmpeg executable (ffmpeg.exe);;All files (*)",
        )
        if selected:
            self.ffmpeg_location.setText(selected)
            self._refresh_ffmpeg_readiness()

    def _refresh_ffmpeg_readiness(self) -> None:
        location = self.ffmpeg_location.text().strip() or None
        if self._ffmpeg_validator is None:
            self._ffmpeg_ready = bool(location)
            self.ffmpeg_readiness.setText(
                "FFmpeg and ffprobe: Configured" if location else "FFmpeg and ffprobe: Not configured"
            )
            return
        ready, detail = self._ffmpeg_validator(location)
        self._ffmpeg_ready = ready
        self.ffmpeg_readiness.setText(
            ("FFmpeg and ffprobe: Ready" if ready else "FFmpeg and ffprobe: Not ready")
            + (f"\n{detail}" if detail else "")
        )

    def _open_setup_docs(self) -> None:
        if self._setup_docs_url:
            QDesktopServices.openUrl(QUrl(self._setup_docs_url))

    def _add_shortcut_page(self, checked: bool) -> None:
        page, layout = _page(
            "Desktop Shortcut",
            "Optionally create a shortcut for this portable copy without administrator access.",
        )
        self.create_shortcut = QCheckBox("Create a desktop shortcut")
        self.create_shortcut.setChecked(checked)
        layout.addWidget(self.create_shortcut)
        layout.addWidget(
            _body("An existing shortcut that targets another copy will not be silently replaced.")
        )
        layout.addStretch(1)
        self.addPage(page)

    def _add_finish_page(self) -> None:
        page, layout = _page("Ready", "Review readiness and finish setup.")
        self.finish_summary = QLabel()
        self.finish_summary.setObjectName("StatusLine")
        self.finish_summary.setWordWrap(True)
        layout.addWidget(self.finish_summary)
        layout.addStretch(1)
        self.addPage(page)

    def _update_sync_availability(self, acknowledged: bool) -> None:
        self.configure_youtube.setEnabled(acknowledged)
        if not acknowledged:
            self.configure_youtube.setChecked(False)
        self._update_api_key_availability(self.configure_youtube.isChecked())

    def _update_api_key_availability(self, configure: bool) -> None:
        self.api_key.setEnabled(bool(configure and self.authorized_ack.isChecked()))

    def _refresh_finish_summary(self, _page_id: int = -1) -> None:
        if not hasattr(self, "finish_summary"):
            return
        local = self.local_import_folder.text().strip() or "Not selected"
        youtube = "Ready" if self.configure_youtube.isChecked() and self.api_key.text().strip() else "Not configured"
        ffmpeg = "Ready" if self._ffmpeg_ready else "Not configured"
        shortcut = "Will be created" if self.create_shortcut.isChecked() else "Not requested"
        self.finish_summary.setText(
            f"Runtime Data: {self.data_folder.text().strip()}\n"
            f"Local Import: {local}\n"
            f"YouTube API: {youtube}\n"
            f"FFmpeg: {ffmpeg}\n"
            f"Desktop Shortcut: {shortcut}"
        )

    def _validated_folders(self) -> bool:
        for label, field in (
            ("runtime data", self.data_folder),
            ("downloads", self.download_folder),
        ):
            text = field.text().strip()
            if not text:
                QMessageBox.warning(self, "Storage required", f"Choose a {label} folder.")
                return False
            ok, error = validate_writable_folder(Path(text))
            if not ok:
                QMessageBox.warning(self, "Storage unavailable", error or "The folder is not writable.")
                return False
        return True

    def validateCurrentPage(self) -> bool:  # noqa: N802 - Qt API name
        # Validate whenever leaving Storage and once more on Finish.
        if self.currentPage() is self.page(1) or self.currentId() == self.pageIds()[-1]:
            return self._validated_folders()
        return True

    def accept(self) -> None:
        if self._validated_folders():
            super().accept()

    def _custom_button_clicked(self, button: int) -> None:
        if button != QWizard.WizardButton.CustomButton1.value:
            return
        if not self._validated_folders():
            return
        self._skipped = True
        self.configure_youtube.setChecked(False)
        self.api_key.clear()
        self.local_import_folder.clear()
        self.create_shortcut.setChecked(False)
        super().accept()

    def result_values(self) -> OnboardingResult:
        selected_data = Path(self.data_folder.text().strip()).expanduser().resolve()
        selected_download = Path(self.download_folder.text().strip()).expanduser().resolve()
        if (
            selected_data != self._initial_data_folder
            and selected_download == self._initial_download_folder
        ):
            selected_download = selected_data / "youtube_downloads"
        local_text = self.local_import_folder.text().strip()
        ffmpeg_text = self.ffmpeg_location.text().strip()
        configure_sync = bool(
            self.authorized_ack.isChecked() and self.configure_youtube.isChecked()
        )
        return OnboardingResult(
            data_folder=selected_data,
            download_folder=selected_download,
            local_import_folder=(Path(local_text).expanduser().resolve() if local_text else None),
            api_key=self.api_key.text().strip() if configure_sync else "",
            authorized_use_acknowledged=self.authorized_ack.isChecked(),
            audio_quality=self.audio_quality.currentText(),
            ffmpeg_location=ffmpeg_text or None,
            create_shortcut=self.create_shortcut.isChecked(),
            skipped=self._skipped,
        )
