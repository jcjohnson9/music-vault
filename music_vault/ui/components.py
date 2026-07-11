from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from music_vault.ui.icons import ui_icon
from music_vault.ui.theme import COLORS, repolish


class IconButton(QPushButton):
    """Accessible icon button with cached Music Vault icon variants."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        accessible_name: str | None = None,
        *,
        size: int = 20,
        variant: str = "secondary",
        text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._icon_name = icon_name
        self._logical_icon_size = max(1, int(size))
        self.setObjectName("IconButton")
        self.setProperty("variant", variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setAccessibleName(accessible_name or tooltip or text or icon_name.replace("-", " "))
        self.setIconSize(QSize(self._logical_icon_size, self._logical_icon_size))
        self.refresh_icon()

    @property
    def icon_name(self) -> str:
        return self._icon_name

    def set_icon_name(self, name: str) -> None:
        if name == self._icon_name:
            return
        self._icon_name = name
        self.refresh_icon()

    def set_variant(self, variant: str) -> None:
        self.setProperty("variant", variant)
        repolish(self)

    def refresh_icon(self) -> None:
        primary = self.property("variant") == "primary"
        normal = COLORS["accent_ink"] if primary else COLORS["text_secondary"]
        active = COLORS["accent_ink"] if primary else COLORS["accent_hover"]
        self.setIcon(
            ui_icon(
                self._icon_name,
                self._logical_icon_size,
                color=normal,
                disabled_color=COLORS["disabled_text"],
                active_color=active,
            )
        )


class ElidedLabel(QLabel):
    """Single-line label that keeps its full accessible text while eliding."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        self._full_text = ""
        self._elide_mode = Qt.TextElideMode.ElideRight
        super().__init__("", parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setObjectName("ElidedLabel")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def fullText(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API spelling
        self._full_text = str(text)
        self.setAccessibleName(self._full_text)
        self._update_elision()

    def setElideMode(self, mode: Qt.TextElideMode) -> None:  # noqa: N802
        self._elide_mode = mode
        self._update_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        width = max(0, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(self._full_text, self._elide_mode, width)
        QLabel.setText(self, shown)
        self.setToolTip(
            f"<qt>{html.escape(self._full_text)}</qt>"
            if shown != self._full_text
            else ""
        )


class SearchField(QLineEdit):
    """Consistent search input with a leading icon and Escape-to-clear."""

    def __init__(self, placeholder: str = "Search", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SearchField")
        self.setPlaceholderText(placeholder)
        self.setAccessibleName(placeholder)
        self._search_action = self.addAction(
            ui_icon("search", 18),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self._search_action.setToolTip("Search")
        self.setClearButtonEnabled(True)

    @property
    def search_action(self) -> QAction:
        return self._search_action

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)


class OverflowActionButton(IconButton):
    """Compact overflow button with an inspectable action registry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "more",
            "More actions",
            "More actions",
            size=20,
            variant="secondary",
            parent=parent,
        )
        self.setObjectName("OverflowActionButton")
        self._menu = QMenu(self)
        self._actions: dict[str, QAction] = {}
        self.setMenu(self._menu)

    def add_action(
        self,
        text: str,
        icon_name: str,
        callback: Callable[[], object],
        destructive: bool = False,
    ) -> QAction:
        label = str(text)
        if label in self._actions:
            raise ValueError(f"Duplicate overflow action: {label}")
        color = COLORS["danger"] if destructive else COLORS["text_secondary"]
        active = COLORS["danger_hover"] if destructive else COLORS["accent_hover"]
        action = QAction(
            ui_icon(icon_name, 18, color=color, active_color=active),
            label,
            self,
        )
        action.setProperty("destructive", destructive)
        action.setToolTip(label)
        action.triggered.connect(lambda _checked=False: callback())
        self._actions[label] = action
        self._menu.addAction(action)
        return action

    def action_texts(self) -> list[str]:
        return list(self._actions)

    def action(self, text: str) -> QAction | None:
        return self._actions.get(text)


class EmptyState(QFrame):
    """Reusable concise empty/error state with an optional next action."""

    def __init__(
        self,
        icon_name: str,
        title: str,
        description: str,
        action_text: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        self.setAccessibleName(title)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("EmptyStateIcon")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(
            ui_icon(icon_name, 32, COLORS["text_muted"]).pixmap(QSize(32, 32))
        )

        self.title_label = QLabel(title)
        self.title_label.setObjectName("EmptyStateTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.description_label = QLabel(description)
        self.description_label.setObjectName("EmptyStateDescription")
        self.description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description_label.setWordWrap(True)
        self.description_label.setMaximumWidth(460)

        self.action_button: QPushButton | None = None
        if action_text:
            self.action_button = QPushButton(action_text)
            self.action_button.setObjectName("PrimaryButton")
            self.action_button.setAccessibleName(action_text)

        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.description_label)
        if self.action_button is not None:
            layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignCenter)


class SectionHeader(QFrame):
    """Aligned title/subtitle header with a reusable trailing-action area."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SectionHeader")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)

        self.title_label = ElidedLabel(title)
        self.title_label.setObjectName("SectionHeaderTitle")
        self.subtitle_label = ElidedLabel(subtitle)
        self.subtitle_label.setObjectName("SectionHeaderSubtitle")
        self.subtitle_label.setVisible(bool(subtitle))

        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.subtitle_label)
        root.addLayout(text_layout, 1)

        self.action_layout = QHBoxLayout()
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(8)
        root.addLayout(self.action_layout)

    def add_action(self, widget: QWidget) -> None:
        self.action_layout.addWidget(widget)
