from __future__ import annotations

import sys
import random
import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QThread, Signal, QSize, QTimer
from PySide6.QtGui import QPixmap, QDesktopServices, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QMediaDevices
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFileDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QMessageBox,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QFrame,
    QGroupBox,
    QCheckBox,
    QComboBox,
    QTextEdit,
    QStackedWidget,
    QSlider,
    QProgressBar,
    QHeaderView,
    QScrollArea,
    QGridLayout,
    QMenu,
)

from music_vault.core.db import MusicVaultDB
from music_vault.core.app_status import write_app_status as export_app_status
from music_vault.core.importer import (
    ImportSourceContext,
    import_file,
    import_folder,
    refresh_covers_for_library,
)
from music_vault.core.playback_errors import playback_error_message
from music_vault.core.paths import (
    app_status_path,
    config_path,
    data_dir,
    database_path,
    default_downloads_dir,
    icon_path,
    youtube_api_key_path,
    youtube_download_archive_path,
    youtube_failed_ids_path,
)
from music_vault.core.safety import sanitize_error_text
from music_vault.core.sync_result import SyncFailure, SyncResult, sync_ui_values
from music_vault.core.youtube_sync import YouTubeSyncConfig, AuthorizedYouTubePlaylistSyncer
from music_vault.metadata.musicbrainz_enricher import search_recording
from music_vault.metadata.cover_art import download_front_cover



class YouTubeSyncWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(object)

    def __init__(
        self,
        playlist_url: str,
        output_dir: str,
        audio_quality: str = "320",
        existing_video_ids: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.playlist_url = playlist_url
        self.output_dir = output_dir
        self.audio_quality = audio_quality
        self.existing_video_ids = existing_video_ids

    def run(self) -> None:
        try:
            output = Path(self.output_dir)
            config = YouTubeSyncConfig(
                playlist_url=self.playlist_url,
                output_dir=output,
                archive_file=youtube_download_archive_path(),
                audio_format="mp3",
                audio_quality=self.audio_quality,
                existing_video_ids=self.existing_video_ids,
            )
            syncer = AuthorizedYouTubePlaylistSyncer(config, progress=self.progress.emit)
            result = syncer.sync()
            self.finished_ok.emit(result)
        except Exception as exc:
            self.finished_ok.emit(SyncResult.failed_result(exc))


class MusicVaultWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("Music Vault v1.0")

        app_icon_path = icon_path()
        if app_icon_path.exists():
            self.setWindowIcon(QIcon(str(app_icon_path)))
        self.resize(1380, 860)

        self.config = self.load_config()
        self.db = MusicVaultDB(
            youtube_download_root=self.config.get("download_folder"),
            legacy_failure_file=youtube_failed_ids_path(),
        )
        self.current_track_id: int | None = None
        self.sync_worker: YouTubeSyncWorker | None = None
        self.is_seeking = False
        self._handling_media_error = False
        self.current_view_kind = "library"
        self.current_playlist_id: int | None = None
        self.current_playlist_name = "Library"
        self.autoplay_enabled = True
        self.shuffle_enabled = False
        self.repeat_mode = "off"  # off, all, one
        self.manual_queue: list[int] = []
        self.base_playback_context: dict | None = None

        self.app_sync_status: dict | None = None

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.75)

        self.current_audio_device_key = None
        self.use_system_default_audio_output()

        self.audio_device_timer = QTimer(self)
        self.audio_device_timer.timeout.connect(self.use_system_default_audio_output)
        self.audio_device_timer.start(2000)

        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_media_error)

        self.build_ui()
        self.load_library()
        self.load_playlists()
        self.refresh_settings_status()
        self.write_app_status()


    def config_file_path(self) -> Path:
        return config_path()

    def default_config(self) -> dict:
        return {
            "download_folder": str(default_downloads_dir()),
            "audio_quality": "320",
        }

    def load_config(self) -> dict:
        config = self.default_config()
        path = self.config_file_path()

        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))

                if isinstance(saved, dict):
                    config.update(saved)
        except Exception:
            pass

        return config

    def save_config(self) -> None:
        path = self.config_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def api_key_path(self) -> Path:
        return youtube_api_key_path()

    def read_saved_api_key(self) -> str:
        path = self.api_key_path()

        if not path.exists():
            return ""

        return path.read_text(encoding="utf-8", errors="ignore").strip()

    def write_app_status(self, extra: dict | None = None) -> None:
        try:
            track = self.db.get_track(self.current_track_id) if self.current_track_id else None
            api_ready = bool(self.read_saved_api_key())
            ffmpeg_ready = bool(self.find_ffmpeg_bin())

            status_extra = {
                "health": {
                    "ok": api_ready and ffmpeg_ready,
                    "api_ready": api_ready,
                    "ffmpeg_ready": ffmpeg_ready,
                },
                "playback": {
                    "currently_playing": self.current_track_id,
                    "current_title": track["title"] if track else None,
                    "current_artist": track["artist"] if track else None,
                    "current_album": track["album"] if track else None,
                    "is_playing": self.player.playbackState() == QMediaPlayer.PlayingState,
                    "shuffle_enabled": self.shuffle_enabled,
                    "autoplay_enabled": self.autoplay_enabled,
                    "repeat_mode": self.repeat_mode,
                    "queue_count": len(self.manual_queue),
                },
            }

            if self.app_sync_status is not None:
                status_extra["sync"] = self.app_sync_status

            if isinstance(extra, dict):
                for section in ("health", "playback", "sync"):
                    values = extra.get(section)
                    if isinstance(values, dict) and isinstance(status_extra.get(section), dict):
                        status_extra[section].update(values)
                    elif isinstance(values, dict):
                        status_extra[section] = values

            export_app_status(self.db, self.config, status_extra)
        except Exception:
            pass

    def build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(16)

        self.sidebar = self.build_sidebar()
        self.pages = QStackedWidget()

        self.library_page = self.build_library_page()
        self.sync_page = self.build_sync_page()
        self.settings_page = self.build_settings_page()

        self.pages.addWidget(self.library_page)
        self.pages.addWidget(self.sync_page)
        self.pages.addWidget(self.settings_page)

        main_shell = QFrame()
        main_shell.setObjectName("MainShell")
        main_layout = QVBoxLayout(main_shell)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(14)

        main_layout.addWidget(self.pages, 1)
        main_layout.addWidget(self.build_player_bar())

        root_layout.addWidget(self.sidebar)
        root_layout.addWidget(main_shell, 1)

        self.setCentralWidget(root)
        self.apply_styles()



    def build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(260)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 22, 18, 22)
        layout.setSpacing(10)

        brand_row = QHBoxLayout()
        logo = QLabel("♪")
        logo.setObjectName("LogoBadge")
        logo.setFixedSize(42, 42)
        logo.setAlignment(Qt.AlignCenter)

        brand_col = QVBoxLayout()
        brand = QLabel("Music Vault")
        brand.setObjectName("Brand")
        subtitle = QLabel("Personal player")
        subtitle.setObjectName("MutedLabel")
        brand_col.addWidget(brand)
        brand_col.addWidget(subtitle)

        brand_row.addWidget(logo)
        brand_row.addLayout(brand_col, 1)

        self.library_btn = self.sidebar_button("Library", 0)
        self.sync_btn_nav = self.sidebar_button("Sync Center", 1)
        self.settings_btn = self.sidebar_button("Settings", 2)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)

        section = QLabel("PLAYLISTS")
        section.setObjectName("SectionLabel")

        self.playlists = QListWidget()
        self.playlists.setObjectName("PlaylistList")
        self.playlists.itemClicked.connect(self.on_playlist_clicked)

        layout.addLayout(brand_row)
        layout.addSpacing(18)
        layout.addWidget(self.library_btn)
        layout.addWidget(self.sync_btn_nav)
        layout.addWidget(self.settings_btn)
        layout.addSpacing(18)
        layout.addWidget(divider)
        layout.addSpacing(10)
        layout.addWidget(section)
        layout.addWidget(self.playlists, 1)

        return sidebar

    def sidebar_button(self, text: str, page_index: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("SidebarButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.pages.setCurrentIndex(page_index))
        return btn




    def build_library_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("HeroHeader")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(16)

        top_row = QHBoxLayout()
        title_col = QVBoxLayout()
        self.page_title = QLabel("Library")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("Your local music collection, synced and ready.")
        self.page_subtitle.setObjectName("MutedLabel")
        title_col.addWidget(self.page_title)
        title_col.addWidget(self.page_subtitle)

        import_btn = QPushButton("Import Folder")
        import_btn.setObjectName("PrimaryButton")
        import_btn.clicked.connect(self.import_music_folder)

        create_playlist_btn = QPushButton("New Playlist")
        create_playlist_btn.setObjectName("SoftButton")
        create_playlist_btn.clicked.connect(self.create_playlist)

        add_playlist_btn = QPushButton("Add to Playlist")
        add_playlist_btn.setObjectName("SoftButton")
        add_playlist_btn.clicked.connect(self.add_selected_to_playlist)

        queue_next_btn = QPushButton("Queue Next")
        queue_next_btn.setObjectName("SoftButton")
        queue_next_btn.clicked.connect(self.queue_selected_next)

        remove_playlist_btn = QPushButton("Remove From Playlist")
        remove_playlist_btn.setObjectName("SoftButton")
        remove_playlist_btn.clicked.connect(self.remove_selected_from_current_playlist)

        enrich_btn = QPushButton("Enrich Selected")
        enrich_btn.setObjectName("SoftButton")
        enrich_btn.clicked.connect(self.enrich_selected)

        clean_btn = QPushButton("Remove Missing")
        clean_btn.setObjectName("SoftButton")
        clean_btn.clicked.connect(self.remove_missing_tracks)

        refresh_art_btn = QPushButton("Refresh Art")
        refresh_art_btn.setObjectName("SoftButton")
        refresh_art_btn.clicked.connect(self.refresh_artwork)

        top_row.addLayout(title_col, 1)
        top_row.addWidget(import_btn)
        top_row.addWidget(create_playlist_btn)
        top_row.addWidget(add_playlist_btn)
        top_row.addWidget(queue_next_btn)
        top_row.addWidget(remove_playlist_btn)
        top_row.addWidget(enrich_btn)
        top_row.addWidget(clean_btn)
        top_row.addWidget(refresh_art_btn)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search songs, artists, albums...")
        self.search_box.textChanged.connect(self.filter_library)
        self.search_box.setObjectName("SearchBox")

        hero_layout.addLayout(top_row)
        hero_layout.addWidget(self.search_box)

        stats_row = QHBoxLayout()
        self.track_count_card = self.stat_card("Tracks", "0")
        self.download_folder_card = self.stat_card("Downloads", "Ready")
        self.api_status_card = self.stat_card("API", "Checking...")
        stats_row.addWidget(self.track_count_card)
        stats_row.addWidget(self.download_folder_card)
        stats_row.addWidget(self.api_status_card)

        table_card = QFrame()
        table_card.setObjectName("Card")
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(18, 18, 18, 18)
        table_layout.setSpacing(12)

        table_header = QHBoxLayout()
        table_title = QLabel("Songs")
        table_title.setObjectName("CardTitle")
        table_hint = QLabel("Double-click a track to play")
        table_hint.setObjectName("MutedLabel")
        table_header.addWidget(table_title)
        table_header.addStretch(1)
        table_header.addWidget(table_hint)

        self.library_table = QTableWidget(0, 5)
        self.library_table.setObjectName("LibraryTable")
        self.library_table.setHorizontalHeaderLabels(["Title", "Artist", "Album", "Year", "Path"])
        self.library_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.library_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.library_table.doubleClicked.connect(self.play_selected)
        self.library_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.library_table.customContextMenuRequested.connect(self.open_song_context_menu)
        self.library_table.verticalHeader().setVisible(False)
        self.library_table.setAlternatingRowColors(True)
        self.library_table.setShowGrid(False)
        self.library_table.horizontalHeader().setStretchLastSection(True)
        self.library_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.library_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.library_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.library_table.setColumnHidden(4, True)

        table_layout.addLayout(table_header)
        table_layout.addWidget(self.library_table, 1)

        browser_page = QFrame()
        browser_page.setObjectName("Card")
        browser_layout = QVBoxLayout(browser_page)
        browser_layout.setContentsMargins(18, 18, 18, 18)
        browser_layout.setSpacing(12)

        browser_header = QHBoxLayout()
        self.browser_title = QLabel("Browse")
        self.browser_title.setObjectName("CardTitle")
        self.browser_hint = QLabel("Click a card to open it")
        self.browser_hint.setObjectName("MutedLabel")
        browser_header.addWidget(self.browser_title)
        browser_header.addStretch(1)
        browser_header.addWidget(self.browser_hint)

        self.browser_scroll = QScrollArea()
        self.browser_scroll.setObjectName("BrowserScroll")
        self.browser_scroll.setWidgetResizable(True)

        self.browser_container = QWidget()
        self.browser_grid = QGridLayout(self.browser_container)
        self.browser_grid.setContentsMargins(4, 4, 4, 4)
        self.browser_grid.setHorizontalSpacing(16)
        self.browser_grid.setVerticalSpacing(16)

        self.browser_scroll.setWidget(self.browser_container)

        browser_layout.addLayout(browser_header)
        browser_layout.addWidget(self.browser_scroll, 1)

        self.library_content_stack = QStackedWidget()
        self.library_content_stack.addWidget(table_card)
        self.library_content_stack.addWidget(browser_page)

        layout.addWidget(hero)
        layout.addLayout(stats_row)
        layout.addWidget(self.library_content_stack, 1)

        return page


    def sync_metric_card(self, label: str, value: str) -> QFrame:
        card = QFrame()
        card.setObjectName("SyncMetricCard")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        value_label = QLabel(value)
        value_label.setObjectName("SyncMetricValue")

        text_label = QLabel(label)
        text_label.setObjectName("MutedLabel")

        card.value_label = value_label

        layout.addWidget(value_label)
        layout.addWidget(text_label)

        return card

    def reset_sync_dashboard(self) -> None:
        if hasattr(self, "sync_status_card"):
            self.sync_status_card.value_label.setText("Idle")

        if hasattr(self, "sync_downloaded_card"):
            self.sync_downloaded_card.value_label.setText("0")

        if hasattr(self, "sync_skipped_card"):
            self.sync_skipped_card.value_label.setText("—")

        if hasattr(self, "sync_failed_card"):
            self.sync_failed_card.value_label.setText("0")

        if hasattr(self, "sync_progress"):
            self.sync_progress.setRange(0, 100)
            self.sync_progress.setValue(0)
            self.sync_progress.setFormat("Ready")

    def set_sync_status(self, status: str) -> None:
        if hasattr(self, "sync_status_card"):
            self.sync_status_card.value_label.setText(status)

        if hasattr(self, "sync_progress") and self.sync_progress.maximum() != 0:
            self.sync_progress.setFormat(status)

    def clear_sync_log(self) -> None:
        if hasattr(self, "youtube_log"):
            self.youtube_log.clear()

        self.reset_sync_dashboard()

    def update_sync_quality_label(self) -> None:
        if hasattr(self, "sync_quality_label"):
            quality = str(self.config.get("audio_quality", "320"))
            self.sync_quality_label.setText(f"Saved quality: {quality} kbps")


    def build_sync_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("TopHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 18, 22, 18)

        title = QLabel("Sync Center")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Bring authorized playlist music into Music Vault.")
        subtitle.setObjectName("MutedLabel")

        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        metric_row = QHBoxLayout()
        self.sync_status_card = self.sync_metric_card("Status", "Idle")
        self.sync_downloaded_card = self.sync_metric_card("Downloaded", "0")
        self.sync_skipped_card = self.sync_metric_card("Existing", "—")
        self.sync_failed_card = self.sync_metric_card("Failed", "0")

        metric_row.addWidget(self.sync_status_card)
        metric_row.addWidget(self.sync_downloaded_card)
        metric_row.addWidget(self.sync_skipped_card)
        metric_row.addWidget(self.sync_failed_card)

        sync_card = QFrame()
        sync_card.setObjectName("Card")
        sync_layout = QVBoxLayout(sync_card)
        sync_layout.setContentsMargins(18, 18, 18, 18)
        sync_layout.setSpacing(12)

        form_title = QLabel("Playlist Sync")
        form_title.setObjectName("CardTitle")

        self.sync_quality_label = QLabel()
        self.sync_quality_label.setObjectName("MutedLabel")
        self.update_sync_quality_label()

        self.youtube_url = QLineEdit()
        self.youtube_url.setPlaceholderText("Paste your YouTube playlist URL")
        self.youtube_url.setObjectName("SearchBox")

        output_row = QHBoxLayout()
        self.youtube_output = QLineEdit(
            self.config.get(
                "download_folder",
                str(default_downloads_dir())
            )
        )
        self.youtube_output.setObjectName("SearchBox")

        choose_output = QPushButton("Choose Folder")
        choose_output.setObjectName("SoftButton")
        choose_output.clicked.connect(self.choose_youtube_output)

        open_output = QPushButton("Open Downloads")
        open_output.setObjectName("SoftButton")
        open_output.clicked.connect(self.open_youtube_output)

        output_row.addWidget(self.youtube_output, 1)
        output_row.addWidget(choose_output)
        output_row.addWidget(open_output)

        self.youtube_confirm = QCheckBox("I own this music or have permission to download it.")
        self.youtube_confirm.setObjectName("PermissionCheck")

        action_row = QHBoxLayout()

        self.youtube_sync_btn = QPushButton("Start Sync")
        self.youtube_sync_btn.setObjectName("PrimaryButton")
        self.youtube_sync_btn.clicked.connect(self.sync_youtube_playlist)

        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.setObjectName("SoftButton")
        clear_log_btn.clicked.connect(self.clear_sync_log)

        action_row.addWidget(self.youtube_sync_btn)
        action_row.addWidget(clear_log_btn)
        action_row.addStretch(1)

        self.sync_progress = QProgressBar()
        self.sync_progress.setObjectName("SyncProgress")
        self.sync_progress.setRange(0, 100)
        self.sync_progress.setValue(0)
        self.sync_progress.setFormat("Ready")
        self.sync_progress.setTextVisible(True)

        log_header = QHBoxLayout()
        log_title = QLabel("Activity Log")
        log_title.setObjectName("CardTitle")
        log_hint = QLabel("Detailed sync messages")
        log_hint.setObjectName("MutedLabel")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        log_header.addWidget(log_hint)

        self.youtube_log = QTextEdit()
        self.youtube_log.setReadOnly(True)
        self.youtube_log.setObjectName("SyncLog")
        self.youtube_log.setPlaceholderText("Sync progress will appear here...")

        sync_layout.addWidget(form_title)
        sync_layout.addWidget(self.sync_quality_label)
        sync_layout.addWidget(QLabel("Playlist URL"))
        sync_layout.addWidget(self.youtube_url)
        sync_layout.addWidget(QLabel("Download Folder"))
        sync_layout.addLayout(output_row)
        sync_layout.addWidget(self.youtube_confirm)
        sync_layout.addLayout(action_row)
        sync_layout.addWidget(self.sync_progress)
        sync_layout.addLayout(log_header)
        sync_layout.addWidget(self.youtube_log, 1)

        layout.addWidget(header)
        layout.addLayout(metric_row)
        layout.addWidget(sync_card, 1)

        return page

    def build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("TopHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 18, 22, 18)

        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Control Music Vault without editing files manually.")
        subtitle.setObjectName("MutedLabel")

        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        settings_card = QFrame()
        settings_card.setObjectName("Card")
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(20, 20, 20, 20)
        settings_layout.setSpacing(18)

        youtube_title = QLabel("YouTube Sync")
        youtube_title.setObjectName("CardTitle")

        api_label = QLabel("YouTube API Key")
        api_label.setObjectName("MutedLabel")

        self.settings_api_key = QLineEdit()
        self.settings_api_key.setObjectName("SearchBox")
        self.settings_api_key.setPlaceholderText("Paste or update your YouTube API key")
        self.settings_api_key.setEchoMode(QLineEdit.Password)
        self.settings_api_key.setText(self.read_saved_api_key())

        folder_label = QLabel("Default Download Folder")
        folder_label.setObjectName("MutedLabel")

        folder_row = QHBoxLayout()
        self.settings_download_folder = QLineEdit()
        self.settings_download_folder.setObjectName("SearchBox")
        self.settings_download_folder.setText(
            self.config.get("download_folder", str(default_downloads_dir()))
        )

        choose_folder_btn = QPushButton("Choose")
        choose_folder_btn.setObjectName("SoftButton")
        choose_folder_btn.clicked.connect(self.choose_default_download_folder)

        open_downloads_btn = QPushButton("Open Downloads")
        open_downloads_btn.setObjectName("SoftButton")
        open_downloads_btn.clicked.connect(self.open_default_download_folder)

        folder_row.addWidget(self.settings_download_folder, 1)
        folder_row.addWidget(choose_folder_btn)
        folder_row.addWidget(open_downloads_btn)

        quality_label = QLabel("Audio Quality")
        quality_label.setObjectName("MutedLabel")

        self.settings_quality = QComboBox()
        self.settings_quality.setObjectName("QualityCombo")
        self.settings_quality.addItems(["192", "256", "320"])

        quality = str(self.config.get("audio_quality", "320"))

        if quality in ["192", "256", "320"]:
            self.settings_quality.setCurrentText(quality)
        else:
            self.settings_quality.setCurrentText("320")

        save_btn = QPushButton("Save Settings")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self.save_settings_from_ui)

        maintenance_title = QLabel("Maintenance")
        maintenance_title.setObjectName("CardTitle")

        maintenance_row = QHBoxLayout()

        open_data_btn = QPushButton("Open Data Folder")
        open_data_btn.setObjectName("SoftButton")
        open_data_btn.clicked.connect(self.open_data_folder)

        clear_failed_btn = QPushButton("Clear Failure History")
        clear_failed_btn.setObjectName("SoftButton")
        clear_failed_btn.clicked.connect(self.clear_failed_downloads)

        refresh_btn = QPushButton("Refresh Status")
        refresh_btn.setObjectName("SoftButton")
        refresh_btn.clicked.connect(self.refresh_settings_status)

        clean_btn = QPushButton("Remove Missing Tracks")
        clean_btn.setObjectName("SoftButton")
        clean_btn.clicked.connect(self.remove_missing_tracks)

        maintenance_row.addWidget(open_data_btn)
        maintenance_row.addWidget(clear_failed_btn)
        maintenance_row.addWidget(refresh_btn)
        maintenance_row.addWidget(clean_btn)
        maintenance_row.addStretch(1)

        status_title = QLabel("Status")
        status_title.setObjectName("CardTitle")

        self.api_key_status = QLabel()
        self.api_key_status.setObjectName("StatusLine")

        self.ffmpeg_status = QLabel()
        self.ffmpeg_status.setObjectName("StatusLine")

        self.db_status = QLabel()
        self.db_status.setObjectName("StatusLine")

        self.config_status = QLabel()
        self.config_status.setObjectName("StatusLine")

        self.app_status_line = QLabel()
        self.app_status_line.setObjectName("StatusLine")

        settings_layout.addWidget(youtube_title)
        settings_layout.addWidget(api_label)
        settings_layout.addWidget(self.settings_api_key)
        settings_layout.addWidget(folder_label)
        settings_layout.addLayout(folder_row)
        settings_layout.addWidget(quality_label)
        settings_layout.addWidget(self.settings_quality)
        settings_layout.addWidget(save_btn)
        settings_layout.addSpacing(10)
        settings_layout.addWidget(maintenance_title)
        settings_layout.addLayout(maintenance_row)
        settings_layout.addSpacing(10)
        settings_layout.addWidget(status_title)
        settings_layout.addWidget(self.api_key_status)
        settings_layout.addWidget(self.ffmpeg_status)
        settings_layout.addWidget(self.db_status)
        settings_layout.addWidget(self.config_status)
        settings_layout.addWidget(self.app_status_line)
        settings_layout.addStretch(1)

        layout.addWidget(header)
        layout.addWidget(settings_card, 1)

        return page

    def build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("PlayerBar")
        bar.setFixedHeight(128)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(16)

        self.cover_art = QLabel()
        self.cover_art.setObjectName("CoverArt")
        self.cover_art.setFixedSize(82, 82)
        self.cover_art.setAlignment(Qt.AlignCenter)
        self.cover_art.setText("♪")

        track_info = QVBoxLayout()
        track_info.setSpacing(4)

        self.now_title = QLabel("No track selected")
        self.now_title.setObjectName("NowTitle")

        self.now_artist = QLabel("Double-click a song to play")
        self.now_artist.setObjectName("MutedLabel")

        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setObjectName("ProgressSlider")
        self.progress_slider.sliderPressed.connect(self.on_slider_pressed)
        self.progress_slider.sliderReleased.connect(self.on_slider_released)

        time_row = QHBoxLayout()
        self.elapsed_label = QLabel("0:00")
        self.elapsed_label.setObjectName("TinyLabel")
        self.duration_label = QLabel("0:00")
        self.duration_label.setObjectName("TinyLabel")
        time_row.addWidget(self.elapsed_label)
        time_row.addStretch(1)
        time_row.addWidget(self.duration_label)

        track_info.addWidget(self.now_title)
        track_info.addWidget(self.now_artist)
        track_info.addWidget(self.progress_slider)
        track_info.addLayout(time_row)

        controls_col = QVBoxLayout()
        controls_col.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(10)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("CircleButton")
        prev_btn.setToolTip("Previous")
        prev_btn.clicked.connect(self.play_previous)

        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("PlayButton")
        self.play_btn.setToolTip("Play / Pause")
        self.play_btn.clicked.connect(self.toggle_play)

        next_btn = QPushButton("›")
        next_btn.setObjectName("CircleButton")
        next_btn.setToolTip("Next")
        next_btn.clicked.connect(self.play_next)

        controls.addWidget(prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(next_btn)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)

        self.autoplay_btn = QPushButton("Auto On")
        self.autoplay_btn.setObjectName("ModeButtonActive")
        self.autoplay_btn.setToolTip("Toggle autoplay next track")
        self.autoplay_btn.clicked.connect(self.toggle_autoplay)

        self.shuffle_btn = QPushButton("Shuffle")
        self.shuffle_btn.setObjectName("ModeButton")
        self.shuffle_btn.setToolTip("Toggle shuffle")
        self.shuffle_btn.clicked.connect(self.toggle_shuffle)

        self.repeat_btn = QPushButton("Repeat Off")
        self.repeat_btn.setObjectName("ModeButton")
        self.repeat_btn.setToolTip("Cycle repeat mode")
        self.repeat_btn.clicked.connect(self.cycle_repeat)

        self.queue_label = QLabel("Queue: 0")
        self.queue_label.setObjectName("TinyLabel")
        self.queue_label.setToolTip("Songs queued to play next")

        mode_row.addWidget(self.autoplay_btn)
        mode_row.addWidget(self.shuffle_btn)
        mode_row.addWidget(self.repeat_btn)
        mode_row.addWidget(self.queue_label)

        controls_col.addLayout(controls)
        controls_col.addLayout(mode_row)

        volume_col = QVBoxLayout()
        volume_label = QLabel("Volume")
        volume_label.setObjectName("TinyLabel")
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setObjectName("VolumeSlider")
        self.volume_slider.setFixedWidth(140)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(75)
        self.volume_slider.valueChanged.connect(lambda value: self.audio_output.setVolume(value / 100))
        volume_col.addWidget(volume_label)
        volume_col.addWidget(self.volume_slider)

        layout.addWidget(self.cover_art)
        layout.addLayout(track_info, 1)
        layout.addLayout(controls_col)
        layout.addLayout(volume_col)

        return bar

    def stat_card(self, label: str, value: str) -> QFrame:
        card = QFrame()
        card.setObjectName("StatCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)

        value_label = QLabel(value)
        value_label.setObjectName("StatValue")

        text_label = QLabel(label)
        text_label.setObjectName("MutedLabel")

        card.value_label = value_label

        layout.addWidget(value_label)
        layout.addWidget(text_label)

        return card


    def apply_styles(self) -> None:
        self.setStyleSheet("""
            QWidget {
                background: #050607;
                color: #F5F7FA;
                font-family: Segoe UI;
                font-size: 13px;
            }

            QFrame#Sidebar {
                background: #070A0D;
                border: 1px solid #161B22;
                border-radius: 24px;
            }

            QLabel#LogoBadge {
                background: #1DB954;
                color: #050607;
                border-radius: 21px;
                font-size: 24px;
                font-weight: 900;
            }

            QLabel#Brand {
                font-size: 21px;
                font-weight: 900;
                color: #FFFFFF;
            }

            QLabel#PageTitle {
                font-size: 34px;
                font-weight: 900;
                color: #FFFFFF;
            }

            QLabel#CardTitle {
                font-size: 17px;
                font-weight: 850;
                color: #FFFFFF;
            }

            QLabel#MutedLabel {
                color: #A7B0BD;
                font-size: 12px;
            }

            QLabel#SectionLabel {
                color: #667085;
                font-size: 11px;
                font-weight: 900;
                letter-spacing: 2px;
            }

            QLabel#TinyLabel {
                color: #7E8794;
                font-size: 11px;
            }

            QLabel#NowTitle {
                color: #FFFFFF;
                font-size: 16px;
                font-weight: 900;
            }

            QLabel#StatusLine {
                background: #0D1117;
                border: 1px solid #202938;
                border-radius: 16px;
                padding: 14px;
                color: #DDE8F8;
            }

            QFrame#MainShell {
                background: transparent;
            }

            QFrame#HeroHeader {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #141A22,
                    stop:0.45 #10151C,
                    stop:1 #0B0F14);
                border: 1px solid #1E2936;
                border-radius: 26px;
            }

            QFrame#TopHeader,
            QFrame#Card,
            QFrame#StatCard {
                background: #0A0E13;
                border: 1px solid #18202B;
                border-radius: 24px;
            }

            QFrame#StatCard {
                min-height: 70px;
            }

            QLabel#StatValue {
                color: #FFFFFF;
                font-size: 24px;
                font-weight: 900;
            }

            QFrame#PlayerBar {
                background: #090D12;
                border: 1px solid #1E2936;
                border-radius: 28px;
            }

            QLabel#CoverArt {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #1DB954,
                    stop:0.55 #2563EB,
                    stop:1 #7C3AED);
                border: none;
                border-radius: 18px;
                color: #FFFFFF;
                font-size: 32px;
                font-weight: 900;
            }

            QPushButton {
                background: #111827;
                border: 1px solid #253244;
                color: #EEF5FF;
                padding: 10px 15px;
                border-radius: 16px;
                font-weight: 750;
            }

            QPushButton:hover {
                background: #182235;
                border: 1px solid #1DB954;
            }

            QPushButton:disabled {
                background: #0B0F14;
                color: #556070;
                border: 1px solid #161B22;
            }

            QPushButton#PrimaryButton {
                background: #1DB954;
                border: 1px solid #1ED760;
                color: #061008;
                font-weight: 900;
            }

            QPushButton#PrimaryButton:hover {
                background: #1ED760;
            }

            QPushButton#SoftButton {
                background: #10161F;
                border: 1px solid #253244;
            }

            QPushButton#SidebarButton {
                text-align: left;
                background: transparent;
                border: 1px solid transparent;
                color: #BCC7D6;
                padding: 13px 14px;
                border-radius: 14px;
                font-weight: 850;
            }

            QPushButton#SidebarButton:hover {
                background: #10161F;
                border: 1px solid #253244;
                color: #FFFFFF;
            }

            QPushButton#CircleButton {
                min-width: 44px;
                min-height: 44px;
                max-width: 44px;
                max-height: 44px;
                border-radius: 22px;
                font-size: 26px;
                padding: 0;
                background: #10161F;
            }

            QPushButton#PlayButton {
                min-width: 58px;
                min-height: 58px;
                max-width: 58px;
                max-height: 58px;
                border-radius: 29px;
                background: #FFFFFF;
                color: #050607;
                border: none;
                font-size: 20px;
                font-weight: 900;
                padding: 0;
            }

            QPushButton#PlayButton:hover {
                background: #1ED760;
                color: #061008;
            }

            QLineEdit#SearchBox,
            QLineEdit {
                background: #080C11;
                border: 1px solid #263244;
                border-radius: 18px;
                padding: 13px 16px;
                color: #EFF4FF;
                selection-background-color: #1DB954;
            }

            QLineEdit#SearchBox:focus,
            QLineEdit:focus {
                border: 1px solid #1DB954;
            }

            QTextEdit#SyncLog {
                background: #070A0D;
                border: 1px solid #202A38;
                border-radius: 18px;
                padding: 12px;
                color: #D0D8E5;
                font-family: Consolas;
                font-size: 12px;
            }

            QListWidget#PlaylistList {
                background: #06090D;
                border: 1px solid #18202B;
                border-radius: 16px;
                padding: 8px;
                outline: none;
            }

            QListWidget#PlaylistList::item {
                padding: 11px;
                border-radius: 11px;
                color: #B8C2D2;
            }

            QListWidget#PlaylistList::item:selected {
                background: #12351F;
                color: #FFFFFF;
            }

            QTableWidget#LibraryTable {
                background: #070A0D;
                alternate-background-color: #090E14;
                border: 1px solid #192230;
                border-radius: 18px;
                gridline-color: transparent;
                selection-background-color: #1DB954;
                selection-color: #061008;
                outline: none;
            }

            QTableWidget#LibraryTable::item {
                padding: 11px;
                border: none;
            }

            QTableWidget#LibraryTable::item:selected {
                background: #1DB954;
                color: #061008;
            }

            QHeaderView::section {
                background: #0E141C;
                color: #AAB4C2;
                border: none;
                padding: 12px;
                font-weight: 900;
            }

            QFrame#Divider {
                background: #1E293B;
            }

            QCheckBox#PermissionCheck {
                color: #CDD8E8;
                spacing: 10px;
            }


            QPushButton#ModeButton {
                background: #0D131B;
                border: 1px solid #263244;
                color: #AEB8C7;
                padding: 7px 11px;
                border-radius: 13px;
                font-size: 11px;
                font-weight: 850;
            }

            QPushButton#ModeButton:hover {
                color: #FFFFFF;
                border: 1px solid #1DB954;
            }

            QPushButton#ModeButtonActive {
                background: #12351F;
                border: 1px solid #1DB954;
                color: #1ED760;
                padding: 7px 11px;
                border-radius: 13px;
                font-size: 11px;
                font-weight: 900;
            }

            QPushButton#ModeButtonActive:hover {
                background: #164A2A;
            }


            QScrollArea#BrowserScroll {
                background: transparent;
                border: none;
            }

            QFrame#BrowserCard {
                background: #0D131B;
                border: 1px solid #202B3A;
                border-radius: 20px;
            }

            QFrame#BrowserCard:hover {
                background: #111A26;
                border: 1px solid #1DB954;
            }

            QLabel#BrowserCover {
                background: #070A0D;
                border-radius: 16px;
                color: #1DB954;
                font-size: 42px;
                font-weight: 900;
            }

            QLabel#BrowserCardTitle {
                color: #FFFFFF;
                font-size: 13px;
                font-weight: 900;
            }


            QComboBox#QualityCombo {
                background: #080C11;
                border: 1px solid #263244;
                border-radius: 16px;
                padding: 10px 14px;
                color: #EFF4FF;
                font-weight: 800;
            }

            QComboBox#QualityCombo:hover {
                border: 1px solid #1DB954;
            }

            QComboBox#QualityCombo::drop-down {
                border: none;
                width: 28px;
            }

            QComboBox#QualityCombo QAbstractItemView {
                background: #0D131B;
                color: #EFF4FF;
                selection-background-color: #1DB954;
                selection-color: #061008;
                border: 1px solid #263244;
            }


            QFrame#SyncMetricCard {
                background: #0A0E13;
                border: 1px solid #18202B;
                border-radius: 20px;
                min-height: 66px;
            }

            QLabel#SyncMetricValue {
                color: #FFFFFF;
                font-size: 20px;
                font-weight: 900;
            }

            QProgressBar#SyncProgress {
                background: #070A0D;
                border: 1px solid #202A38;
                border-radius: 12px;
                height: 24px;
                color: #FFFFFF;
                text-align: center;
                font-weight: 800;
            }

            QProgressBar#SyncProgress::chunk {
                background: #1DB954;
                border-radius: 10px;
            }

            QSlider::groove:horizontal {
                border: none;
                height: 6px;
                background: #263244;
                border-radius: 3px;
            }

            QSlider::sub-page:horizontal {
                background: #1DB954;
                border-radius: 3px;
            }

            QSlider::handle:horizontal {
                background: #FFFFFF;
                border: 2px solid #1DB954;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
        """)




    def load_library(self, tracks=None, title: str | None = None, subtitle: str | None = None) -> None:
        if hasattr(self, "library_content_stack"):
            self.library_content_stack.setCurrentIndex(0)

        if tracks is None:
            tracks = self.db.list_tracks()

        self.library_table.setRowCount(len(tracks))
        self.library_table.setIconSize(QSize(42, 42))

        for row_idx, track in enumerate(tracks):
            values = [
                track["title"] or Path(track["path"]).stem,
                track["artist"] or "",
                track["album"] or "",
                track["year"] or "",
                track["path"],
            ]

            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, track["id"])

                if col_idx == 0:
                    item.setToolTip(str(track["path"]))

                    cover_path = track["cover_path"] if "cover_path" in track.keys() else None

                    if cover_path and Path(cover_path).exists():
                        item.setIcon(QIcon(str(cover_path)))

                self.library_table.setItem(row_idx, col_idx, item)

            self.library_table.setRowHeight(row_idx, 54)

        self.track_count_card.value_label.setText(str(len(tracks)))

        if title and hasattr(self, "page_title"):
            self.page_title.setText(title)

        if subtitle and hasattr(self, "page_subtitle"):
            self.page_subtitle.setText(subtitle)

        self.filter_library(self.search_box.text() if hasattr(self, "search_box") else "")
        self.write_app_status()

    def load_playlists(self) -> None:
        self.playlists.clear()

        def add_sidebar_item(label: str, kind: str, playlist_id: int | None = None) -> None:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, {
                "kind": kind,
                "id": playlist_id,
                "name": label,
            })
            self.playlists.addItem(item)

        add_sidebar_item("Library", "library")
        add_sidebar_item("Recently Added", "recent")
        add_sidebar_item("Downloaded", "downloaded")
        add_sidebar_item("Albums", "albums")
        add_sidebar_item("Artists", "artists")
        add_sidebar_item("+ New Playlist", "new")

        for playlist in self.db.list_playlists():
            add_sidebar_item(playlist["name"], "custom", playlist["id"])


    def clear_browser_grid(self) -> None:
        while self.browser_grid.count():
            item = self.browser_grid.takeAt(0)
            widget = item.widget()

            if widget is not None:
                widget.deleteLater()

    def make_browser_card(self, title: str, subtitle: str, cover_path: str | None, callback) -> QFrame:
        card = QFrame()
        card.setObjectName("BrowserCard")
        card.setFixedSize(174, 224)
        card.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        cover = QLabel()
        cover.setObjectName("BrowserCover")
        cover.setFixedSize(146, 146)
        cover.setAlignment(Qt.AlignCenter)

        if cover_path and Path(cover_path).exists():
            pixmap = QPixmap(cover_path)

            if not pixmap.isNull():
                cover.setPixmap(
                    pixmap.scaled(
                        QSize(146, 146),
                        Qt.KeepAspectRatioByExpanding,
                        Qt.SmoothTransformation
                    )
                )
            else:
                cover.setText("♪")
        else:
            cover.setText("♪")

        title_label = QLabel(title)
        title_label.setObjectName("BrowserCardTitle")
        title_label.setWordWrap(True)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("MutedLabel")
        subtitle_label.setWordWrap(True)

        layout.addWidget(cover)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        layout.addStretch(1)

        def handle_click(event):
            callback()

        card.mousePressEvent = handle_click

        return card

    def album_groups(self) -> list[dict]:
        rows = self.db.conn.execute("""
            SELECT album, artist, cover_path
            FROM tracks
            ORDER BY album COLLATE NOCASE, artist COLLATE NOCASE
        """).fetchall()

        groups = {}

        for row in rows:
            album = (row["album"] or "Unknown Album").strip() or "Unknown Album"
            artist = (row["artist"] or "Unknown Artist").strip() or "Unknown Artist"
            key = album.lower()

            if key not in groups:
                groups[key] = {
                    "album": album,
                    "artist": artist,
                    "count": 0,
                    "cover_path": None,
                }

            groups[key]["count"] += 1

            if not groups[key]["cover_path"] and row["cover_path"]:
                groups[key]["cover_path"] = row["cover_path"]

        return sorted(groups.values(), key=lambda item: item["album"].lower())

    def artist_groups(self) -> list[dict]:
        rows = self.db.conn.execute("""
            SELECT artist, cover_path
            FROM tracks
            ORDER BY artist COLLATE NOCASE
        """).fetchall()

        groups = {}

        for row in rows:
            artist = (row["artist"] or "Unknown Artist").strip() or "Unknown Artist"
            key = artist.lower()

            if key not in groups:
                groups[key] = {
                    "artist": artist,
                    "count": 0,
                    "cover_path": None,
                }

            groups[key]["count"] += 1

            if not groups[key]["cover_path"] and row["cover_path"]:
                groups[key]["cover_path"] = row["cover_path"]

        return sorted(groups.values(), key=lambda item: item["artist"].lower())

    def show_album_browser(self) -> None:
        self.library_content_stack.setCurrentIndex(1)
        self.page_title.setText("Albums")
        self.page_subtitle.setText("Browse your collection by album.")
        self.browser_title.setText("Albums")
        self.browser_hint.setText("Click an album to view its tracks")
        self.clear_browser_grid()

        albums = self.album_groups()
        self.track_count_card.value_label.setText(str(len(albums)))

        columns = 5

        for index, album in enumerate(albums):
            card = self.make_browser_card(
                album["album"],
                f'{album["artist"]} • {album["count"]} tracks',
                album["cover_path"],
                lambda album_name=album["album"]: self.open_album(album_name)
            )
            self.browser_grid.addWidget(card, index // columns, index % columns)

    def show_artist_browser(self) -> None:
        self.library_content_stack.setCurrentIndex(1)
        self.page_title.setText("Artists")
        self.page_subtitle.setText("Browse your collection by artist.")
        self.browser_title.setText("Artists")
        self.browser_hint.setText("Click an artist to view their tracks")
        self.clear_browser_grid()

        artists = self.artist_groups()
        self.track_count_card.value_label.setText(str(len(artists)))

        columns = 5

        for index, artist in enumerate(artists):
            card = self.make_browser_card(
                artist["artist"],
                f'{artist["count"]} tracks',
                artist["cover_path"],
                lambda artist_name=artist["artist"]: self.open_artist(artist_name)
            )
            self.browser_grid.addWidget(card, index // columns, index % columns)

    def open_album(self, album_name: str) -> None:
        rows = self.db.conn.execute("""
            SELECT id, title, artist, album, year, path, cover_path, duration_seconds, created_at
            FROM tracks
            WHERE COALESCE(NULLIF(album, ''), 'Unknown Album') = ?
            ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE
        """, (album_name,)).fetchall()

        self.current_view_kind = "album_tracks"
        self.current_playlist_name = album_name

        self.load_library(
            rows,
            album_name,
            "Album view"
        )

    def open_artist(self, artist_name: str) -> None:
        rows = self.db.conn.execute("""
            SELECT id, title, artist, album, year, path, cover_path, duration_seconds, created_at
            FROM tracks
            WHERE COALESCE(NULLIF(artist, ''), 'Unknown Artist') = ?
            ORDER BY album COLLATE NOCASE, title COLLATE NOCASE
        """, (artist_name,)).fetchall()

        self.current_view_kind = "artist_tracks"
        self.current_playlist_name = artist_name

        self.load_library(
            rows,
            artist_name,
            "Artist view"
        )

    def refresh_artwork(self) -> None:
        updated = refresh_covers_for_library(self.db)
        self.refresh_current_view()

        if updated:
            QMessageBox.information(self, "Artwork refreshed", f"Updated artwork for {updated} tracks.")
        else:
            QMessageBox.information(
                self,
                "Artwork refreshed",
                "No new embedded artwork found. New YouTube downloads should show artwork if thumbnails are embedded."
            )


    def refresh_current_view(self) -> None:
        if self.current_view_kind == "albums":
            self.show_album_browser()
            self.write_app_status()
            return

        if self.current_view_kind == "artists":
            self.show_artist_browser()
            self.write_app_status()
            return

        if self.current_view_kind == "recent":
            self.load_library(
                self.db.list_recent_tracks(),
                "Recently Added",
                "The newest tracks imported into Music Vault."
            )
            return

        if self.current_view_kind == "downloaded":
            self.load_library(
                self.db.list_downloaded_tracks(),
                "Downloaded",
                "Tracks downloaded through Sync Center."
            )
            return

        if self.current_view_kind == "custom" and self.current_playlist_id is not None:
            self.load_library(
                self.db.get_playlist_tracks(self.current_playlist_id),
                self.current_playlist_name,
                "Custom playlist"
            )
            return

        self.current_view_kind = "library"
        self.current_playlist_id = None
        self.current_playlist_name = "Library"
        self.load_library(
            self.db.list_tracks(),
            "Library",
            "Your local music collection, synced and ready."
        )

    def on_playlist_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole) or {}
        kind = data.get("kind")
        playlist_id = data.get("id")
        name = data.get("name") or item.text()

        if kind == "new":
            self.create_playlist()
            return

        self.current_view_kind = kind or "library"
        self.current_playlist_id = playlist_id
        self.current_playlist_name = name

        self.pages.setCurrentIndex(0)

        if kind == "albums":
            self.show_album_browser()
            return

        if kind == "artists":
            self.show_artist_browser()
            return

        self.refresh_current_view()

    def create_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")

        if not ok:
            return

        name = name.strip()

        if not name:
            return

        try:
            playlist_id = self.db.create_playlist(name)
        except Exception as exc:
            QMessageBox.warning(self, "Playlist error", str(exc))
            return

        self.load_playlists()
        self.current_view_kind = "custom"
        self.current_playlist_id = playlist_id
        self.current_playlist_name = name
        self.refresh_current_view()

    def add_selected_to_playlist(self) -> None:
        track_id = self.selected_track_id()

        if track_id is None:
            QMessageBox.information(self, "Select a track", "Select a song first.")
            return

        playlists = self.db.list_playlists()
        names = [row["name"] for row in playlists]
        names.append("+ Create New Playlist")

        choice, ok = QInputDialog.getItem(
            self,
            "Add to Playlist",
            "Choose playlist:",
            names,
            0,
            False
        )

        if not ok or not choice:
            return

        if choice == "+ Create New Playlist":
            name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")

            if not ok or not name.strip():
                return

            playlist_id = self.db.create_playlist(name.strip())
            playlist_name = name.strip()
        else:
            playlist = next((row for row in playlists if row["name"] == choice), None)

            if playlist is None:
                return

            playlist_id = playlist["id"]
            playlist_name = playlist["name"]

        self.db.add_track_to_playlist(playlist_id, track_id)
        self.load_playlists()

        QMessageBox.information(self, "Added", f"Added selected song to {playlist_name}.")

    def remove_selected_from_current_playlist(self) -> None:
        if self.current_view_kind != "custom" or self.current_playlist_id is None:
            QMessageBox.information(self, "Playlist only", "Open a custom playlist first.")
            return

        track_id = self.selected_track_id()

        if track_id is None:
            QMessageBox.information(self, "Select a track", "Select a song first.")
            return

        self.db.remove_track_from_playlist(self.current_playlist_id, track_id)
        self.refresh_current_view()

    def import_music_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose music folder")

        if not folder:
            return

        count = import_folder(self.db, folder)
        self.refresh_current_view()

        QMessageBox.information(self, "Import complete", f"Imported or refreshed {count} audio files.")

    def choose_youtube_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose YouTube download folder")

        if folder:
            self.youtube_output.setText(folder)

    def open_youtube_output(self) -> None:
        folder = Path(self.youtube_output.text().strip())

        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))


    def log_youtube(self, message: str) -> None:
        message = sanitize_error_text(message)
        if hasattr(self, "youtube_log"):
            self.youtube_log.append(message)

        lower = message.lower()

        if "download" in lower:
            self.set_sync_status("Downloading")
        elif "skip" in lower or "archive" in lower:
            self.set_sync_status("Skipping")
        elif "fail" in lower or "error" in lower:
            self.set_sync_status("Issue")
        elif "import" in lower:
            self.set_sync_status("Importing")


    def sync_youtube_playlist(self) -> None:
        playlist_url = self.youtube_url.text().strip()
        output_dir = self.youtube_output.text().strip()

        if not output_dir:
            output_dir = self.config.get(
                "download_folder",
                str(default_downloads_dir())
            )
            self.youtube_output.setText(output_dir)

        if not playlist_url:
            QMessageBox.information(self, "Playlist required", "Paste your YouTube playlist URL first.")
            return

        if not self.youtube_confirm.isChecked():
            QMessageBox.information(
                self,
                "Authorization required",
                "Confirm this playlist contains music you own or have permission to download it."
            )
            return

        self.config["download_folder"] = str(Path(output_dir).resolve())

        if hasattr(self, "settings_quality"):
            self.config["audio_quality"] = self.settings_quality.currentText()
        else:
            self.config["audio_quality"] = str(self.config.get("audio_quality", "320"))

        self.save_config()
        self.update_sync_quality_label()

        self.youtube_sync_btn.setEnabled(False)
        self.youtube_sync_btn.setText("Syncing...")
        self.youtube_log.clear()

        self.sync_status_card.value_label.setText("Starting")
        self.sync_downloaded_card.value_label.setText("0")
        self.sync_skipped_card.value_label.setText("—")
        self.sync_failed_card.value_label.setText("0")

        self.sync_progress.setRange(0, 0)
        self.sync_progress.setFormat("Syncing...")

        self.log_youtube("Starting Music Vault sync.")
        self.log_youtube(f"Download folder: {self.config['download_folder']}")
        self.log_youtube(f"Audio quality: {self.config['audio_quality']} kbps")

        self.sync_worker = YouTubeSyncWorker(
            playlist_url,
            self.config["download_folder"],
            self.config["audio_quality"],
            frozenset(self.db.existing_youtube_video_ids()),
        )

        self.sync_worker.progress.connect(self.log_youtube)
        self.sync_worker.finished_ok.connect(self.youtube_sync_finished)
        self.sync_worker.start()


    def youtube_sync_finished(self, result: SyncResult) -> None:
        if not isinstance(result, SyncResult):
            result = SyncResult.failed_result("The sync worker returned an invalid result.")

        imported_count = 0
        for item in result.import_items:
            try:
                if import_file(
                    self.db,
                    item.path,
                    ImportSourceContext(
                        source_kind="youtube",
                        source_video_id=item.video_id,
                        source_upload_date=item.source_upload_date,
                    ),
                ):
                    imported_count += 1
                    result.successful_video_ids.add(item.video_id)
            except Exception as exc:
                result.add_failure(
                    SyncFailure(
                        item.video_id,
                        Path(item.path).stem,
                        sanitize_error_text(exc),
                        "import",
                    )
                )

        result.finish_imports(imported_count)
        for failure in result.failures:
            if not failure.video_id:
                continue
            self.db.record_sync_failure(
                playlist_id=result.playlist_id or "unknown",
                playlist_title=result.playlist_title,
                video_id=failure.video_id,
                title=failure.title,
                reason=failure.reason,
                error_category=failure.error_category,
                attempted_at=result.finished_at,
            )
        for video_id in result.successful_video_ids:
            self.db.resolve_sync_failure(video_id, result.finished_at)

        self.youtube_sync_btn.setEnabled(True)
        self.youtube_sync_btn.setText("Start Sync")

        self.sync_progress.setRange(0, 100)
        self.sync_progress.setValue(0 if result.status == "failed" else 100)
        values = sync_ui_values(result)
        self.sync_progress.setFormat(values["status"])
        self.sync_status_card.value_label.setText(values["status"])

        if hasattr(self, "refresh_current_view"):
            self.refresh_current_view()
        else:
            self.load_library()

        self.refresh_settings_status()

        self.sync_downloaded_card.value_label.setText(values["downloaded"])
        self.sync_skipped_card.value_label.setText(values["existing"])
        self.sync_failed_card.value_label.setText(values["failed"])

        self.log_youtube("")
        self.log_youtube("Sync summary:")
        self.log_youtube(f"Status: {values['status']}")
        self.log_youtube(f"Playlist: {result.playlist_title or 'Unavailable'}")
        self.log_youtube(f"New items: {result.new_item_count}")
        self.log_youtube(f"Downloaded: {result.downloaded_count}")
        self.log_youtube(f"Existing: {result.existing_count}")
        self.log_youtube(f"Imported/refreshed: {result.imported_count}")
        self.log_youtube(f"Failed: {result.failed_count}")
        for failure in result.failures:
            self.log_youtube(f"- {failure.title or failure.video_id or 'Sync'}: {failure.reason}")

        self.app_sync_status = result.to_status_dict()
        self.write_app_status()

        summary = (
            f"{values['status']}. Downloaded {result.downloaded_count}, "
            f"imported {result.imported_count}, failed {result.failed_count}."
        )
        if result.status == "complete":
            QMessageBox.information(self, "YouTube sync complete", summary)
        else:
            QMessageBox.warning(self, "YouTube sync result", summary)

    def selected_track_id(self) -> int | None:
        row = self.library_table.currentRow()

        if row < 0:
            return None

        item = self.library_table.item(row, 0)

        return int(item.data(Qt.UserRole)) if item else None

    def play_selected(self) -> None:
        track_id = self.selected_track_id()

        if track_id is None:
            return

        self.play_track_by_id(track_id)

    def visible_track_ids(self) -> list[int]:
        track_ids = []

        for row in self.visible_track_rows():
            item = self.library_table.item(row, 0)

            if item is not None:
                track_ids.append(int(item.data(Qt.UserRole)))

        return track_ids

    def capture_base_playback_context(self, track_id: int) -> None:
        track_ids = self.visible_track_ids()

        if track_id not in track_ids:
            track_ids.insert(0, track_id)

        self.base_playback_context = {
            "kind": self.current_view_kind,
            "playlist_id": self.current_playlist_id,
            "playlist_name": self.current_playlist_name,
            "track_ids": track_ids,
            "current_track_id": track_id,
        }

    def base_track_ids(self) -> list[int]:
        if self.base_playback_context:
            track_ids = self.base_playback_context.get("track_ids") or []

            if track_ids:
                return list(track_ids)

        selected_track_id = self.selected_track_id()

        if selected_track_id is None:
            return []

        self.capture_base_playback_context(selected_track_id)
        return list(self.base_playback_context["track_ids"])

    def play_track_by_id(
        self,
        track_id: int,
        capture_base_context: bool = True,
        show_missing_warning: bool = True,
    ) -> bool:
        track = self.db.get_track(track_id)

        if not track:
            return False

        path = Path(track["path"])

        if not path.exists():
            if show_missing_warning:
                QMessageBox.warning(
                    self,
                    "Missing file",
                    "This track file no longer exists. Use Settings > Remove Missing Tracks."
                )
            return False

        if capture_base_context:
            self.capture_base_playback_context(track_id)

        self.current_track_id = track_id
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

        artist = track["artist"] or "Unknown Artist"
        title = track["title"] or path.stem

        self.now_title.setText(title)
        self.now_artist.setText(artist)

        self.set_cover_art(track["cover_path"])
        self.write_app_status()
        return True

    def set_cover_art(self, cover_path: str | None) -> None:
        if cover_path and Path(cover_path).exists():
            pixmap = QPixmap(cover_path)

            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    QSize(78, 78),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation
                )
                self.cover_art.setPixmap(scaled)
                self.cover_art.setText("")
                return

        self.cover_art.setPixmap(QPixmap())
        self.cover_art.setText("♪")

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return

        if self.player.source().isEmpty():
            self.play_selected()
        else:
            self.player.play()



    def play_next(self) -> None:
        if self.play_next_from_manual_queue():
            return

        if self.shuffle_enabled:
            self.play_random_from_base_context()
            return

        self.play_next_from_base_context()

    def play_next_from_manual_queue(self) -> bool:
        while self.manual_queue:
            queued_track_id = self.manual_queue.pop(0)
            self.update_queue_label()
            self.write_app_status()

            if self.play_track_by_id(
                queued_track_id,
                capture_base_context=False,
                show_missing_warning=False,
            ):
                return True

        return False

    def play_next_from_base_context(self) -> bool:
        track_ids = self.base_track_ids()

        if not track_ids:
            return False

        context = self.base_playback_context or {}
        current_track_id = context.get("current_track_id")

        try:
            current_index = track_ids.index(current_track_id)
        except ValueError:
            current_index = -1

        if current_index + 1 < len(track_ids):
            candidates = track_ids[current_index + 1:]
        elif self.repeat_mode == "all":
            candidates = track_ids
        else:
            return False

        for track_id in candidates:
            if self.play_base_track_by_id(track_id):
                return True

        return False

    def play_base_track_by_id(self, track_id: int) -> bool:
        if not self.play_track_by_id(
            track_id,
            capture_base_context=False,
            show_missing_warning=False,
        ):
            return False

        if self.base_playback_context is None:
            self.capture_base_playback_context(track_id)
        else:
            self.base_playback_context["current_track_id"] = track_id

        return True

    def play_random_from_base_context(self) -> bool:
        track_ids = self.base_track_ids()

        if not track_ids:
            return False

        context = self.base_playback_context or {}
        current_track_id = context.get("current_track_id")
        choices = [track_id for track_id in track_ids if track_id != current_track_id]

        if not choices:
            choices = track_ids

        random.shuffle(choices)

        for track_id in choices:
            if self.play_base_track_by_id(track_id):
                return True

        return False

    def play_previous(self) -> None:
        track_ids = self.base_track_ids()

        if not track_ids:
            return

        context = self.base_playback_context or {}
        current_track_id = context.get("current_track_id")

        try:
            current_index = track_ids.index(current_track_id)
        except ValueError:
            current_index = 0

        if current_index > 0:
            candidates = list(reversed(track_ids[:current_index]))
        elif self.repeat_mode == "all":
            candidates = list(reversed(track_ids))
        else:
            return

        for track_id in candidates:
            if self.play_base_track_by_id(track_id):
                return



    def update_queue_label(self) -> None:
        if hasattr(self, "queue_label"):
            self.queue_label.setText(f"Queue: {len(self.manual_queue)}")

    def queue_selected_next(self) -> None:
        track_id = self.selected_track_id()

        if track_id is None:
            QMessageBox.information(self, "Select a track", "Select a song first.")
            return

        # Manual queue order is FIFO: first queued, first played.
        self.manual_queue.append(track_id)
        self.update_queue_label()
        self.write_app_status()

        track = self.db.get_track(track_id)
        title = "Selected song"
        artist = ""

        if track:
            title = track["title"] or Path(track["path"]).stem
            artist = track["artist"] or ""

        self.statusBar().showMessage(f"Queued next: {title}" + (f" — {artist}" if artist else ""), 3000)

    def open_song_context_menu(self, position) -> None:
        row = self.library_table.rowAt(position.y())

        if row < 0:
            return

        self.library_table.selectRow(row)

        menu = QMenu(self)

        play_action = menu.addAction("Play")
        play_next_action = menu.addAction("Play Next")
        add_playlist_action = menu.addAction("Add to Playlist")

        action = menu.exec(self.library_table.viewport().mapToGlobal(position))

        if action == play_action:
            self.play_selected()
        elif action == play_next_action:
            self.queue_selected_next()
        elif action == add_playlist_action:
            self.add_selected_to_playlist()

    def visible_track_rows(self) -> list[int]:
        return [
            row for row in range(self.library_table.rowCount())
            if not self.library_table.isRowHidden(row)
        ]

    def play_row(self, row: int) -> None:
        if row < 0 or row >= self.library_table.rowCount():
            return

        self.library_table.selectRow(row)
        self.play_selected()

    def play_random_visible(self) -> None:
        rows = self.visible_track_rows()

        if not rows:
            return

        current_row = self.library_table.currentRow()

        choices = [row for row in rows if row != current_row]

        if not choices:
            choices = rows

        self.play_row(random.choice(choices))


    def on_media_status_changed(self, status) -> None:
        if status != QMediaPlayer.EndOfMedia:
            return

        if self.repeat_mode == "one":
            self.player.setPosition(0)
            self.player.play()
            return

        # Queued songs should play next even if Auto is off.
        if self.manual_queue:
            if self.play_next_from_manual_queue():
                return

        if self.shuffle_enabled:
            self.play_random_from_base_context()
        elif self.autoplay_enabled:
            self.play_next_from_base_context()
        elif self.repeat_mode == "all":
            self.play_next_from_base_context()

    def on_media_error(self, _error, _error_string: str = "") -> None:
        if self._handling_media_error:
            return
        self._handling_media_error = True
        track = self.db.get_track(self.current_track_id) if self.current_track_id else None
        title = track["title"] if track else None
        self.statusBar().showMessage(playback_error_message(title), 7000)
        QTimer.singleShot(0, self.continue_after_media_error)

    def continue_after_media_error(self) -> None:
        """Skip an unplayable item without changing queue/base-context ordering."""
        self._handling_media_error = False
        if self.play_next_from_manual_queue():
            return
        if self.shuffle_enabled:
            self.play_random_from_base_context()
        elif self.autoplay_enabled or self.repeat_mode == "all":
            self.play_next_from_base_context()


    def toggle_autoplay(self) -> None:
        self.autoplay_enabled = not self.autoplay_enabled

        if self.autoplay_enabled:
            self.shuffle_enabled = False

        self.update_playback_mode_buttons()


    def toggle_shuffle(self) -> None:
        self.shuffle_enabled = not self.shuffle_enabled

        if self.shuffle_enabled:
            self.autoplay_enabled = False

        self.update_playback_mode_buttons()

    def cycle_repeat(self) -> None:
        if self.repeat_mode == "off":
            self.repeat_mode = "all"
        elif self.repeat_mode == "all":
            self.repeat_mode = "one"
        else:
            self.repeat_mode = "off"

        self.update_playback_mode_buttons()


    def update_playback_mode_buttons(self) -> None:
        if self.autoplay_enabled:
            self.autoplay_btn.setText("Auto On")
            self.autoplay_btn.setObjectName("ModeButtonActive")
        else:
            self.autoplay_btn.setText("Auto Off")
            self.autoplay_btn.setObjectName("ModeButton")

        if self.shuffle_enabled:
            self.shuffle_btn.setText("Shuffle On")
            self.shuffle_btn.setObjectName("ModeButtonActive")
        else:
            self.shuffle_btn.setText("Shuffle")
            self.shuffle_btn.setObjectName("ModeButton")

        if self.repeat_mode == "off":
            self.repeat_btn.setText("Repeat Off")
            self.repeat_btn.setObjectName("ModeButton")
        elif self.repeat_mode == "all":
            self.repeat_btn.setText("Repeat All")
            self.repeat_btn.setObjectName("ModeButtonActive")
        else:
            self.repeat_btn.setText("Repeat One")
            self.repeat_btn.setObjectName("ModeButtonActive")

        self.update_queue_label()

        for btn in [self.autoplay_btn, self.shuffle_btn, self.repeat_btn]:
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()

        self.write_app_status()

    def on_playback_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlayingState:
            self.play_btn.setText("❚❚")
        else:
            self.play_btn.setText("▶")

        self.write_app_status()

    def on_position_changed(self, position: int) -> None:
        if not self.is_seeking:
            self.progress_slider.setValue(position)

        self.elapsed_label.setText(self.format_time(position))

    def on_duration_changed(self, duration: int) -> None:
        self.progress_slider.setRange(0, duration)
        self.duration_label.setText(self.format_time(duration))

    def on_slider_pressed(self) -> None:
        self.is_seeking = True

    def on_slider_released(self) -> None:
        self.is_seeking = False
        self.player.setPosition(self.progress_slider.value())

    def format_time(self, milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes}:{seconds:02d}"

    def enrich_selected(self) -> None:
        track_id = self.selected_track_id()

        if track_id is None:
            QMessageBox.information(self, "Select a track", "Select a track first.")
            return

        track = self.db.get_track(track_id)

        if not track:
            return

        title = track["title"] or Path(track["path"]).stem
        artist = track["artist"]

        try:
            candidates = search_recording(title, artist)
        except Exception as exc:
            QMessageBox.warning(self, "Metadata lookup failed", str(exc))
            return

        if not candidates:
            QMessageBox.information(self, "No match", "No MusicBrainz match found.")
            return

        best = max(candidates, key=lambda candidate: candidate.score)
        confidence = (
            "\n\nWarning: this is an uncertain match. Review it carefully."
            if best.score < 80
            else ""
        )
        details = (
            f"Title: {best.title or 'Unknown'}\n"
            f"Artist: {best.artist or 'Unknown'}\n"
            f"Release: {best.album or 'Unknown'}\n"
            f"Year: {best.year or 'Unknown'}\n"
            "Provider: MusicBrainz\n"
            f"Score: {best.score}"
            f"{confidence}\n\nApply this candidate?"
        )
        if QMessageBox.question(
            self,
            "Confirm metadata candidate",
            details,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        cover_path = None

        if best.release_id:
            try:
                cover_path = download_front_cover(best.release_id)
            except Exception:
                cover_path = None

        updates = {
            "title": best.title,
            "artist": best.artist,
            "album": best.album,
            "year": best.year,
            "musicbrainz_recording_id": best.recording_id,
            "musicbrainz_release_id": best.release_id,
            "cover_path": cover_path,
        }
        self.db.update_track_metadata(
            track_id,
            **{key: value for key, value in updates.items() if value not in (None, "")},
        )

        self.refresh_current_view()

        QMessageBox.information(
            self,
            "Metadata updated",
            f"Applied best match with confidence score {best.score}."
        )

    def filter_library(self, text: str) -> None:
        needle = text.lower().strip()

        for row in range(self.library_table.rowCount()):
            row_text = " ".join(
                self.library_table.item(row, col).text().lower()
                for col in range(self.library_table.columnCount())
                if self.library_table.item(row, col)
            )
            self.library_table.setRowHidden(row, needle not in row_text)


    def remove_missing_tracks(self) -> None:
        rows = self.db.conn.execute("SELECT id, path FROM tracks").fetchall()
        missing = [(row["id"],) for row in rows if not Path(row["path"]).exists()]

        if not missing:
            QMessageBox.information(self, "Library clean", "No missing tracks found.")
            return

        confirm = QMessageBox.question(
            self,
            "Remove missing tracks?",
            f"Remove {len(missing)} missing tracks from the Music Vault list?"
        )

        if confirm != QMessageBox.Yes:
            return

        self.db.conn.executemany("DELETE FROM playlist_tracks WHERE track_id=?", missing)
        self.db.conn.executemany("DELETE FROM tracks WHERE id=?", missing)
        self.db.conn.commit()

        self.refresh_current_view()
        self.write_app_status()

        QMessageBox.information(self, "Cleaned", f"Removed {len(missing)} missing tracks.")

    def audio_device_key(self, device) -> str:
        try:
            return bytes(device.id()).decode("utf-8", errors="ignore")
        except Exception:
            try:
                return str(device.description())
            except Exception:
                return "unknown"

    def use_system_default_audio_output(self) -> None:
        try:
            device = QMediaDevices.defaultAudioOutput()
            key = self.audio_device_key(device)

            if key and key != self.current_audio_device_key:
                volume = self.audio_output.volume()
                self.audio_output.setDevice(device)
                self.audio_output.setVolume(volume)
                self.current_audio_device_key = key

                try:
                    description = device.description()
                except Exception:
                    description = "System Default"

                if hasattr(self, "ffmpeg_status"):
                    current_text = self.ffmpeg_status.text()
                    if "Audio Output:" not in current_text:
                        self.ffmpeg_status.setText(current_text + f"\\nAudio Output: Following system default ({description})")

        except Exception:
            pass



    def choose_default_download_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose default download folder",
            self.settings_download_folder.text().strip()
        )

        if folder:
            self.settings_download_folder.setText(folder)

    def save_settings_from_ui(self) -> None:
        data_dir().mkdir(parents=True, exist_ok=True)

        api_key = self.settings_api_key.text().strip()

        if api_key:
            self.api_key_path().write_text(api_key, encoding="utf-8")

        download_folder = self.settings_download_folder.text().strip()

        if not download_folder:
            download_folder = str(default_downloads_dir())

        Path(download_folder).mkdir(parents=True, exist_ok=True)

        self.config["download_folder"] = str(Path(download_folder).resolve())
        self.config["audio_quality"] = self.settings_quality.currentText()
        self.save_config()

        if hasattr(self, "youtube_output"):
            self.youtube_output.setText(self.config["download_folder"])

        self.refresh_settings_status()
        self.write_app_status()

        QMessageBox.information(self, "Settings saved", "Music Vault settings were saved.")

    def open_data_folder(self) -> None:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def open_default_download_folder(self) -> None:
        folder = Path(
            self.settings_download_folder.text().strip()
            or self.config.get("download_folder")
            or default_downloads_dir()
        )
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def clear_failed_downloads(self) -> None:
        if self.db.unresolved_failure_count() == 0:
            QMessageBox.information(self, "Failure history", "There are no unresolved failures to clear.")
            return

        confirm = QMessageBox.question(
            self,
            "Clear failure history?",
            "Clear structured synchronization failure history? This does not delete any music."
        )

        if confirm != QMessageBox.Yes:
            return

        self.db.clear_failure_history()
        QMessageBox.information(self, "Failure history cleared", "Synchronization failure history was cleared.")
        self.refresh_settings_status()


    def refresh_settings_status(self) -> None:
        api_ready = bool(self.read_saved_api_key())

        if api_ready:
            self.api_key_status.setText("YouTube API Key: Found")
            self.api_status_card.value_label.setText("Ready")
        else:
            self.api_key_status.setText("YouTube API Key: Missing")
            self.api_status_card.value_label.setText("Missing")

        ffmpeg_bin = self.find_ffmpeg_bin()

        if ffmpeg_bin:
            self.ffmpeg_status.setText(f"FFmpeg: Found at {ffmpeg_bin}")
        else:
            self.ffmpeg_status.setText("FFmpeg: Not found. Downloads may fail during MP3 conversion.")

        download_folder = Path(
            self.config.get(
                "download_folder",
                str(default_downloads_dir())
            )
        )

        if download_folder.exists():
            self.download_folder_card.value_label.setText("Ready")
        else:
            self.download_folder_card.value_label.setText("Not Made")

        db_path = getattr(self.db, "db_path", database_path())
        self.db_status.setText(f"Database: {Path(db_path).resolve()}")

        failed_count = self.db.unresolved_failure_count()

        config_lines = [
            f"Config: {self.config_file_path().resolve()}",
            f"Download Folder: {download_folder.resolve()}",
            f"Audio Quality: {self.config.get('audio_quality', '320')} kbps",
            f"Unresolved Sync Failures: {failed_count}",
        ]

        self.config_status.setText(chr(10).join(config_lines))

        if hasattr(self, "app_status_line"):
            self.app_status_line.setText(
                f"App Status: {app_status_path()}"
            )

        if hasattr(self, "settings_download_folder"):
            self.settings_download_folder.setText(str(download_folder.resolve()))

        if hasattr(self, "settings_quality"):
            quality = str(self.config.get("audio_quality", "320"))

            if quality in ["192", "256", "320"]:
                self.settings_quality.setCurrentText(quality)

        if hasattr(self, "settings_api_key") and not self.settings_api_key.text().strip():
            self.settings_api_key.setText(self.read_saved_api_key())

    def find_ffmpeg_bin(self) -> str | None:
        tools_root = Path.home() / "Documents" / "MusicVaultTools" / "ffmpeg"

        if tools_root.exists():
            for bin_dir in tools_root.glob("*/bin"):
                if (bin_dir / "ffmpeg.exe").exists():
                    return str(bin_dir)

        return None


def main() -> None:
    app = QApplication(sys.argv)
    window = MusicVaultWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
