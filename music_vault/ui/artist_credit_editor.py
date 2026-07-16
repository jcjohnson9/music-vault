from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from music_vault.metadata.artist_credits import (
    ArtistCreditInput,
    ArtistCreditService,
    TrackArtistCredit,
)
from music_vault.metadata.intelligence_schema import ARTIST_CREDIT_ROLES, ARTIST_ENTITY_TYPES


def _friendly(value: str) -> str:
    return value.replace("_", " ").title()


def _join_credit_names(credits: Sequence[ArtistCreditInput]) -> str:
    parts: list[str] = []
    for index, credit in enumerate(credits):
        name = credit.display_name.strip()
        if not name:
            continue
        if index == 0:
            parts.append(name)
            continue
        join = credit.join_phrase or ", "
        if join in {",", "/", "&", "x"}:
            join = ", " if join == "," else f" {join} "
        elif join and not join[0].isspace():
            join = f" {join} "
        parts.append(f"{join}{name}")
    return "".join(parts).strip()


class ArtistCreditEditor(QGroupBox):
    """Compact, ordered editor for a track's structured artist credits."""

    credits_changed = Signal()

    ORDER_COLUMN = 0
    NAME_COLUMN = 1
    ENTITY_COLUMN = 2
    ROLE_COLUMN = 3
    JOIN_COLUMN = 4
    DISCOGS_COLUMN = 5
    MUSICBRAINZ_COLUMN = 6

    def __init__(
        self,
        service: ArtistCreditService,
        track_id: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Structured artist credits", parent)
        self.setObjectName("ArtistCreditEditor")
        self.service = service
        self.track_id = int(track_id)
        self._loading = False
        self._baseline: tuple[tuple[object, ...], ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        note = QLabel(
            "Primary, featured, collaborator, remixer, and performer credits stay ordered. "
            "Manual saves are locked by default."
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = QTableWidget(0, 7)
        self.table.setObjectName("ArtistCreditTable")
        self.table.setHorizontalHeaderLabels(
            ["#", "Artist", "Entity", "Role", "Join", "Discogs ID", "MusicBrainz ID"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.ORDER_COLUMN, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.ORDER_COLUMN, 44)
        self.table.setColumnWidth(self.NAME_COLUMN, 240)
        self.table.setColumnWidth(self.ENTITY_COLUMN, 140)
        self.table.setColumnWidth(self.ROLE_COLUMN, 140)
        self.table.setColumnWidth(self.JOIN_COLUMN, 160)
        self.table.setColumnWidth(self.DISCOGS_COLUMN, 120)
        header.setSectionResizeMode(
            self.MUSICBRAINZ_COLUMN, QHeaderView.ResizeMode.Stretch
        )
        self.table.setMinimumHeight(170)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add Credit")
        self.add_button.setObjectName("GhostButton")
        self.add_button.clicked.connect(self.add_credit)
        self.remove_button = QPushButton("Remove Credit")
        self.remove_button.setObjectName("GhostButton")
        self.remove_button.clicked.connect(self.remove_selected_credit)
        self.up_button = QPushButton("Move Up")
        self.up_button.setObjectName("GhostButton")
        self.up_button.clicked.connect(lambda: self.move_selected_credit(-1))
        self.down_button = QPushButton("Move Down")
        self.down_button.setObjectName("GhostButton")
        self.down_button.clicked.connect(lambda: self.move_selected_credit(1))
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status_label = QLabel("")
        self.status_label.setObjectName("MutedLabel")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.load_credits()

    @staticmethod
    def _readonly_id(value: str | None, object_name: str) -> QLabel:
        label = QLabel(value or "Not available")
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setProperty("provider_id", value or "")
        return label

    @staticmethod
    def _combo(values: Sequence[str], current: str, object_name: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(object_name)
        for value in values:
            combo.addItem(_friendly(value), value)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else combo.findData("unknown"))
        return combo

    def _append_row(
        self,
        credit: TrackArtistCredit | ArtistCreditInput,
        *,
        select: bool = False,
    ) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        order = QTableWidgetItem(str(row + 1))
        order.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        order.setFlags(order.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, self.ORDER_COLUMN, order)

        if isinstance(credit, TrackArtistCredit):
            name = credit.artist.display_name
            entity_type = credit.artist.entity_type
            role = credit.role
            join_phrase = credit.join_phrase
            discogs_id = credit.artist.discogs_artist_id
            musicbrainz_id = credit.artist.musicbrainz_artist_id
        else:
            name = credit.display_name
            entity_type = credit.entity_type
            role = credit.role
            join_phrase = credit.join_phrase
            discogs_id = credit.discogs_artist_id
            musicbrainz_id = credit.musicbrainz_artist_id

        name_edit = QLineEdit(name)
        name_edit.setMinimumWidth(170)
        name_edit.setObjectName("ArtistCreditName")
        name_edit.setAccessibleName(f"Artist credit {row + 1} name")
        name_edit.setProperty("original_name", name)
        entity_combo = self._combo(ARTIST_ENTITY_TYPES, entity_type, "ArtistCreditEntityType")
        role_combo = self._combo(ARTIST_CREDIT_ROLES, role, "ArtistCreditRole")
        join_edit = QLineEdit(join_phrase)
        join_edit.setMinimumWidth(110)
        join_edit.setObjectName("ArtistCreditJoinPhrase")
        join_edit.setMaxLength(80)
        join_edit.setPlaceholderText("feat., &, with, x")
        discogs_label = self._readonly_id(discogs_id, "ArtistCreditDiscogsId")
        musicbrainz_label = self._readonly_id(musicbrainz_id, "ArtistCreditMusicBrainzId")

        self.table.setCellWidget(row, self.NAME_COLUMN, name_edit)
        self.table.setCellWidget(row, self.ENTITY_COLUMN, entity_combo)
        self.table.setCellWidget(row, self.ROLE_COLUMN, role_combo)
        self.table.setCellWidget(row, self.JOIN_COLUMN, join_edit)
        self.table.setCellWidget(row, self.DISCOGS_COLUMN, discogs_label)
        self.table.setCellWidget(row, self.MUSICBRAINZ_COLUMN, musicbrainz_label)

        name_edit.textChanged.connect(self._row_changed)
        entity_combo.currentIndexChanged.connect(self._row_changed)
        role_combo.currentIndexChanged.connect(self._row_changed)
        join_edit.textChanged.connect(self._row_changed)
        if select:
            self.table.selectRow(row)
        return row

    def load_credits(self, credits: Sequence[TrackArtistCredit] | None = None) -> None:
        self._loading = True
        try:
            self.table.setRowCount(0)
            for credit in credits if credits is not None else self.service.track_credits(self.track_id):
                self._append_row(credit)
            self._renumber()
            self._baseline = self._signature()
            self.status_label.setText(
                f"{self.table.rowCount()} structured credit"
                f"{'s' if self.table.rowCount() != 1 else ''}."
            )
        finally:
            self._loading = False

    def add_credit(self) -> None:
        role = "primary" if self.table.rowCount() == 0 else "featured"
        self._append_row(ArtistCreditInput("", role=role), select=True)
        name = self.table.cellWidget(self.table.currentRow(), self.NAME_COLUMN)
        if isinstance(name, QLineEdit):
            name.setFocus()
        self._changed()

    def remove_selected_credit(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self.status_label.setText("Select a credit to remove.")
            return
        self.table.removeRow(row)
        self._renumber()
        if self.table.rowCount():
            self.table.selectRow(min(row, self.table.rowCount() - 1))
        self._changed()

    def move_selected_credit(self, offset: int) -> None:
        row = self.table.currentRow()
        target = row + int(offset)
        if row < 0 or target < 0 or target >= self.table.rowCount():
            return
        drafts = [
            self._row_input(index, allow_blank=True)
            for index in range(self.table.rowCount())
        ]
        drafts[row], drafts[target] = drafts[target], drafts[row]
        self._loading = True
        try:
            self.table.setRowCount(0)
            for draft in drafts:
                self._append_row(draft)
            self._renumber()
            self.table.selectRow(target)
        finally:
            self._loading = False
        self._changed()

    def _renumber(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.ORDER_COLUMN)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, self.ORDER_COLUMN, item)
            item.setText(str(row + 1))

    def _row_changed(self, *_args: object) -> None:
        self._changed()

    def _changed(self) -> None:
        if self._loading:
            return
        self.status_label.setText("Manual credit changes ready; Save locks the credit set.")
        self.credits_changed.emit()

    def _row_input(self, row: int, *, allow_blank: bool) -> ArtistCreditInput:
        name_edit = self.table.cellWidget(row, self.NAME_COLUMN)
        entity_combo = self.table.cellWidget(row, self.ENTITY_COLUMN)
        role_combo = self.table.cellWidget(row, self.ROLE_COLUMN)
        join_edit = self.table.cellWidget(row, self.JOIN_COLUMN)
        discogs_label = self.table.cellWidget(row, self.DISCOGS_COLUMN)
        musicbrainz_label = self.table.cellWidget(row, self.MUSICBRAINZ_COLUMN)
        if not (
            isinstance(name_edit, QLineEdit)
            and isinstance(entity_combo, QComboBox)
            and isinstance(role_combo, QComboBox)
            and isinstance(join_edit, QLineEdit)
            and isinstance(discogs_label, QLabel)
            and isinstance(musicbrainz_label, QLabel)
        ):
            raise RuntimeError("The artist-credit editor row is incomplete.")
        name = name_edit.text().strip()
        if not name and not allow_blank:
            raise ValueError(f"Artist credit {row + 1} requires a name.")
        original_name = str(name_edit.property("original_name") or "")
        identity_unchanged = bool(name) and name == original_name
        return ArtistCreditInput(
            name,
            role=str(role_combo.currentData() or "primary"),
            join_phrase=join_edit.text(),
            entity_type=str(entity_combo.currentData() or "unknown"),
            discogs_artist_id=(
                str(discogs_label.property("provider_id")) or None
                if identity_unchanged
                else None
            ),
            musicbrainz_artist_id=(
                str(musicbrainz_label.property("provider_id")) or None
                if identity_unchanged
                else None
            ),
        )

    def credit_inputs(
        self,
        *,
        require_primary: bool = True,
        allow_blank: bool = False,
    ) -> tuple[ArtistCreditInput, ...]:
        credits = tuple(
            self._row_input(row, allow_blank=allow_blank)
            for row in range(self.table.rowCount())
        )
        populated = tuple(credit for credit in credits if credit.display_name.strip())
        if require_primary and (
            not populated or not any(credit.role == "primary" for credit in populated)
        ):
            raise ValueError("At least one primary artist credit is required.")
        return populated

    def display_artist(self, *, allow_incomplete: bool = False) -> str:
        credits = self.credit_inputs(
            require_primary=not allow_incomplete,
            allow_blank=allow_incomplete,
        )
        return _join_credit_names(credits)

    def _signature(self) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                credit.display_name,
                credit.entity_type,
                credit.role,
                credit.join_phrase,
                credit.discogs_artist_id,
                credit.musicbrainz_artist_id,
            )
            for credit in self.credit_inputs(require_primary=False, allow_blank=True)
        )

    def is_dirty(self) -> bool:
        return self._signature() != self._baseline

    def save_manual(self, *, commit: bool = True) -> tuple[TrackArtistCredit, ...]:
        stored = self.service.replace_track_credits(
            self.track_id,
            self.credit_inputs(),
            provenance="manual",
            is_manual=True,
            is_locked=True,
            actor="user",
            reason="manual_artist_credit_edit",
            commit=commit,
        )
        self.load_credits(stored)
        return stored


__all__ = ["ArtistCreditEditor"]
