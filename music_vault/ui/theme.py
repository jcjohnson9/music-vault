from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from music_vault.core.paths import assets_dir


COLORS: Final[dict[str, str]] = {
    "app_background": "#06080C",
    "sidebar_background": "#090D12",
    "elevated_surface": "#111720",
    "card_surface": "#0D1219",
    "subtle_surface": "#0A0F15",
    "hover_surface": "#18212D",
    "pressed_surface": "#202C3A",
    "border": "#202A37",
    "strong_border": "#334155",
    "text_primary": "#F4F7FB",
    "text_secondary": "#C4CDD8",
    "text_muted": "#8490A0",
    "accent": "#1DB954",
    "accent_hover": "#25D366",
    "accent_pressed": "#169746",
    "accent_ink": "#041108",
    "danger": "#F06464",
    "danger_hover": "#FF7373",
    "warning": "#F3B84B",
    "selection": "#173F27",
    "selection_hover": "#1D4E31",
    "now_playing": "#25D366",
    "focus_ring": "#5BE58A",
    "disabled_text": "#596474",
    "scrollbar": "#3A4656",
    "scrollbar_hover": "#566477",
}

SPACING: Final[dict[str, int]] = {
    "xxs": 4,
    "xs": 8,
    "sm": 12,
    "md": 16,
    "lg": 20,
    "xl": 24,
    "xxl": 32,
}

RADII: Final[dict[str, int]] = {
    "control": 10,
    "card": 16,
    "panel": 20,
    "circular": 999,
    "artwork": 14,
}

TYPOGRAPHY: Final[dict[str, object]] = {
    "family": "Segoe UI",
    "family_candidates": ("Segoe UI Variable", "Segoe UI"),
    "page_title_size": 30,
    "section_title_size": 18,
    "card_title_size": 15,
    "body_size": 13,
    "metadata_size": 12,
    "caption_size": 11,
    "button_size": 12,
}


def preferred_font_family() -> str:
    """Choose one installed system font name that Qt QSS can parse reliably."""

    try:
        from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication

        if QGuiApplication.instance() is None:
            return str(TYPOGRAPHY["family"])
        installed = set(QFontDatabase.families())
        for candidate in TYPOGRAPHY["family_candidates"]:
            if candidate in installed:
                return str(candidate)
        application_family = QGuiApplication.instance().font().family()
        return application_family or QFont().defaultFamily() or str(TYPOGRAPHY["family"])
    except (RuntimeError, TypeError):
        return str(TYPOGRAPHY["family"])


def _qss_asset_url(name: str) -> str:
    """Return a quoted, source/packaged-safe QSS URL for a fixed UI asset."""

    icon_root = (assets_dir() / "icons" / "ui").resolve()
    candidate = (icon_root / name).resolve()
    if candidate.parent != icon_root:
        raise ValueError("Theme asset paths must remain inside the UI icon directory.")
    return Path(candidate).as_posix().replace('"', "%22")


def application_stylesheet() -> str:
    """Return the shared Music Vault stylesheet generated from design tokens."""

    c = COLORS
    r = RADII
    t = TYPOGRAPHY
    font_family = preferred_font_family().replace('"', "")
    check_icon = _qss_asset_url("check.svg")
    chevron_icon = _qss_asset_url("chevron-down.svg")
    return f"""
QMainWindow,
QWidget#AppRoot {{
    background: {c['app_background']};
    color: {c['text_primary']};
    font-family: "{font_family}";
    font-size: {t['body_size']}px;
}}

QWidget {{
    color: {c['text_primary']};
    font-family: "{font_family}";
    font-size: {t['body_size']}px;
}}

QLabel {{
    background: transparent;
    border: none;
    color: {c['text_primary']};
}}

QLabel#Brand {{
    color: {c['text_primary']};
    font-size: 20px;
    font-weight: 700;
}}

QLabel#PageTitle {{
    color: {c['text_primary']};
    font-size: {t['page_title_size']}px;
    font-weight: 700;
}}

QLabel#CardTitle,
QLabel#SectionHeaderTitle {{
    color: {c['text_primary']};
    font-size: {t['card_title_size']}px;
    font-weight: 650;
}}

QLabel#MutedLabel,
QLabel#SectionHeaderSubtitle,
QLabel#EmptyStateDescription {{
    color: {c['text_muted']};
    font-size: {t['metadata_size']}px;
}}

QLabel#SectionLabel {{
    color: {c['text_muted']};
    font-size: {t['caption_size']}px;
    font-weight: 700;
}}

QLabel#TinyLabel {{
    color: {c['text_muted']};
    font-size: {t['caption_size']}px;
}}

QLabel#NowTitle {{
    color: {c['text_primary']};
    font-size: 15px;
    font-weight: 700;
}}

QLabel#StatValue,
QLabel#SyncMetricValue {{
    color: {c['text_primary']};
    font-size: 22px;
    font-weight: 700;
}}

QLabel#StatusLine {{
    background: {c['subtle_surface']};
    border: 1px solid {c['border']};
    border-radius: {r['control']}px;
    padding: 12px;
    color: {c['text_secondary']};
}}

QLabel#LogoBadge {{
    background: {c['accent']};
    color: {c['accent_ink']};
    border-radius: 20px;
}}

QFrame#MainShell,
QFrame#SectionHeader {{
    background: transparent;
    border: none;
}}

QWidget#PlayerRegion,
QFrame#PlayerRegion,
QWidget#PlayerCenter,
QFrame#PlayerCenter,
QScrollArea#SettingsScroll,
QScrollArea#SettingsScroll > QWidget,
QScrollArea#SettingsScroll > QWidget > QWidget,
QScrollArea#SettingsScroll QWidget#qt_scrollarea_viewport {{
    background: transparent;
    border: none;
}}

QFrame#Sidebar {{
    background: {c['sidebar_background']};
    border: 1px solid {c['border']};
    border-radius: {r['panel']}px;
}}

QFrame#HeroHeader {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #151D27, stop:0.52 #101720, stop:1 #0B1017);
    border: 1px solid {c['border']};
    border-radius: {r['panel']}px;
}}

QFrame#TopHeader,
QFrame#Card,
QFrame#StatCard,
QFrame#SyncMetricCard {{
    background: {c['card_surface']};
    border: 1px solid {c['border']};
    border-radius: {r['card']}px;
}}

QFrame#SyncMetricCard[syncState="complete"] {{
    background: {c['selection']};
    border-color: {c['accent']};
}}

QFrame#SyncMetricCard[syncState="complete_with_issues"] {{
    background: #2A2112;
    border-color: {c['warning']};
}}

QFrame#SyncMetricCard[syncState="failed"] {{
    background: #2C1519;
    border-color: {c['danger']};
}}

QFrame#SyncMetricCard[syncState="syncing"] {{
    background: #10271A;
    border-color: {c['accent_hover']};
}}

QFrame#SyncMetricCard[syncState="complete"] QLabel#SyncMetricValue,
QFrame#SyncMetricCard[syncState="syncing"] QLabel#SyncMetricValue {{
    color: {c['accent_hover']};
}}

QFrame#SyncMetricCard[syncState="complete_with_issues"] QLabel#SyncMetricValue {{
    color: {c['warning']};
}}

QFrame#SyncMetricCard[syncState="failed"] QLabel#SyncMetricValue {{
    color: {c['danger']};
}}

QFrame#StatCard,
QFrame#SyncMetricCard {{
    min-height: 68px;
}}

QFrame#Divider {{
    background: {c['border']};
    border: none;
}}

QFrame#PlayerBar {{
    background: {c['sidebar_background']};
    border: 1px solid {c['strong_border']};
    border-radius: {r['panel']}px;
}}

QLabel#CoverArt {{
    background: {c['subtle_surface']};
    border: 1px solid {c['border']};
    border-radius: {r['artwork']}px;
    color: {c['accent']};
}}

QListView#MediaGridView {{
    background: transparent;
    border: none;
    outline: none;
    padding: 0;
    selection-background-color: transparent;
}}

QListView#MediaGridView::item,
QListView#MediaGridView::item:hover,
QListView#MediaGridView::item:selected,
QListView#MediaGridView::item:focus {{
    background: transparent;
    border: none;
    outline: none;
    padding: 0;
}}

QFrame#EmptyState {{
    background: {c['subtle_surface']};
    border: 1px dashed {c['strong_border']};
    border-radius: {r['card']}px;
}}

QLabel#EmptyStateIcon {{
    color: {c['text_muted']};
}}

QLabel#EmptyStateTitle {{
    color: {c['text_primary']};
    font-size: {t['section_title_size']}px;
    font-weight: 650;
}}

QPushButton {{
    min-height: 36px;
    background: {c['elevated_surface']};
    border: 1px solid {c['strong_border']};
    border-radius: {r['control']}px;
    color: {c['text_primary']};
    padding: 0 14px;
    font-size: {t['button_size']}px;
    font-weight: 600;
}}

QPushButton:hover {{
    background: {c['hover_surface']};
    border-color: {c['scrollbar_hover']};
}}

QPushButton:pressed {{
    background: {c['pressed_surface']};
    border-color: {c['accent_pressed']};
}}

QPushButton:focus {{
    border: 2px solid {c['focus_ring']};
}}

QPushButton:disabled {{
    background: {c['subtle_surface']};
    border-color: {c['border']};
    color: {c['disabled_text']};
}}

QPushButton#PrimaryButton,
QPushButton[variant="primary"] {{
    background: {c['accent']};
    border-color: {c['accent']};
    color: {c['accent_ink']};
    font-weight: 700;
}}

QPushButton#PrimaryButton:hover,
QPushButton[variant="primary"]:hover {{
    background: {c['accent_hover']};
    border-color: {c['accent_hover']};
}}

QPushButton#PrimaryButton:pressed,
QPushButton[variant="primary"]:pressed {{
    background: {c['accent_pressed']};
    border-color: {c['accent_pressed']};
}}

QPushButton#SoftButton,
QPushButton[variant="secondary"] {{
    background: {c['elevated_surface']};
    border-color: {c['border']};
}}

QPushButton#DangerButton,
QPushButton[variant="danger"] {{
    background: transparent;
    border-color: {c['danger']};
    color: {c['danger']};
}}

QPushButton#DangerButton:hover,
QPushButton[variant="danger"]:hover {{
    background: #35171B;
    border-color: {c['danger_hover']};
    color: {c['danger_hover']};
}}

QPushButton#SidebarButton {{
    min-height: 40px;
    text-align: left;
    background: transparent;
    border-color: transparent;
    color: {c['text_secondary']};
    padding: 0 12px;
}}

QPushButton#SidebarButton:hover {{
    background: {c['hover_surface']};
    color: {c['text_primary']};
}}

QPushButton#SidebarButton:checked,
QPushButton#SidebarButton[active="true"] {{
    background: {c['selection']};
    border-color: {c['accent_pressed']};
    color: {c['now_playing']};
}}

QPushButton#IconButton,
QPushButton#OverflowActionButton,
QPushButton#CircleButton {{
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    max-height: 36px;
    padding: 0;
    border-radius: 18px;
    background: transparent;
    border-color: transparent;
}}

QPushButton#IconButton:hover,
QPushButton#OverflowActionButton:hover,
QPushButton#CircleButton:hover {{
    background: {c['hover_surface']};
    border-color: {c['border']};
}}

QPushButton#IconButton:pressed,
QPushButton#OverflowActionButton:pressed,
QPushButton#CircleButton:pressed {{
    background: {c['pressed_surface']};
}}

QPushButton#PlayButton {{
    min-width: 48px;
    max-width: 48px;
    min-height: 48px;
    max-height: 48px;
    padding: 0;
    border-radius: 24px;
    background: {c['text_primary']};
    border: none;
    color: {c['app_background']};
}}

QPushButton#PlayButton:hover {{
    background: {c['accent_hover']};
}}

QPushButton#ModeButton,
QPushButton#ModeButtonActive {{
    min-height: 30px;
    padding: 0 6px;
    border-radius: 9px;
    background: transparent;
    border-color: transparent;
    color: {c['text_muted']};
    font-size: {t['caption_size']}px;
}}

QPushButton#ModeButton:hover {{
    background: {c['hover_surface']};
    color: {c['text_primary']};
}}

QPushButton#ModeButtonActive,
QPushButton[active="true"] {{
    background: {c['selection']};
    border-color: {c['accent_pressed']};
    color: {c['now_playing']};
}}

QPushButton::menu-indicator {{
    image: none;
    width: 0;
}}

QLineEdit,
QLineEdit#SearchBox,
QLineEdit#SearchField {{
    min-height: 40px;
    background: {c['subtle_surface']};
    border: 1px solid {c['border']};
    border-radius: {r['control']}px;
    color: {c['text_primary']};
    padding: 0 12px;
    selection-background-color: {c['accent']};
    selection-color: {c['accent_ink']};
}}

QLineEdit:hover,
QLineEdit#SearchField:hover {{
    border-color: {c['strong_border']};
}}

QLineEdit:focus,
QLineEdit#SearchField:focus {{
    border: 2px solid {c['focus_ring']};
}}

QLineEdit:disabled {{
    background: {c['subtle_surface']};
    color: {c['disabled_text']};
}}

QTextEdit#SyncLog,
QTextEdit {{
    background: {c['subtle_surface']};
    border: 1px solid {c['border']};
    border-radius: {r['control']}px;
    color: {c['text_secondary']};
    padding: 10px;
    selection-background-color: {c['selection_hover']};
}}

QTextEdit:focus {{
    border: 2px solid {c['focus_ring']};
}}

QListWidget#PlaylistList {{
    background: transparent;
    border: none;
    outline: none;
    padding: 2px;
}}

QListWidget#PlaylistList::item {{
    min-height: 34px;
    border-radius: 9px;
    color: {c['text_secondary']};
    padding: 3px 10px;
}}

QListWidget#PlaylistList::item:hover {{
    background: {c['hover_surface']};
    color: {c['text_primary']};
}}

QListWidget#PlaylistList::item:selected {{
    background: {c['selection']};
    color: {c['text_primary']};
}}

QTableWidget#LibraryTable,
QTableView {{
    background: {c['subtle_surface']};
    alternate-background-color: #0C1219;
    border: 1px solid {c['border']};
    border-radius: {r['card']}px;
    gridline-color: transparent;
    selection-background-color: {c['selection']};
    selection-color: {c['text_primary']};
    outline: none;
}}

QTableWidget#LibraryTable::item,
QTableView::item {{
    border: none;
    padding: 8px 10px;
}}

QTableWidget#LibraryTable::item:hover,
QTableView::item:hover {{
    background: {c['hover_surface']};
}}

QTableWidget#LibraryTable::item:selected,
QTableView::item:selected {{
    background: {c['selection']};
    color: {c['text_primary']};
}}

QTableWidget#LibraryTable:focus {{
    border: 1px solid {c['focus_ring']};
}}

QHeaderView::section {{
    background: {c['elevated_surface']};
    color: {c['text_muted']};
    border: none;
    border-bottom: 1px solid {c['border']};
    padding: 10px;
    font-size: {t['caption_size']}px;
    font-weight: 650;
}}

QScrollArea#BrowserScroll,
QScrollArea {{
    background: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QAbstractScrollArea::corner {{
    background: transparent;
}}

QScrollBar:vertical {{
    width: 10px;
    margin: 2px;
    background: transparent;
}}

QScrollBar::handle:vertical {{
    min-height: 32px;
    background: {c['scrollbar']};
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover,
QScrollBar::handle:vertical:pressed {{
    background: {c['scrollbar_hover']};
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
    background: transparent;
    border: none;
}}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: transparent;
}}

QScrollBar:horizontal {{
    height: 10px;
    margin: 2px;
    background: transparent;
}}

QScrollBar::handle:horizontal {{
    min-width: 32px;
    background: {c['scrollbar']};
    border-radius: 4px;
}}

QScrollBar::handle:horizontal:hover,
QScrollBar::handle:horizontal:pressed {{
    background: {c['scrollbar_hover']};
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0px;
    background: transparent;
    border: none;
}}

QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

QMenu {{
    background: {c['elevated_surface']};
    border: 1px solid {c['strong_border']};
    border-radius: {r['control']}px;
    padding: 6px;
    color: {c['text_primary']};
}}

QMenu::item {{
    min-height: 30px;
    border-radius: 7px;
    padding: 4px 28px 4px 10px;
}}

QMenu::item:selected {{
    background: {c['hover_surface']};
}}

QMenu::item:disabled {{
    color: {c['disabled_text']};
}}

QMenu::separator {{
    height: 1px;
    background: {c['border']};
    margin: 5px 8px;
}}

QToolTip {{
    background: {c['elevated_surface']};
    color: {c['text_primary']};
    border: 1px solid {c['strong_border']};
    border-radius: 7px;
    padding: 6px 8px;
}}

QCheckBox {{
    color: {c['text_secondary']};
    spacing: 9px;
}}

QCheckBox:focus {{
    color: {c['text_primary']};
}}

QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    background: {c['subtle_surface']};
    border: 1px solid {c['strong_border']};
    border-radius: 5px;
}}

QCheckBox::indicator:hover {{
    border-color: {c['focus_ring']};
}}

QCheckBox::indicator:checked {{
    background: {c['accent']};
    border-color: {c['accent']};
    image: url("{check_icon}");
}}

QComboBox {{
    min-height: 38px;
    background: {c['subtle_surface']};
    border: 1px solid {c['border']};
    border-radius: {r['control']}px;
    color: {c['text_primary']};
    padding: 0 32px 0 12px;
}}

QComboBox:hover {{
    border-color: {c['strong_border']};
}}

QComboBox:focus {{
    border: 2px solid {c['focus_ring']};
}}

QComboBox::drop-down {{
    width: 28px;
    border: none;
}}

QComboBox::down-arrow {{
    image: url("{chevron_icon}");
    width: 12px;
    height: 12px;
    background: {c['text_muted']};
    border-radius: 6px;
}}

QComboBox QAbstractItemView {{
    background: {c['elevated_surface']};
    border: 1px solid {c['strong_border']};
    color: {c['text_primary']};
    selection-background-color: {c['selection']};
    selection-color: {c['text_primary']};
    outline: none;
}}

QProgressBar {{
    min-height: 18px;
    max-height: 18px;
    background: {c['subtle_surface']};
    border: 1px solid {c['border']};
    border-radius: 9px;
    color: {c['text_primary']};
    text-align: center;
}}

QProgressBar::chunk {{
    background: {c['accent']};
    border-radius: 7px;
}}

QProgressBar#SyncProgress[syncState="complete"] {{
    border-color: {c['accent']};
    color: {c['accent_ink']};
}}

QProgressBar#SyncProgress[syncState="complete_with_issues"] {{
    border-color: {c['warning']};
    color: {c['accent_ink']};
}}

QProgressBar#SyncProgress[syncState="failed"] {{
    border-color: {c['danger']};
}}

QProgressBar#SyncProgress[syncState="syncing"] {{
    border-color: {c['accent_hover']};
}}

QProgressBar#SyncProgress[syncState="complete"]::chunk,
QProgressBar#SyncProgress[syncState="syncing"]::chunk {{
    background: {c['accent']};
}}

QProgressBar#SyncProgress[syncState="complete_with_issues"]::chunk {{
    background: {c['warning']};
}}

QProgressBar#SyncProgress[syncState="failed"]::chunk {{
    background: {c['danger']};
}}

QTextEdit#SyncLog[syncState="complete"],
QTextEdit#SyncLog[syncState="syncing"] {{
    border-color: {c['accent']};
}}

QTextEdit#SyncLog[syncState="complete_with_issues"] {{
    border-color: {c['warning']};
}}

QTextEdit#SyncLog[syncState="failed"] {{
    border-color: {c['danger']};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {c['strong_border']};
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {c['accent']};
    border-radius: 2px;
}}

QSlider::add-page:horizontal {{
    background: {c['strong_border']};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    width: 12px;
    height: 12px;
    margin: -5px 0;
    background: {c['text_primary']};
    border: 1px solid {c['accent']};
    border-radius: 6px;
}}

QSlider::handle:horizontal:hover {{
    width: 14px;
    height: 14px;
    margin: -6px 0;
    background: {c['accent_hover']};
}}

QSlider::groove:horizontal:focus {{
    height: 6px;
    border: 1px solid {c['focus_ring']};
    border-radius: 3px;
}}

QSlider::handle:horizontal:focus {{
    border: 2px solid {c['focus_ring']};
}}

QDialog#MetadataEditorDialog {{
    background: {c['app_background']};
}}

QMessageBox {{
    background: {c['card_surface']};
}}

QMessageBox QLabel {{
    background: transparent;
    color: {c['text_primary']};
}}

QDialog#MetadataEditorDialog QTabWidget::pane {{
    border: 1px solid {c['border']};
    border-radius: 10px;
    background: {c['card_surface']};
    top: -1px;
}}

QDialog#MetadataEditorDialog QTabBar::tab {{
    background: transparent;
    color: {c['text_secondary']};
    padding: 9px 14px;
    border-bottom: 2px solid transparent;
}}

QDialog#MetadataEditorDialog QTabBar::tab:selected {{
    color: {c['text_primary']};
    border-bottom-color: {c['accent']};
}}

QFrame#MetadataFieldCard {{
    background: {c['elevated_surface']};
    border: 1px solid {c['border']};
    border-radius: 9px;
}}

QLabel#MetadataFieldLabel {{
    color: {c['text_primary']};
    font-size: 13px;
    font-weight: 700;
}}

QLabel#MetadataBadge,
QLabel#MetadataLockBadgeLocked {{
    border-radius: 8px;
    padding: 3px 7px;
    font-size: 10px;
    font-weight: 700;
}}

QLabel#MetadataBadge {{
    color: {c['text_secondary']};
    background: {c['hover_surface']};
    border: 1px solid {c['border']};
}}

QLabel#MetadataLockBadgeLocked {{
    color: {c['accent_hover']};
    background: {c['selection']};
    border: 1px solid {c['accent']};
}}

QLabel#MetadataInfoBanner {{
    color: {c['text_secondary']};
    background: {c['hover_surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 10px;
}}

QLabel#MetadataArtworkPreview {{
    background: {c['subtle_surface']};
    border: 1px solid {c['strong_border']};
    border-radius: 10px;
}}

QLabel#ErrorLabel {{
    color: {c['danger']};
    font-weight: 600;
}}
""".strip()


def build_stylesheet() -> str:
    """Compatibility alias for callers that describe stylesheet generation."""

    return application_stylesheet()


def repolish(widget: object) -> None:
    """Refresh QSS selectors after a widget dynamic property changes."""

    try:
        style = widget.style()  # type: ignore[attr-defined]
        style.unpolish(widget)
        style.polish(widget)
        widget.update()  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        return


def apply_dark_title_bar(widget: object) -> bool:
    """Ask Windows DWM for a dark native title bar, or safely do nothing."""

    if sys.platform != "win32":
        return False

    try:
        import ctypes

        hwnd = int(widget.winId())  # type: ignore[attr-defined]
        if not hwnd:
            return False
        dwmapi = ctypes.windll.dwmapi
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attribute),
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return False
