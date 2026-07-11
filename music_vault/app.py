from __future__ import annotations

import sys
import random
import json
import math
import os
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QThread, Signal, QSize, QTimer, QRectF
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPixmap,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
)
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
    QAbstractItemView,
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
    QSizePolicy,
    QButtonGroup,
)

from music_vault.core.db import MusicVaultDB
from music_vault.core.app_status import write_app_status as export_app_status
from music_vault.core.importer import (
    ImportSourceContext,
    import_file,
    import_folder,
    refresh_covers_for_library,
)
from music_vault.core.library_browser import (
    AlbumSummary,
    ArtistSummary,
    BrowserInvalidationReason,
    BrowserKind,
    BrowserSummaryCache,
    browser_revision,
    load_album_summaries,
    load_artist_summaries,
    query_album_tracks,
    query_artist_tracks,
)
from music_vault.core.playback_errors import playback_error_message
from music_vault.core.playback_state import (
    DEFAULT_VOLUME_PERCENT,
    build_track_row_map,
    config_for_persistence,
    locate_track_row,
    normalize_volume_percent,
)
from music_vault.core.paths import (
    app_status_path,
    artist_images_dir,
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
from music_vault.metadata.service import MetadataChangeResult, MetadataService
from music_vault.metadata.artist_images import (
    ArtistIdentity,
    ArtistImageCache,
    ArtistImageResult,
    ArtistImageService,
    ArtistImageStatus,
    create_artist_image_provider,
    is_safe_artist_source_url,
)
from music_vault.ui.components import (
    ElidedLabel,
    EmptyState,
    IconButton,
    OverflowActionButton,
    SearchField,
)
from music_vault.ui.browser_loader import BrowserSummaryLoader
from music_vault.ui.icons import render_icon_pixmap, ui_icon
from music_vault.ui.media_grid import (
    MediaFilterProxyModel,
    MediaGridModel,
    MediaGridState,
    MediaGridView,
    MediaImageState,
    MediaItem,
    MediaKind,
)
from music_vault.ui.metadata_editor import MetadataEditorDialog
from music_vault.ui.metadata_remediation import MetadataRemediationDialog
from music_vault.ui.review import schedule_ui_review
from music_vault.ui.theme import COLORS, application_stylesheet, apply_dark_title_bar, repolish
from music_vault.ui.thumbnail_cache import ThumbnailCache, make_thumbnail_key


NOW_PLAYING_ROLE = int(Qt.UserRole) + 1
VOLUME_SAVE_DEBOUNCE_MS = 500


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
        self.setMinimumSize(1100, 720)

        self.config = self.load_config()
        self.volume_percent = normalize_volume_percent(
            self.config.get("volume_percent"),
            DEFAULT_VOLUME_PERCENT,
        )
        self.config["volume_percent"] = self.volume_percent
        self._pending_volume_percent: int | None = None
        self.db = MusicVaultDB(
            youtube_download_root=self.config.get("download_folder"),
            legacy_failure_file=youtube_failed_ids_path(),
        )
        self.metadata_service = MetadataService(self.db)
        self.artist_image_cache = ArtistImageCache()
        self.artist_image_service = ArtistImageService(
            create_artist_image_provider(),
            self.artist_image_cache,
            parent=self,
        )
        self._pending_artist_image_keys: set[str] = set()
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
        self.track_row_map: dict[int, int] = {}
        self._playing_row: int | None = None
        self._styled_now_playing_track_id: int | None = None
        self._dark_title_bar_applied = False
        self._dark_title_bar_attempted = False
        self.browser_summary_cache = BrowserSummaryCache()
        self._detail_browser_context = None
        self._metadata_editor: MetadataEditorDialog | None = None
        self._metadata_remediation_dialog: MetadataRemediationDialog | None = None
        self.browser_summary_loader = BrowserSummaryLoader(self)
        self.browser_summary_loader.loaded.connect(self._browser_summaries_loaded)
        self.browser_summary_loader.failed.connect(self._browser_summaries_failed)
        self.thumbnail_cache = ThumbnailCache(parent=self)
        self._browser_summary_maps: dict[str, dict[str, AlbumSummary | ArtistSummary]] = {
            "albums": {},
            "artists": {},
        }
        self._browser_model_revisions: dict[str, object | None] = {
            "albums": None,
            "artists": None,
        }
        self._browser_scroll_positions = {"albums": 0, "artists": 0}
        self._active_browser_kind: str | None = None

        self.app_sync_status: dict | None = None

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(self.volume_percent / 100.0)

        self.volume_save_timer = QTimer(self)
        self.volume_save_timer.setSingleShot(True)
        self.volume_save_timer.setInterval(VOLUME_SAVE_DEBOUNCE_MS)
        self.volume_save_timer.timeout.connect(self.flush_pending_volume_save)

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
        self.update_playback_mode_buttons()
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
            "volume_percent": DEFAULT_VOLUME_PERCENT,
            "artist_image_fetch_enabled": False,
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

        config["volume_percent"] = normalize_volume_percent(
            config.get("volume_percent"),
            DEFAULT_VOLUME_PERCENT,
        )
        # Only the JSON boolean true opts in. Strings and numeric values must
        # never silently enable an external artist-name lookup.
        config["artist_image_fetch_enabled"] = (
            config.get("artist_image_fetch_enabled") is True
        )
        return config

    def save_config(self) -> None:
        path = self.config_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(config_for_persistence(self.config), indent=2),
            encoding="utf-8",
        )

    def initialize_volume_controls(self) -> int:
        volume = normalize_volume_percent(
            self.config.get("volume_percent"),
            DEFAULT_VOLUME_PERCENT,
        )
        self.volume_percent = volume
        self.config["volume_percent"] = volume

        previous_signal_state = self.volume_slider.blockSignals(True)
        try:
            self.volume_slider.setValue(volume)
        finally:
            self.volume_slider.blockSignals(previous_signal_state)

        self.audio_output.setVolume(volume / 100.0)
        return volume

    def on_volume_changed(self, value: int) -> None:
        volume = normalize_volume_percent(value, self.volume_percent)
        self.volume_percent = volume
        self.config["volume_percent"] = volume
        self.audio_output.setVolume(volume / 100.0)
        self.update_volume_icon()
        self._pending_volume_percent = volume
        self.volume_save_timer.start()

    def update_volume_icon(self) -> None:
        if not hasattr(self, "volume_icon"):
            return
        if self.volume_percent <= 0:
            icon_name = "volume-muted"
        elif self.volume_percent < 45:
            icon_name = "volume-low"
        else:
            icon_name = "volume"
        self.volume_icon.setPixmap(
            render_icon_pixmap(icon_name, 18, COLORS["text_secondary"])
        )

    def flush_pending_volume_save(self) -> bool:
        if self._pending_volume_percent is None:
            return False

        self.volume_save_timer.stop()
        self.config["volume_percent"] = self._pending_volume_percent
        self.save_config()
        self._pending_volume_percent = None
        return True

    def api_key_path(self) -> Path:
        return youtube_api_key_path()

    def read_saved_api_key(self) -> str:
        if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() == "1":
            return ""
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
        root.setObjectName("AppRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        self.sidebar = self.build_sidebar()
        self.pages = QStackedWidget()

        self.library_page = self.build_library_page()
        self.sync_page = self.build_sync_page()
        self.settings_page = self.build_settings_page()

        self.pages.addWidget(self.library_page)
        self.pages.addWidget(self.sync_page)
        self.pages.addWidget(self.settings_page)
        self.pages.currentChanged.connect(self.update_sidebar_navigation_state)

        main_shell = QFrame()
        main_shell.setObjectName("MainShell")
        main_layout = QVBoxLayout(main_shell)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        main_layout.addWidget(self.pages, 1)
        self.player_bar = self.build_player_bar()
        main_layout.addWidget(self.player_bar)

        root_layout.addWidget(self.sidebar)
        root_layout.addWidget(main_shell, 1)

        self.setCentralWidget(root)
        self.apply_styles()
        self.update_sidebar_navigation_state()



    def build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(8)

        brand_row = QHBoxLayout()
        logo = QLabel()
        logo.setObjectName("LogoBadge")
        logo.setFixedSize(42, 42)
        logo.setAlignment(Qt.AlignCenter)
        logo.setPixmap(render_icon_pixmap("music-note", 24, COLORS["accent_ink"]))

        brand_col = QVBoxLayout()
        brand = QLabel("Music Vault")
        brand.setObjectName("Brand")
        subtitle = QLabel("Personal player")
        subtitle.setObjectName("MutedLabel")
        brand_col.addWidget(brand)
        brand_col.addWidget(subtitle)

        brand_row.addWidget(logo)
        brand_row.addLayout(brand_col, 1)

        self.library_btn = self.sidebar_button("Library", 0, "library")
        self.sync_btn_nav = self.sidebar_button("Sync Center", 1, "sync")
        self.settings_btn = self.sidebar_button("Settings", 2, "settings")
        self.sidebar_button_group = QButtonGroup(self)
        self.sidebar_button_group.setExclusive(True)
        for index, button in enumerate(
            (self.library_btn, self.sync_btn_nav, self.settings_btn)
        ):
            self.sidebar_button_group.addButton(button, index)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)

        section = QLabel("PLAYLISTS")
        section.setObjectName("SectionLabel")

        self.playlists = QListWidget()
        self.playlists.setObjectName("PlaylistList")
        self.playlists.setTextElideMode(Qt.ElideRight)
        self.playlists.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.playlists.setAccessibleName("Library views and playlists")
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

    def sidebar_button(self, text: str, page_index: int, icon_name: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("SidebarButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setCheckable(True)
        btn.setIcon(ui_icon(icon_name, 20))
        btn.setIconSize(QSize(20, 20))
        btn.setToolTip(text)
        btn.setAccessibleName(text)
        btn.clicked.connect(lambda: self.pages.setCurrentIndex(page_index))
        return btn

    def update_sidebar_navigation_state(self, _index: int | None = None) -> None:
        if not hasattr(self, "pages"):
            return
        current = self.pages.currentIndex()
        for index, button in enumerate(
            (self.library_btn, self.sync_btn_nav, self.settings_btn)
        ):
            button.setChecked(index == current)
            repolish(button)

    def make_action_button(
        self,
        text: str,
        icon_name: str,
        callback,
        *,
        object_name: str = "SoftButton",
        tooltip: str | None = None,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        if object_name == "PrimaryButton":
            normal_icon_color = active_icon_color = COLORS["accent_ink"]
        elif object_name == "DangerButton":
            normal_icon_color = COLORS["danger"]
            active_icon_color = COLORS["danger_hover"]
        else:
            normal_icon_color = COLORS["text_secondary"]
            active_icon_color = COLORS["text_primary"]
        button.setIcon(
            ui_icon(
                icon_name,
                18,
                color=normal_icon_color,
                active_color=active_icon_color,
            )
        )
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip or text)
        button.setAccessibleName(text)
        button.clicked.connect(callback)
        return button




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

        title_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        self.page_title = QLabel("Library")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel("Your local music collection, synced and ready.")
        self.page_subtitle.setObjectName("MutedLabel")
        title_col.addWidget(self.page_title)
        title_col.addWidget(self.page_subtitle)
        title_row.addLayout(title_col, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.import_btn = self.make_action_button(
            "Import Folder",
            "import",
            self.import_music_folder,
            object_name="PrimaryButton",
            tooltip="Import a folder of local music",
        )
        self.create_playlist_btn = self.make_action_button(
            "New Playlist", "add", self.create_playlist
        )
        self.add_playlist_btn = self.make_action_button(
            "Add to Playlist", "playlists", self.add_selected_to_playlist
        )
        self.queue_next_btn = self.make_action_button(
            "Queue Next", "queue-next", self.queue_selected_next
        )
        self.library_overflow = OverflowActionButton(self)
        self.library_overflow.setToolTip("More library actions")
        self.library_overflow.setAccessibleName("More library actions")
        self.library_overflow.add_action(
            "Remove From Playlist",
            "remove",
            self.remove_selected_from_current_playlist,
            destructive=True,
        )
        self.edit_metadata_action = self.library_overflow.add_action(
            "Edit Metadata", "metadata", self.open_metadata_editor
        )
        self.edit_metadata_action.setEnabled(False)
        self.library_overflow.add_action(
            "Review Library Metadata", "metadata", self.open_metadata_remediation
        )
        self.library_overflow.add_action(
            "Remove Missing", "warning", self.remove_missing_tracks,
            destructive=True,
        )
        self.library_overflow.add_action(
            "Refresh Art", "refresh", self.refresh_artwork
        )

        action_row.addWidget(self.import_btn)
        action_row.addWidget(self.create_playlist_btn)
        action_row.addWidget(self.add_playlist_btn)
        action_row.addWidget(self.queue_next_btn)
        action_row.addWidget(self.library_overflow)
        action_row.addStretch(1)

        self.search_box = SearchField(
            placeholder="Search songs, artists, albums...",
            parent=page,
        )
        self.search_box.textChanged.connect(self.filter_library)
        self.search_box.setObjectName("SearchBox")
        self.search_box.setAccessibleName("Search the current music view")

        hero_layout.addLayout(title_row)
        hero_layout.addLayout(action_row)
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
        self.library_table.itemSelectionChanged.connect(self.update_metadata_action_state)
        self.library_table.verticalHeader().setVisible(False)
        self.library_table.setAlternatingRowColors(False)
        self.library_table.setShowGrid(False)
        self.library_table.setWordWrap(False)
        self.library_table.setTextElideMode(Qt.ElideRight)
        self.library_table.setFocusPolicy(Qt.StrongFocus)
        self.library_table.setAccessibleName("Music library tracks")
        self.library_table.horizontalHeader().setStretchLastSection(False)
        self.library_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.library_table.horizontalHeader().resizeSection(3, 68)
        self.library_table.setColumnHidden(4, True)

        self.library_empty_state = EmptyState(
            "library",
            "Your library is ready for music",
            "Import a folder to begin building your local collection.",
            parent=table_card,
        )
        self.search_empty_state = EmptyState(
            "search",
            "No matching tracks",
            "Try a shorter title, artist, or album search.",
            parent=table_card,
        )
        self.library_body_stack = QStackedWidget()
        self.library_body_stack.setObjectName("LibraryBodyStack")
        self.library_body_stack.addWidget(self.library_table)
        self.library_body_stack.addWidget(self.library_empty_state)
        self.library_body_stack.addWidget(self.search_empty_state)

        table_layout.addLayout(table_header)
        table_layout.addWidget(self.library_body_stack, 1)

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

        self.browser_action_btn = self.make_action_button(
            "Enable Artist Photos",
            "artists",
            self.confirm_enable_artist_photos,
        )
        self.browser_action_btn.setVisible(False)
        browser_header.insertWidget(2, self.browser_action_btn)

        self.album_browser_model = MediaGridModel(parent=browser_page)
        self.artist_browser_model = MediaGridModel(parent=browser_page)
        self.album_browser_proxy = MediaFilterProxyModel(browser_page)
        self.artist_browser_proxy = MediaFilterProxyModel(browser_page)
        self.album_browser_proxy.setSourceModel(self.album_browser_model)
        self.artist_browser_proxy.setSourceModel(self.artist_browser_model)
        self.album_browser_model.bind_thumbnail_cache(self.thumbnail_cache)
        self.artist_browser_model.bind_thumbnail_cache(self.thumbnail_cache)

        self.browser_view = MediaGridView(browser_page)
        self.browser_view.item_opened.connect(self.open_browser_item)
        self.browser_view.item_context_requested.connect(self.show_browser_context_menu)
        self.browser_view.visible_items_changed.connect(self.load_visible_browser_images)

        browser_layout.addLayout(browser_header)
        browser_layout.addWidget(self.browser_view, 1)

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
        self.set_sync_visual_state("idle")

    def set_sync_status(self, status: str) -> None:
        if hasattr(self, "sync_status_card"):
            self.sync_status_card.value_label.setText(status)

        if hasattr(self, "sync_progress") and self.sync_progress.maximum() != 0:
            self.sync_progress.setFormat(status)

    def set_sync_visual_state(self, state: str) -> None:
        normalized = state if state in {
            "idle", "syncing", "complete", "complete_with_issues", "failed"
        } else "idle"
        for widget_name in (
            "sync_status_card",
            "sync_failed_card",
            "sync_progress",
            "youtube_log",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setProperty("syncState", normalized)
                repolish(widget)

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
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        sync_content = QWidget()
        sync_content.setObjectName("SyncContent")
        layout = QVBoxLayout(sync_content)
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
        self.youtube_url.setAccessibleName("Authorized YouTube playlist URL")

        output_row = QHBoxLayout()
        self.youtube_output = QLineEdit(
            self.config.get(
                "download_folder",
                str(default_downloads_dir())
            )
        )
        self.youtube_output.setObjectName("SearchBox")
        self.youtube_output.setAccessibleName("YouTube download folder")

        choose_output = self.make_action_button(
            "Choose Folder", "folder", self.choose_youtube_output
        )
        open_output = self.make_action_button(
            "Open Downloads", "downloaded", self.open_youtube_output
        )

        output_row.addWidget(self.youtube_output, 1)
        output_row.addWidget(choose_output)
        output_row.addWidget(open_output)

        self.youtube_confirm = QCheckBox("I own this music or have permission to download it.")
        self.youtube_confirm.setObjectName("PermissionCheck")

        action_row = QHBoxLayout()

        self.youtube_sync_btn = QPushButton("Start Sync")
        self.youtube_sync_btn.setObjectName("PrimaryButton")
        self.youtube_sync_btn.setIcon(
            ui_icon(
                "sync",
                18,
                color=COLORS["accent_ink"],
                active_color=COLORS["accent_ink"],
            )
        )
        self.youtube_sync_btn.setToolTip("Start an authorized playlist sync")
        self.youtube_sync_btn.setAccessibleName("Start authorized playlist sync")
        self.youtube_sync_btn.clicked.connect(self.sync_youtube_playlist)

        clear_log_btn = self.make_action_button(
            "Clear Log", "remove", self.clear_sync_log
        )

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
        self.youtube_log.setPlaceholderText(
            "No sync activity yet. Authorized sync progress will appear here."
        )
        self.youtube_log.setAccessibleName("YouTube synchronization activity log")
        self.youtube_log.setMinimumHeight(120)

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

        self.sync_scroll = QScrollArea()
        self.sync_scroll.setObjectName("SyncScroll")
        self.sync_scroll.setWidgetResizable(True)
        self.sync_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sync_scroll.setWidget(sync_content)
        page_layout.addWidget(self.sync_scroll)

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
        self.settings_api_key.setAccessibleName("YouTube API key")
        self.settings_api_key.setText(self.read_saved_api_key())

        folder_label = QLabel("Default Download Folder")
        folder_label.setObjectName("MutedLabel")

        folder_row = QHBoxLayout()
        self.settings_download_folder = QLineEdit()
        self.settings_download_folder.setObjectName("SearchBox")
        self.settings_download_folder.setAccessibleName("Default download folder")
        self.settings_download_folder.setText(
            self.config.get("download_folder", str(default_downloads_dir()))
        )

        choose_folder_btn = self.make_action_button(
            "Choose", "folder", self.choose_default_download_folder
        )
        open_downloads_btn = self.make_action_button(
            "Open Downloads", "downloaded", self.open_default_download_folder
        )

        folder_row.addWidget(self.settings_download_folder, 1)
        folder_row.addWidget(choose_folder_btn)
        folder_row.addWidget(open_downloads_btn)

        quality_label = QLabel("Audio Quality")
        quality_label.setObjectName("MutedLabel")

        self.settings_quality = QComboBox()
        self.settings_quality.setObjectName("QualityCombo")
        self.settings_quality.setAccessibleName("Download audio quality")
        self.settings_quality.addItems(["192", "256", "320"])

        quality = str(self.config.get("audio_quality", "320"))

        if quality in ["192", "256", "320"]:
            self.settings_quality.setCurrentText(quality)
        else:
            self.settings_quality.setCurrentText("320")

        save_btn = QPushButton("Save Settings")
        save_btn.setObjectName("PrimaryButton")
        save_btn.setIcon(
            ui_icon(
                "settings",
                18,
                color=COLORS["accent_ink"],
                active_color=COLORS["accent_ink"],
            )
        )
        save_btn.setToolTip("Save Music Vault settings")
        save_btn.setAccessibleName("Save Music Vault settings")
        save_btn.clicked.connect(self.save_settings_from_ui)

        artist_images_title = QLabel("Artist Photos")
        artist_images_title.setObjectName("CardTitle")
        artist_images_description = QLabel(
            "Optional public metadata lookup for visible artists. Cached photos "
            "remain local and no provider API key is required."
        )
        artist_images_description.setObjectName("MutedLabel")
        artist_images_description.setWordWrap(True)

        self.settings_artist_images_enabled = QCheckBox(
            "Enable external artist-photo fetching"
        )
        self.settings_artist_images_enabled.setAccessibleName(
            "Enable external artist-photo fetching"
        )
        self.settings_artist_images_enabled.setChecked(
            self.config.get("artist_image_fetch_enabled") is True
        )
        self.settings_artist_images_enabled.clicked.connect(
            self.on_artist_image_setting_clicked
        )

        artist_images_row = QHBoxLayout()
        clear_artist_images_btn = self.make_action_button(
            "Clear Artist Photos",
            "remove",
            self.clear_artist_image_cache,
            object_name="DangerButton",
        )
        open_artist_images_btn = self.make_action_button(
            "Open Artist Cache",
            "folder",
            self.open_artist_image_cache_folder,
        )
        artist_images_row.addWidget(clear_artist_images_btn)
        artist_images_row.addWidget(open_artist_images_btn)
        artist_images_row.addStretch(1)

        self.artist_images_status = QLabel()
        self.artist_images_status.setObjectName("StatusLine")
        self.artist_images_status.setWordWrap(True)

        maintenance_title = QLabel("Maintenance")
        maintenance_title.setObjectName("CardTitle")

        maintenance_row = QHBoxLayout()

        open_data_btn = self.make_action_button(
            "Open Data Folder", "folder", self.open_data_folder
        )
        clear_failed_btn = self.make_action_button(
            "Clear Failure History", "remove", self.clear_failed_downloads,
            object_name="DangerButton",
        )
        refresh_btn = self.make_action_button(
            "Refresh Status", "refresh", self.refresh_settings_status
        )
        clean_btn = self.make_action_button(
            "Remove Missing Tracks", "warning", self.remove_missing_tracks,
            object_name="DangerButton",
        )

        maintenance_row.addWidget(open_data_btn)
        maintenance_row.addWidget(clear_failed_btn)
        maintenance_row.addWidget(refresh_btn)
        maintenance_row.addWidget(clean_btn)
        maintenance_row.addStretch(1)

        status_title = QLabel("Status")
        status_title.setObjectName("CardTitle")

        self.api_key_status = QLabel()
        self.api_key_status.setObjectName("StatusLine")
        self.api_key_status.setWordWrap(True)

        self.ffmpeg_status = QLabel()
        self.ffmpeg_status.setObjectName("StatusLine")
        self.ffmpeg_status.setWordWrap(True)

        self.db_status = QLabel()
        self.db_status.setObjectName("StatusLine")
        self.db_status.setWordWrap(True)

        self.config_status = QLabel()
        self.config_status.setObjectName("StatusLine")
        self.config_status.setWordWrap(True)

        self.app_status_line = QLabel()
        self.app_status_line.setObjectName("StatusLine")
        self.app_status_line.setWordWrap(True)

        settings_layout.addWidget(youtube_title)
        settings_layout.addWidget(api_label)
        settings_layout.addWidget(self.settings_api_key)
        settings_layout.addWidget(folder_label)
        settings_layout.addLayout(folder_row)
        settings_layout.addWidget(quality_label)
        settings_layout.addWidget(self.settings_quality)
        settings_layout.addWidget(save_btn)
        settings_layout.addSpacing(10)
        settings_layout.addWidget(artist_images_title)
        settings_layout.addWidget(artist_images_description)
        settings_layout.addWidget(self.settings_artist_images_enabled)
        settings_layout.addLayout(artist_images_row)
        settings_layout.addWidget(self.artist_images_status)
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

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setObjectName("SettingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setWidget(settings_card)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        layout.addWidget(header)
        layout.addWidget(self.settings_scroll, 1)

        return page

    def build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("PlayerBar")
        bar.setFixedHeight(144)

        layout = QGridLayout(bar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setHorizontalSpacing(12)
        layout.setColumnMinimumWidth(0, 190)
        layout.setColumnMinimumWidth(1, 320)
        layout.setColumnMinimumWidth(2, 190)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 0)
        layout.setColumnStretch(2, 1)

        left_region = QFrame()
        left_region.setObjectName("PlayerRegion")
        left_region.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        left_layout = QHBoxLayout(left_region)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        self.cover_art = QLabel()
        self.cover_art.setObjectName("CoverArt")
        self.cover_art.setFixedSize(76, 76)
        self.cover_art.setAlignment(Qt.AlignCenter)
        self.cover_art.setPixmap(
            render_icon_pixmap("music-note", 30, COLORS["text_primary"])
        )

        track_info = QVBoxLayout()
        track_info.setSpacing(4)
        track_info.setContentsMargins(0, 0, 0, 0)

        self.now_title = ElidedLabel("No track selected")
        self.now_title.setObjectName("NowTitle")

        self.now_artist = ElidedLabel("Double-click a song to play")
        self.now_artist.setObjectName("MutedLabel")

        track_info.addStretch(1)
        track_info.addWidget(self.now_title)
        track_info.addWidget(self.now_artist)
        track_info.addStretch(1)
        left_layout.addWidget(self.cover_art)
        left_layout.addLayout(track_info, 1)

        self.player_center = QFrame()
        self.player_center.setObjectName("PlayerCenter")
        self.player_center.setMinimumWidth(320)
        self.player_center.setMaximumWidth(420)
        center_layout = QVBoxLayout(self.player_center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(6)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addStretch(1)

        self.prev_btn = IconButton(
            "previous", "Previous track", size=22, variant="circle", parent=bar
        )
        self.prev_btn.setObjectName("CircleButton")
        self.prev_btn.clicked.connect(self.play_previous)

        self.play_btn = IconButton(
            "play", "Play or pause", size=24, variant="play", parent=bar
        )
        self.play_btn.setObjectName("PlayButton")
        self.play_btn.setIcon(
            ui_icon(
                "play",
                24,
                color=COLORS["app_background"],
                active_color=COLORS["app_background"],
            )
        )
        self.play_btn.clicked.connect(self.toggle_play)

        self.next_btn = IconButton(
            "next", "Next track", size=22, variant="circle", parent=bar
        )
        self.next_btn.setObjectName("CircleButton")
        self.next_btn.clicked.connect(self.play_next)

        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_btn)
        controls.addStretch(1)

        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setObjectName("ProgressSlider")
        self.progress_slider.setAccessibleName("Playback position")
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

        center_layout.addLayout(controls)
        center_layout.addWidget(self.progress_slider)
        center_layout.addLayout(time_row)

        right_region = QFrame()
        right_region.setObjectName("PlayerRegion")
        right_region.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        right_layout = QVBoxLayout(right_region)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_row.addStretch(1)

        self.autoplay_btn = QPushButton("Auto")
        self.autoplay_btn.setObjectName("ModeButtonActive")
        self.autoplay_btn.setIcon(ui_icon("autoplay", 16))
        self.autoplay_btn.setFixedWidth(58)
        self.autoplay_btn.setToolTip("Toggle autoplay next track")
        self.autoplay_btn.setAccessibleName("Toggle autoplay")
        self.autoplay_btn.clicked.connect(self.toggle_autoplay)

        self.shuffle_btn = QPushButton()
        self.shuffle_btn.setObjectName("ModeButton")
        self.shuffle_btn.setIcon(ui_icon("shuffle", 17))
        self.shuffle_btn.setFixedWidth(34)
        self.shuffle_btn.setToolTip("Toggle shuffle")
        self.shuffle_btn.setAccessibleName("Toggle shuffle")
        self.shuffle_btn.clicked.connect(self.toggle_shuffle)

        self.repeat_btn = QPushButton()
        self.repeat_btn.setObjectName("ModeButton")
        self.repeat_btn.setIcon(ui_icon("repeat", 17))
        self.repeat_btn.setFixedWidth(34)
        self.repeat_btn.setToolTip("Cycle repeat mode")
        self.repeat_btn.setAccessibleName("Cycle repeat mode")
        self.repeat_btn.clicked.connect(self.cycle_repeat)

        self.queue_label = QLabel("Q: 0")
        self.queue_label.setObjectName("TinyLabel")
        self.queue_label.setFixedWidth(40)
        self.queue_label.setAlignment(Qt.AlignCenter)
        self.queue_label.setToolTip("Songs queued to play next")

        mode_row.addWidget(self.autoplay_btn)
        mode_row.addWidget(self.shuffle_btn)
        mode_row.addWidget(self.repeat_btn)
        mode_row.addWidget(self.queue_label)

        volume_row = QHBoxLayout()
        volume_row.setSpacing(8)
        volume_row.addStretch(1)
        self.volume_icon = QLabel()
        self.volume_icon.setObjectName("VolumeIcon")
        self.volume_icon.setFixedSize(20, 20)
        self.volume_icon.setAlignment(Qt.AlignCenter)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setObjectName("VolumeSlider")
        self.volume_slider.setAccessibleName("Playback volume")
        self.volume_slider.setMinimumWidth(96)
        self.volume_slider.setMaximumWidth(150)
        self.volume_slider.setRange(0, 100)
        self.initialize_volume_controls()
        self.volume_slider.valueChanged.connect(self.on_volume_changed)
        self.update_volume_icon()
        volume_row.addWidget(self.volume_icon)
        volume_row.addWidget(self.volume_slider, 1)

        right_layout.addStretch(1)
        right_layout.addLayout(mode_row)
        right_layout.addLayout(volume_row)
        right_layout.addStretch(1)

        layout.addWidget(left_region, 0, 0)
        layout.addWidget(self.player_center, 0, 1)
        layout.addWidget(right_region, 0, 2)

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
        self.setStyleSheet(application_stylesheet())


    def rebuild_track_row_map(self) -> dict[int, int]:
        track_ids = []
        for row in range(self.library_table.rowCount()):
            item = self.library_table.item(row, 0)
            track_ids.append(item.data(Qt.UserRole) if item is not None else None)

        self.track_row_map = build_track_row_map(track_ids)
        return dict(self.track_row_map)

    def locate_visible_track_row(self, track_id: int | None) -> int | None:
        row = self.locate_track_row_in_table(track_id)
        if row is None or self.library_table.isRowHidden(row):
            return None
        return row

    def locate_track_row_in_table(self, track_id: int | None) -> int | None:
        row = locate_track_row(track_id, self.track_row_map)
        if row is not None and 0 <= row < self.library_table.rowCount():
            item = self.library_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == track_id:
                return row

        if track_id is None:
            return None
        self.rebuild_track_row_map()
        return locate_track_row(track_id, self.track_row_map)

    def library_table_is_currently_visible(self) -> bool:
        if hasattr(self, "pages") and hasattr(self, "library_page"):
            if self.pages.currentWidget() is not self.library_page:
                return False
        if hasattr(self, "library_content_stack"):
            if self.library_content_stack.currentIndex() != 0:
                return False
        return True

    def restore_table_selection(self, track_id: int | None) -> int | None:
        self.library_table.clearSelection()
        self.library_table.setCurrentCell(-1, -1)
        if track_id is None:
            return None

        row = self.locate_track_row_in_table(track_id)
        if row is None or self.library_table.isRowHidden(row):
            return None
        self.library_table.selectRow(row)
        return row

    def set_playing_row_treatment(self, row: int, playing: bool) -> None:
        if row < 0 or row >= self.library_table.rowCount():
            return
        title_item = self.library_table.item(row, 0)
        if title_item is None:
            return

        title_item.setData(NOW_PLAYING_ROLE, playing)
        font = title_item.font()
        font.setBold(playing)
        title_item.setFont(font)
        title_item.setForeground(
            QBrush(QColor(COLORS["now_playing"])) if playing else QBrush()
        )

    def apply_now_playing_row_state(
        self,
        *,
        select_if_visible: bool = False,
        scroll_if_visible: bool = False,
    ) -> int | None:
        row = self.locate_track_row_in_table(self.current_track_id)

        if (
            self._styled_now_playing_track_id is not None
            and self._styled_now_playing_track_id != self.current_track_id
        ):
            previous_row = locate_track_row(
                self._styled_now_playing_track_id,
                self.track_row_map,
            )
            if previous_row is not None:
                self.set_playing_row_treatment(previous_row, False)

        if row is None:
            self._playing_row = None
            self._styled_now_playing_track_id = None
            return None

        self.set_playing_row_treatment(row, True)
        self._playing_row = row
        self._styled_now_playing_track_id = self.current_track_id

        if (
            self.library_table.isRowHidden(row)
            or not self.library_table_is_currently_visible()
        ):
            return row

        if select_if_visible:
            self.library_table.selectRow(row)
        if scroll_if_visible:
            item = self.library_table.item(row, 0)
            if item is not None:
                self.library_table.scrollToItem(
                    item,
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
        return row

    def update_now_playing_indicator(
        self,
        track_id: int,
        *,
        select_if_visible: bool = True,
        scroll_if_visible: bool = True,
    ) -> int | None:
        self.current_track_id = int(track_id)
        return self.apply_now_playing_row_state(
            select_if_visible=select_if_visible,
            scroll_if_visible=scroll_if_visible,
        )

    def load_library(self, tracks=None, title: str | None = None, subtitle: str | None = None) -> None:
        self._remember_browser_scroll()
        self._active_browser_kind = None
        if hasattr(self, "library_content_stack"):
            self.library_content_stack.setCurrentIndex(0)

        if tracks is None:
            tracks = self.db.list_tracks()

        selected_track_id = self.selected_track_id()

        self.library_table.setRowCount(len(tracks))
        self.library_table.setIconSize(QSize(42, 42))
        self.track_row_map = {}
        self._playing_row = None
        self._styled_now_playing_track_id = None

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
                    item.setToolTip(str(values[0]))

                    cover_path = track["cover_path"] if "cover_path" in track.keys() else None

                    if cover_path and Path(cover_path).exists():
                        item.setIcon(QIcon(str(cover_path)))

                self.library_table.setItem(row_idx, col_idx, item)

            self.library_table.setRowHeight(row_idx, 54)

        self.rebuild_track_row_map()

        self.track_count_card.value_label.setText(str(len(tracks)))

        if title and hasattr(self, "page_title"):
            self.page_title.setText(title)

        if subtitle and hasattr(self, "page_subtitle"):
            self.page_subtitle.setText(subtitle)

        if not tracks and hasattr(self, "library_empty_state"):
            if self.current_view_kind == "custom":
                self.library_empty_state.title_label.setText("This playlist is empty")
                self.library_empty_state.description_label.setText(
                    "Add a track from Library to begin this playlist."
                )
            else:
                self.library_empty_state.title_label.setText(
                    "Your library is ready for music"
                )
                self.library_empty_state.description_label.setText(
                    "Import a folder to begin building your local collection."
                )

        self.filter_library(self.search_box.text() if hasattr(self, "search_box") else "")
        self.restore_table_selection(selected_track_id)
        self.apply_now_playing_row_state()
        self.update_metadata_action_state()
        self.write_app_status()

    def load_playlists(self) -> None:
        self.playlists.clear()

        def add_sidebar_item(label: str, kind: str, playlist_id: int | None = None) -> None:
            item = QListWidgetItem(label)
            icon_name = {
                "library": "library",
                "recent": "recently-added",
                "downloaded": "downloaded",
                "albums": "albums",
                "artists": "artists",
                "new": "add",
                "custom": "playlists",
            }.get(kind, "playlists")
            item.setIcon(ui_icon(icon_name, 18))
            item.setToolTip(label)
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


    def rounded_cover_pixmap(
        self,
        source: QPixmap,
        size: int,
        radius: float = 14.0,
        *,
        dpr: float | None = None,
    ) -> QPixmap:
        pixel_ratio = float(self.devicePixelRatioF() if dpr is None else dpr)
        if not math.isfinite(pixel_ratio) or pixel_ratio <= 0:
            pixel_ratio = 1.0
        pixel_ratio = min(pixel_ratio, 4.0)
        physical_size = max(1, int(math.ceil(size * pixel_ratio)))
        scaled = source.scaled(
            QSize(physical_size, physical_size),
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        x_offset = max(0, (scaled.width() - physical_size) // 2)
        y_offset = max(0, (scaled.height() - physical_size) // 2)
        target = QPixmap(physical_size, physical_size)
        target.fill(Qt.transparent)
        painter = QPainter(target)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        physical_radius = radius * pixel_ratio
        path.addRoundedRect(
            QRectF(0, 0, physical_size, physical_size),
            physical_radius,
            physical_radius,
        )
        painter.setClipPath(path)
        painter.drawPixmap(
            0,
            0,
            scaled,
            x_offset,
            y_offset,
            physical_size,
            physical_size,
        )
        painter.end()
        target.setDevicePixelRatio(pixel_ratio)
        return target

    def _browser_model(self, kind: str) -> MediaGridModel:
        return self.album_browser_model if kind == "albums" else self.artist_browser_model

    def _browser_proxy(self, kind: str) -> MediaFilterProxyModel:
        return self.album_browser_proxy if kind == "albums" else self.artist_browser_proxy

    def _remember_browser_scroll(self) -> None:
        if self._active_browser_kind and hasattr(self, "browser_view"):
            self._browser_scroll_positions[self._active_browser_kind] = (
                self.browser_view.verticalScrollBar().value()
            )

    def _activate_browser_view(self, kind: str) -> None:
        if self._active_browser_kind != kind:
            self._remember_browser_scroll()
        self._active_browser_kind = kind
        proxy = self._browser_proxy(kind)
        if self.browser_view.model() is not proxy:
            self.browser_view.setModel(proxy)
        self.browser_view.setAccessibleName(
            "Album browser" if kind == "albums" else "Artist browser"
        )
        scroll_position = self._browser_scroll_positions.get(kind, 0)

        def restore_scroll() -> None:
            if self._active_browser_kind == kind and self.browser_view.model() is proxy:
                self.browser_view.verticalScrollBar().setValue(scroll_position)

        QTimer.singleShot(0, restore_scroll)

    def _request_browser_summaries(self, kind: str) -> None:
        revision = browser_revision(self.db.conn)
        browser_kind = BrowserKind(kind)
        cached = self.browser_summary_cache.get(browser_kind, revision)
        if cached is not None:
            self._apply_browser_summaries(kind, cached, revision)
            return

        self.browser_view.set_view_state(
            MediaGridState.LOADING,
            f"Loading {kind.title()}",
            "Preparing your local library summary.",
            "albums" if kind == "albums" else "artist-unknown",
        )
        token = self.browser_summary_cache.token(browser_kind, revision)
        db_path = Path(getattr(self.db, "db_path", database_path()))
        query = (
            partial(load_album_summaries, db_path)
            if kind == "albums"
            else partial(load_artist_summaries, db_path)
        )
        self.browser_summary_loader.request(kind, token, query)

    def _browser_summaries_loaded(
        self,
        kind: str,
        _request_token: int,
        cache_token: object,
        summaries: object,
    ) -> None:
        try:
            accepted = self.browser_summary_cache.put(cache_token, tuple(summaries))
        except (TypeError, ValueError):
            accepted = False
        if not accepted or self._active_browser_kind != kind:
            return
        if self.current_view_kind != kind:
            return
        self._apply_browser_summaries(kind, tuple(summaries), cache_token.revision)

    def _browser_summaries_failed(
        self,
        kind: str,
        _request_token: int,
        _cache_token: object,
        message: str,
    ) -> None:
        if self._active_browser_kind != kind or self.current_view_kind != kind:
            return
        self.track_count_card.value_label.setText("0")
        self.browser_view.set_view_state(
            MediaGridState.ERROR,
            f"Could not load {kind.title()}",
            message,
            "error",
        )

    @staticmethod
    def _track_count_text(count: int) -> str:
        return f"{count} track" if int(count) == 1 else f"{count} tracks"

    def _album_media_item(self, summary: AlbumSummary) -> MediaItem:
        details = [summary.album_artist]
        if summary.canonical_year:
            details.append(summary.canonical_year)
        details.append(self._track_count_text(summary.track_count))
        cover_path = summary.representative_cover_path
        return MediaItem(
            key=summary.browser_key,
            kind=MediaKind.ALBUM,
            title=summary.album_title,
            subtitle=" • ".join(details),
            artwork_path=cover_path,
            image_state=(MediaImageState.LOADING if cover_path else MediaImageState.MISSING),
        )

    def _artist_media_item(self, summary: ArtistSummary) -> MediaItem:
        # Artist cards deliberately start with no track cover. A dedicated
        # artist-image cache may supply an artwork path later.
        return MediaItem(
            key=summary.browser_key,
            kind=MediaKind.ARTIST,
            title=summary.display_name,
            subtitle=self._track_count_text(summary.track_count),
            artwork_path=None,
            image_state=MediaImageState.MISSING,
        )

    def _apply_browser_summaries(
        self,
        kind: str,
        summaries: tuple[AlbumSummary | ArtistSummary, ...],
        revision: object,
    ) -> None:
        model = self._browser_model(kind)
        proxy = self._browser_proxy(kind)
        changed = self._browser_model_revisions.get(kind) != revision
        self._browser_summary_maps[kind] = {
            summary.browser_key: summary for summary in summaries
        }
        if changed:
            items = (
                tuple(self._album_media_item(summary) for summary in summaries)
                if kind == "albums"
                else tuple(self._artist_media_item(summary) for summary in summaries)
            )
            model.set_items(items)
            model.set_thumbnail_generation(self.thumbnail_cache.generation)
            self._browser_model_revisions[kind] = revision
            self._browser_scroll_positions[kind] = 0
            self.browser_view.clearSelection()

        proxy.set_filter_text(self.search_box.text() if hasattr(self, "search_box") else "")
        self.track_count_card.value_label.setText(str(proxy.rowCount()))
        if not summaries:
            title = "No albums yet" if kind == "albums" else "No artists yet"
            description = (
                "Imported tracks with album metadata will appear here."
                if kind == "albums"
                else "Imported artist metadata will appear here."
            )
            self.browser_view.set_view_state(
                MediaGridState.EMPTY,
                title,
                description,
                "albums" if kind == "albums" else "artist-unknown",
            )
        elif proxy.rowCount() == 0:
            self.browser_view.set_view_state(
                MediaGridState.EMPTY,
                "No matching cards",
                "Try a shorter album or artist search.",
                "search",
            )
        else:
            self.browser_view.set_view_state(MediaGridState.CONTENT)
        self.browser_view.schedule_visible_items()

    def show_album_browser(self) -> None:
        self.current_view_kind = "albums"
        self.library_content_stack.setCurrentIndex(1)
        self.page_title.setText("Albums")
        self.page_subtitle.setText("Browse your collection by album.")
        self.browser_title.setText("Albums")
        self.browser_hint.setText("Click an album to view its tracks")
        self.browser_action_btn.setVisible(False)
        self._activate_browser_view("albums")
        self._request_browser_summaries("albums")

    def show_artist_browser(self) -> None:
        self.current_view_kind = "artists"
        self.library_content_stack.setCurrentIndex(1)
        self.page_title.setText("Artists")
        self.page_subtitle.setText("Browse your collection by artist.")
        self.browser_title.setText("Artists")
        self.browser_hint.setText("Click an artist to view their tracks")
        self.browser_action_btn.setVisible(
            not bool(self.config.get("artist_image_fetch_enabled", False))
        )
        self._activate_browser_view("artists")
        self._request_browser_summaries("artists")

    def open_browser_item(self, browser_key: str) -> None:
        if self._active_browser_kind == "albums":
            self.open_album(browser_key)
        elif self._active_browser_kind == "artists":
            self.open_artist(browser_key)

    def show_browser_context_menu(self, browser_key: str, global_position) -> None:
        summary = self._browser_summary_maps.get(self._active_browser_kind or "", {}).get(
            browser_key
        )
        if summary is None:
            return
        menu = QMenu(self)
        action = menu.addAction(
            ui_icon("albums" if self._active_browser_kind == "albums" else "artists", 18),
            "Open Album" if self._active_browser_kind == "albums" else "Open Artist",
        )
        action.triggered.connect(lambda: self.open_browser_item(browser_key))
        if self._active_browser_kind == "artists":
            item = self.artist_browser_model.item_for_key(browser_key)
            if item is not None:
                menu.addSeparator()
                if self.config.get("artist_image_fetch_enabled") is True:
                    summary = self._browser_summary_maps["artists"].get(browser_key)
                    if (
                        isinstance(summary, ArtistSummary)
                        and summary.key.normalized_name
                    ):
                        refresh_action = menu.addAction(
                            ui_icon("refresh", 18),
                            "Refresh Artist Photo",
                        )
                        refresh_action.triggered.connect(
                            lambda: self.refresh_artist_photo(browser_key)
                        )
                if item.has_cached_image:
                    clear_action = menu.addAction(
                        ui_icon("remove", 18),
                        "Clear Cached Artist Photo",
                    )
                    clear_action.triggered.connect(
                        lambda: self.clear_cached_artist_photo(browser_key)
                    )
                if item.source_url and is_safe_artist_source_url(item.source_url):
                    source_action = menu.addAction(
                        ui_icon("folder", 18),
                        "View Image Source",
                    )
                    source_action.triggered.connect(
                        lambda: self.open_artist_image_source(browser_key)
                    )
        menu.exec(global_position)

    def load_visible_browser_images(self, browser_keys: tuple[str, ...]) -> None:
        kind = self._active_browser_kind
        if kind not in {"albums", "artists"}:
            return
        model = self._browser_model(kind)
        dpr = self.browser_view.devicePixelRatioF()
        crop = "square" if kind == "albums" else "portrait"
        generation = self.thumbnail_cache.generation
        model.set_thumbnail_generation(generation)
        for browser_key in browser_keys:
            item = model.item_for_key(browser_key)
            if (
                item is None
                or not item.artwork_path
                or item.image_state is MediaImageState.FAILED
            ):
                continue
            thumbnail_key = make_thumbnail_key(item.artwork_path, 156, dpr, crop)
            model.bind_thumbnail(browser_key, thumbnail_key)
            self.thumbnail_cache.request(
                item.artwork_path,
                156,
                dpr,
                crop=crop,
                generation=generation,
            )

        if kind != "artists":
            return
        network_enabled = self.config.get("artist_image_fetch_enabled") is True
        for browser_key in browser_keys:
            item = self.artist_browser_model.item_for_key(browser_key)
            summary = self._browser_summary_maps["artists"].get(browser_key)
            if (
                item is None
                or not isinstance(summary, ArtistSummary)
                or not summary.key.normalized_name
                or item.artwork_path
                or browser_key in self._pending_artist_image_keys
            ):
                continue
            self._pending_artist_image_keys.add(browser_key)
            if network_enabled:
                self.artist_browser_model.replace_item(
                    browser_key,
                    image_state=MediaImageState.LOADING,
                )
            self.artist_image_service.request(
                item.title,
                lambda result, key=browser_key: self._artist_image_result(key, result),
                network_enabled=network_enabled,
            )

    def _artist_image_result(
        self,
        browser_key: str,
        result: ArtistImageResult,
    ) -> None:
        self._pending_artist_image_keys.discard(browser_key)
        summary = self._browser_summary_maps["artists"].get(browser_key)
        item = self.artist_browser_model.item_for_key(browser_key)
        if not isinstance(summary, ArtistSummary) or item is None:
            return
        if result.identity.normalized_key != summary.key.normalized_name:
            return

        if (
            result.status is ArtistImageStatus.RESOLVED
            and result.cache_file is not None
            and result.cache_file.is_file()
        ):
            self.artist_browser_model.replace_item(
                browser_key,
                artwork_path=str(result.cache_file),
                image_state=MediaImageState.LOADING,
                has_cached_image=True,
                source_url=(
                    result.source_page_url
                    if is_safe_artist_source_url(result.source_page_url)
                    else None
                ),
            )
            if browser_key in self.browser_view.visible_item_keys():
                self.load_visible_browser_images((browser_key,))
            return

        self.artist_browser_model.replace_item(
            browser_key,
            artwork_path=None,
            image_state=(
                MediaImageState.FAILED
                if result.status
                in {ArtistImageStatus.TEMPORARY_ERROR, ArtistImageStatus.UNAVAILABLE}
                else MediaImageState.MISSING
            ),
            has_cached_image=False,
            source_url=None,
        )

    def refresh_artist_photo(self, browser_key: str) -> None:
        if self.config.get("artist_image_fetch_enabled") is not True:
            return
        summary = self._browser_summary_maps["artists"].get(browser_key)
        item = self.artist_browser_model.item_for_key(browser_key)
        if (
            not isinstance(summary, ArtistSummary)
            or not summary.key.normalized_name
            or item is None
            or browser_key in self._pending_artist_image_keys
        ):
            return
        self._pending_artist_image_keys.add(browser_key)
        self.artist_browser_model.replace_item(
            browser_key,
            image_state=MediaImageState.LOADING,
        )
        self.artist_image_service.request(
            item.title,
            lambda result, key=browser_key: self._artist_image_result(key, result),
            force=True,
            network_enabled=True,
        )

    def clear_cached_artist_photo(self, browser_key: str) -> None:
        summary = self._browser_summary_maps["artists"].get(browser_key)
        item = self.artist_browser_model.item_for_key(browser_key)
        if not isinstance(summary, ArtistSummary) or item is None:
            return
        if item.artwork_path:
            self.thumbnail_cache.invalidate_source(item.artwork_path)
        self.artist_image_service.clear_cache(
            ArtistIdentity.from_display_name(summary.display_name)
        )
        self._pending_artist_image_keys.clear()
        self._reset_abandoned_artist_image_states()
        self.artist_browser_model.replace_item(
            browser_key,
            artwork_path=None,
            image_state=MediaImageState.MISSING,
            has_cached_image=False,
            source_url=None,
        )
        if self.current_view_kind == "artists":
            self.load_visible_browser_images(
                tuple(
                    key
                    for key in self.browser_view.visible_item_keys()
                    if key != browser_key
                )
            )
        self.refresh_artist_cache_status()

    def open_artist_image_source(self, browser_key: str) -> None:
        item = self.artist_browser_model.item_for_key(browser_key)
        if item is None or not is_safe_artist_source_url(item.source_url):
            return
        QDesktopServices.openUrl(QUrl(str(item.source_url)))

    def invalidate_browser_data(
        self,
        reason: BrowserInvalidationReason | str,
    ) -> None:
        """Invalidate only browser data affected by a real library mutation."""
        plan = self.browser_summary_cache.invalidate(reason)
        if plan.album_summaries:
            self.browser_summary_loader.invalidate("albums")
            self._browser_model_revisions["albums"] = None
        if plan.artist_summaries:
            self.browser_summary_loader.invalidate("artists")
            self._browser_model_revisions["artists"] = None
        if plan.album_thumbnails:
            for item in self.album_browser_model.items():
                if item.artwork_path:
                    self.thumbnail_cache.invalidate_source(item.artwork_path)
        if plan.artist_thumbnails:
            for item in self.artist_browser_model.items():
                if item.artwork_path:
                    self.thumbnail_cache.invalidate_source(item.artwork_path)

    def open_album(self, browser_key: str) -> None:
        summary = self._browser_summary_maps["albums"].get(str(browser_key))
        if not isinstance(summary, AlbumSummary):
            return
        self._remember_browser_scroll()
        rows = query_album_tracks(self.db.conn, summary.key)
        self.current_view_kind = "album_tracks"
        self.current_playlist_name = summary.album_title
        self._detail_browser_context = ("album_tracks", summary.key, summary.album_title)
        self.load_library(rows, summary.album_title, "Album view")

    def open_artist(self, browser_key: str) -> None:
        summary = self._browser_summary_maps["artists"].get(str(browser_key))
        if not isinstance(summary, ArtistSummary):
            return
        self._remember_browser_scroll()
        rows = query_artist_tracks(self.db.conn, summary.key)
        self.current_view_kind = "artist_tracks"
        self.current_playlist_name = summary.display_name
        self._detail_browser_context = ("artist_tracks", summary.key, summary.display_name)
        self.load_library(rows, summary.display_name, "Artist view")

    def refresh_artwork(self) -> None:
        updated = refresh_covers_for_library(self.db)
        if updated:
            self.invalidate_browser_data(BrowserInvalidationReason.ARTWORK_REFRESH)
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
        if self.current_view_kind in {"album_tracks", "artist_tracks"}:
            context = self._detail_browser_context
            if context and context[0] == self.current_view_kind:
                kind, key, label = context
                rows = (
                    query_album_tracks(self.db.conn, key)
                    if kind == "album_tracks"
                    else query_artist_tracks(self.db.conn, key)
                )
                self.load_library(
                    rows,
                    label,
                    "Album view" if kind == "album_tracks" else "Artist view",
                )
                return

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
        self._detail_browser_context = None

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
        if count:
            self.invalidate_browser_data(BrowserInvalidationReason.IMPORT_FOLDER)
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
        self.set_sync_visual_state("syncing")

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
        if imported_count:
            self.invalidate_browser_data(BrowserInvalidationReason.YOUTUBE_IMPORT)
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
        self.set_sync_visual_state(result.status)

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

    def selected_track_ids(self) -> list[int]:
        selection = self.library_table.selectionModel()
        if selection is None:
            return []
        track_ids: list[int] = []
        for index in selection.selectedRows(0):
            item = self.library_table.item(index.row(), 0)
            if item is not None:
                track_ids.append(int(item.data(Qt.UserRole)))
        return list(dict.fromkeys(track_ids))

    def update_metadata_action_state(self) -> None:
        action = getattr(self, "edit_metadata_action", None)
        if action is not None:
            table_active = (
                getattr(self, "_active_browser_kind", None) is None
                and (
                    not hasattr(self, "library_content_stack")
                    or self.library_content_stack.currentIndex() == 0
                )
            )
            action.setEnabled(table_active and len(self.selected_track_ids()) == 1)

    def open_metadata_editor(self, *, musicbrainz_tab: bool = False) -> None:
        track_ids = self.selected_track_ids()
        table_active = (
            getattr(self, "_active_browser_kind", None) is None
            and (
                not hasattr(self, "library_content_stack")
                or self.library_content_stack.currentIndex() == 0
            )
        )
        if not table_active or len(track_ids) != 1:
            QMessageBox.information(
                self,
                "Select one track",
                "Select exactly one track to edit its metadata.",
            )
            return
        self.open_metadata_editor_for_track(track_ids[0], musicbrainz_tab=musicbrainz_tab)

    def open_metadata_editor_for_track(
        self, track_id: int, *, musicbrainz_tab: bool = False
    ) -> None:
        if self._metadata_editor is not None and self._metadata_editor.isVisible():
            self._metadata_editor.raise_()
            self._metadata_editor.activateWindow()
            return
        if self.db.get_track(int(track_id)) is None:
            return
        dialog = MetadataEditorDialog(self.metadata_service, int(track_id), self)
        dialog.metadata_changed.connect(self.metadata_change_applied)
        dialog.finished.connect(lambda _result: setattr(self, "_metadata_editor", None))
        self._metadata_editor = dialog
        if musicbrainz_tab:
            dialog.tabs.setCurrentWidget(dialog.musicbrainz_tab)
        dialog.open()

    def open_metadata_remediation(self) -> None:
        dialog = self._metadata_remediation_dialog
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = MetadataRemediationDialog(self.db, self)
        dialog.tracks_changed.connect(self.remediation_tracks_changed)
        dialog.edit_track_requested.connect(self.open_metadata_editor_for_track)
        dialog.finished.connect(
            lambda _result: setattr(self, "_metadata_remediation_dialog", None)
        )
        self._metadata_remediation_dialog = dialog
        dialog.open()

    def remediation_tracks_changed(self, track_ids: object) -> None:
        try:
            changed_ids = tuple(sorted({int(value) for value in track_ids}))
        except (TypeError, ValueError):
            return
        if not changed_ids:
            return

        self.invalidate_browser_data(BrowserInvalidationReason.FUTURE_METADATA)
        for track_id in changed_ids:
            self.refresh_visible_track_metadata(track_id)

        if self.current_track_id in changed_ids:
            track = self.db.get_track(self.current_track_id)
            if track is not None:
                self.now_title.setText(track["title"] or Path(track["path"]).stem)
                self.now_artist.setText(track["artist"] or "Unknown Artist")
                self.set_cover_art(track["cover_path"])

        if self.current_view_kind in {"albums", "artists"}:
            if self.current_view_kind == "albums":
                self.show_album_browser()
            else:
                self.show_artist_browser()
        elif self.current_view_kind in {"album_tracks", "artist_tracks"}:
            self.refresh_current_view()
        self.write_app_status()

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

        self.update_now_playing_indicator(track_id)
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
                self.cover_art.setPixmap(
                    self.rounded_cover_pixmap(pixmap, 72, radius=10.0)
                )
                return

        self.cover_art.setPixmap(
            render_icon_pixmap("music-note", 30, COLORS["text_primary"])
        )

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
            self.queue_label.setText(f"Q: {len(self.manual_queue)}")

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
        play_action.setIcon(ui_icon("play", 18))
        play_next_action = menu.addAction("Play Next")
        play_next_action.setIcon(ui_icon("queue-next", 18))
        add_playlist_action = menu.addAction("Add to Playlist")
        add_playlist_action.setIcon(ui_icon("playlists", 18))
        menu.addSeparator()
        edit_metadata_action = menu.addAction("Edit Metadata")
        edit_metadata_action.setIcon(ui_icon("metadata", 18))

        action = menu.exec(self.library_table.viewport().mapToGlobal(position))

        if action == play_action:
            self.play_selected()
        elif action == play_next_action:
            self.queue_selected_next()
        elif action == add_playlist_action:
            self.add_selected_to_playlist()
        elif action == edit_metadata_action:
            self.open_metadata_editor()

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
            self.autoplay_btn.setText("Auto")
            self.autoplay_btn.setObjectName("ModeButtonActive")
            self.autoplay_btn.setIcon(
                ui_icon("autoplay", 16, color=COLORS["accent"])
            )
            self.autoplay_btn.setToolTip("Autoplay is on")
        else:
            self.autoplay_btn.setText("Auto")
            self.autoplay_btn.setObjectName("ModeButton")
            self.autoplay_btn.setIcon(ui_icon("autoplay", 16))
            self.autoplay_btn.setToolTip("Autoplay is off")

        if self.shuffle_enabled:
            self.shuffle_btn.setObjectName("ModeButtonActive")
            self.shuffle_btn.setIcon(
                ui_icon("shuffle", 17, color=COLORS["accent"])
            )
            self.shuffle_btn.setToolTip("Shuffle is on")
        else:
            self.shuffle_btn.setObjectName("ModeButton")
            self.shuffle_btn.setIcon(ui_icon("shuffle", 17))
            self.shuffle_btn.setToolTip("Shuffle is off")

        if self.repeat_mode == "off":
            self.repeat_btn.setObjectName("ModeButton")
            self.repeat_btn.setIcon(ui_icon("repeat", 17))
            self.repeat_btn.setToolTip("Repeat is off")
        elif self.repeat_mode == "all":
            self.repeat_btn.setObjectName("ModeButtonActive")
            self.repeat_btn.setIcon(
                ui_icon("repeat", 17, color=COLORS["accent"])
            )
            self.repeat_btn.setToolTip("Repeat all is on")
        else:
            self.repeat_btn.setObjectName("ModeButtonActive")
            self.repeat_btn.setIcon(
                ui_icon("repeat-one", 17, color=COLORS["accent"])
            )
            self.repeat_btn.setToolTip("Repeat one is on")

        self.update_queue_label()

        for btn in [self.autoplay_btn, self.shuffle_btn, self.repeat_btn]:
            repolish(btn)

        self.write_app_status()

    def on_playback_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlayingState:
            self.play_btn.setIcon(
                ui_icon(
                    "pause",
                    24,
                    color=COLORS["app_background"],
                    active_color=COLORS["app_background"],
                )
            )
            self.play_btn.setToolTip("Pause")
            self.play_btn.setAccessibleName("Pause")
        else:
            self.play_btn.setIcon(
                ui_icon(
                    "play",
                    24,
                    color=COLORS["app_background"],
                    active_color=COLORS["app_background"],
                )
            )
            self.play_btn.setToolTip("Play")
            self.play_btn.setAccessibleName("Play")

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
        """Compatibility entry point: open explicit candidate review, never auto-apply."""

        self.open_metadata_editor(musicbrainz_tab=True)

    def refresh_visible_track_metadata(self, track_id: int) -> None:
        track = self.db.get_track(track_id)
        row = locate_track_row(track_id, self.track_row_map)
        if track is None or row is None:
            return
        values = (
            track["title"] or Path(track["path"]).stem,
            track["artist"] or "",
            track["album"] or "",
            track["year"] or "",
        )
        for column, value in enumerate(values):
            item = self.library_table.item(row, column)
            if item is not None:
                item.setText(str(value))
        title_item = self.library_table.item(row, 0)
        if title_item is not None:
            title_item.setToolTip(str(values[0]))
            title_item.setIcon(QIcon())
            cover_path = track["cover_path"]
            if cover_path and Path(cover_path).is_file():
                title_item.setIcon(QIcon(str(cover_path)))
        self.apply_now_playing_row_state()

    def metadata_change_applied(self, result: MetadataChangeResult) -> None:
        if not isinstance(result, MetadataChangeResult) or not result.changed:
            return
        changed = set(result.changed_fields)
        old_artwork = result.before.value("artwork")
        new_artwork = result.after.value("artwork")
        if "artwork" in changed:
            for path in (old_artwork, new_artwork):
                if path:
                    self.thumbnail_cache.invalidate_source(path)

        identity_fields = {"artist", "album", "album_artist", "release_date"}
        if changed & identity_fields:
            self.invalidate_browser_data(BrowserInvalidationReason.FUTURE_METADATA)
        elif "artwork" in changed:
            self.invalidate_browser_data(BrowserInvalidationReason.ARTWORK_REFRESH)

        self.refresh_visible_track_metadata(result.track_id)
        if self.current_track_id == result.track_id:
            track = self.db.get_track(result.track_id)
            if track is not None:
                self.now_title.setText(track["title"] or Path(track["path"]).stem)
                self.now_artist.setText(track["artist"] or "Unknown Artist")
                self.set_cover_art(track["cover_path"])

        if self.current_view_kind in {"albums", "artists"}:
            if self.current_view_kind == "albums":
                self.show_album_browser()
            else:
                self.show_artist_browser()
        elif self.current_view_kind in {"album_tracks", "artist_tracks"}:
            self.refresh_current_view()
        self.write_app_status()

    def filter_library(self, text: str) -> None:
        if (
            self.current_view_kind in {"albums", "artists"}
            and getattr(self, "_active_browser_kind", None) == self.current_view_kind
            and hasattr(self, "browser_view")
        ):
            kind = self.current_view_kind
            proxy = self._browser_proxy(kind)
            source_count = proxy.sourceModel().rowCount() if proxy.sourceModel() else 0
            proxy.set_filter_text(text)
            self.track_count_card.value_label.setText(str(proxy.rowCount()))
            if self.browser_view.view_state() not in {
                MediaGridState.LOADING,
                MediaGridState.ERROR,
            }:
                if source_count and proxy.rowCount() == 0:
                    self.browser_view.set_view_state(
                        MediaGridState.EMPTY,
                        "No matching cards",
                        "Try a shorter album or artist search.",
                        "search",
                    )
                elif source_count:
                    self.browser_view.set_view_state(MediaGridState.CONTENT)
                else:
                    self.browser_view.set_view_state(
                        MediaGridState.EMPTY,
                        "No albums yet" if kind == "albums" else "No artists yet",
                        (
                            "Imported tracks with album metadata will appear here."
                            if kind == "albums"
                            else "Imported artist metadata will appear here."
                        ),
                        "albums" if kind == "albums" else "artist-unknown",
                    )
            self.browser_view.schedule_visible_items()
            return

        needle = text.lower().strip()
        visible_count = 0

        for row in range(self.library_table.rowCount()):
            row_text = " ".join(
                self.library_table.item(row, col).text().lower()
                for col in range(self.library_table.columnCount())
                if self.library_table.item(row, col)
            )
            hidden = needle not in row_text
            self.library_table.setRowHidden(row, hidden)
            if not hidden:
                visible_count += 1

        if hasattr(self, "library_body_stack"):
            if self.library_table.rowCount() == 0:
                self.library_body_stack.setCurrentIndex(1)
            elif visible_count == 0:
                self.library_body_stack.setCurrentIndex(2)
            else:
                self.library_body_stack.setCurrentIndex(0)


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

        self.invalidate_browser_data(BrowserInvalidationReason.REMOVE_MISSING)
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

    def confirm_enable_artist_photos(self) -> None:
        if self.config.get("artist_image_fetch_enabled") is True:
            return
        answer = QMessageBox.question(
            self,
            "Enable Artist Photos?",
            "When enabled, Music Vault sends visible artist names to public "
            "MusicBrainz and Wikimedia/Wikipedia services and caches image "
            "results locally. No API key is used. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            if hasattr(self, "settings_artist_images_enabled"):
                self.settings_artist_images_enabled.setChecked(False)
            return
        self.config["artist_image_fetch_enabled"] = True
        if hasattr(self, "settings_artist_images_enabled"):
            self.settings_artist_images_enabled.setChecked(True)
        self.save_config()
        self.refresh_artist_cache_status()
        if hasattr(self, "browser_action_btn"):
            self.browser_action_btn.setVisible(False)
        if self.current_view_kind == "artists":
            self.load_visible_browser_images(self.browser_view.visible_item_keys())

    def on_artist_image_setting_clicked(self, checked: bool) -> None:
        if checked:
            self.confirm_enable_artist_photos()
            return
        self.config["artist_image_fetch_enabled"] = False
        self.artist_image_service.cancel_all()
        self._pending_artist_image_keys.clear()
        self._reset_abandoned_artist_image_states()
        self.save_config()
        self.refresh_artist_cache_status()
        if self.current_view_kind == "artists":
            self.browser_action_btn.setVisible(True)

    def _reset_abandoned_artist_image_states(self) -> None:
        for item in self.artist_browser_model.items():
            if item.image_state is not MediaImageState.LOADING:
                continue
            self.artist_browser_model.replace_item(
                item.key,
                image_state=(
                    MediaImageState.READY
                    if item.artwork_path
                    else MediaImageState.MISSING
                ),
            )

    def clear_artist_image_cache(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear artist-photo cache?",
            "Delete cached artist photos and lookup results only? Your music, "
            "metadata, database, and album artwork will not be changed.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        for item in self.artist_browser_model.items():
            if item.artwork_path:
                self.thumbnail_cache.invalidate_source(item.artwork_path)
        self.artist_image_service.clear_cache()
        self._pending_artist_image_keys.clear()
        for item in self.artist_browser_model.items():
            self.artist_browser_model.replace_item(
                item.key,
                artwork_path=None,
                image_state=MediaImageState.MISSING,
                has_cached_image=False,
                source_url=None,
            )
        self.refresh_artist_cache_status()
        QMessageBox.information(
            self,
            "Artist photos cleared",
            "The local artist-photo cache was cleared.",
        )

    def open_artist_image_cache_folder(self) -> None:
        folder = artist_images_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    @staticmethod
    def _format_cache_bytes(value: int) -> str:
        size = max(0, int(value))
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def refresh_artist_cache_status(self) -> None:
        if not hasattr(self, "artist_images_status"):
            return
        try:
            stats = self.artist_image_cache.statistics()
            self.artist_images_status.setText(
                "Artist Photo Fetching: "
                + (
                    "Enabled"
                    if self.config.get("artist_image_fetch_enabled") is True
                    else "Disabled"
                )
                + f"\nCached Results: {stats['entry_count']}"
                + f"\nCached Images: {stats['file_count']} "
                + f"({self._format_cache_bytes(stats['total_bytes'])})"
                + f"\nCache Folder: {artist_images_dir()}"
            )
        except Exception:
            self.artist_images_status.setText(
                "Artist Photo Cache: Status unavailable"
            )

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
        self.config["artist_image_fetch_enabled"] = bool(
            self.settings_artist_images_enabled.isChecked()
            and self.config.get("artist_image_fetch_enabled") is True
        )
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

        if hasattr(self, "settings_artist_images_enabled"):
            previous = self.settings_artist_images_enabled.blockSignals(True)
            try:
                self.settings_artist_images_enabled.setChecked(
                    self.config.get("artist_image_fetch_enabled") is True
                )
            finally:
                self.settings_artist_images_enabled.blockSignals(previous)
        self.refresh_artist_cache_status()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._dark_title_bar_attempted:
            self._dark_title_bar_attempted = True
            self._dark_title_bar_applied = apply_dark_title_bar(self)

    def closeEvent(self, event) -> None:
        self.flush_pending_volume_save()
        self.browser_summary_loader.close()
        self.thumbnail_cache.close()
        self.artist_image_service.shutdown()
        super().closeEvent(event)

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
    schedule_ui_review(window, app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
