from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from music_vault.core.safety import sanitize_error_text
from music_vault.metadata.review_policy import classify_stored_review_evidence
from music_vault.metadata.schema import EDITABLE_METADATA_FIELDS


REVIEW_FILTERS = (
    ("All Outcomes", None),
    ("Applied", "applied"),
    ("Applied with Gaps", "applied_with_gaps"),
    ("Accepted Source Fallback", "source_fallback"),
    ("Failed", "failed"),
    ("No Match", "no_match"),
    ("Skipped", "skipped"),
    ("Provider Disagreement", "provider_disagreement"),
    ("Version Conflict", "version_conflict"),
    ("Album Ambiguity", "album_ambiguity"),
    ("Date Ambiguity", "date_ambiguity"),
    ("Artist Ambiguity", "artist_ambiguity"),
    ("YouTube Exclusive", "youtube_exclusive"),
)

_STATE_LABELS = {
    "applied": "Applied",
    "applied_with_gaps": "Applied with Gaps",
    "source_fallback": "Accepted Source Fallback",
    "review": "Legacy Pending",
    "ready": "Legacy Pending",
    "failed": "Failed",
    "no_match": "No Match",
    "skipped": "Skipped",
}

_GAP_LABELS = {
    "album": "Album unavailable",
    "album_artist": "Album artist unavailable",
    "release_date": "Release year unavailable",
    "original_release_date": "Original release unavailable",
    "artwork": "Artwork unavailable",
    "exact_edition": "Exact edition unresolved",
    "label": "Label unavailable",
    "catalog_number": "Catalogue number unavailable",
}

_DISCOGS_HOME_URL = "https://www.discogs.com/"
_DISCOGS_PAGE_HOSTS = frozenset({"discogs.com", "www.discogs.com"})
_DISCOGS_ID_RE = re.compile(r"[1-9]\d*")
_DISCOGS_PAGE_RE = re.compile(
    r"/(release|master|artist)/([1-9]\d*)(?:-[^/?#]+)?/?"
)


def _discogs_id(value: object) -> str | None:
    identity = str(value or "").strip()
    return identity if _DISCOGS_ID_RE.fullmatch(identity) else None


def _discogs_attribution_url(
    provider_reference: object,
    *,
    release_id: object = None,
    master_id: object = None,
    artist_id: object = None,
) -> str:
    """Return only a canonical Discogs page whose type and ID correspond."""

    identities = {
        "release": _discogs_id(release_id),
        "master": _discogs_id(master_id),
        "artist": _discogs_id(artist_id),
    }
    try:
        parsed = urlsplit(str(provider_reference or "").strip())
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if parsed is not None:
        host = (parsed.hostname or "").rstrip(".").casefold()
        match = _DISCOGS_PAGE_RE.fullmatch(parsed.path)
        if (
            parsed.scheme.casefold() == "https"
            and host in _DISCOGS_PAGE_HOSTS
            and port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and match is not None
            and identities.get(match.group(1)) == match.group(2)
        ):
            kind, identity = match.group(1), match.group(2)
            return f"https://www.discogs.com/{kind}/{identity}"
    for kind in ("release", "master", "artist"):
        if identities[kind] is not None:
            return f"https://www.discogs.com/{kind}/{identities[kind]}"
    return _DISCOGS_HOME_URL


def _decoded(value: object) -> Mapping[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _plain(value: object, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text if text else fallback


class MetadataIntelligenceDialog(QDialog):
    """Aggregate, token-free review surface for resumable metadata jobs."""

    edit_track_requested = Signal(int)
    review_applied = Signal(int)
    resume_requested = Signal(str, str)

    def __init__(self, db, service=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.service = service
        self.setObjectName("MetadataIntelligenceDialog")
        self.setWindowTitle("Metadata Intelligence • Music Vault")
        self.resize(1120, 700)
        self.setMinimumSize(820, 560)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        title = QLabel("Metadata Intelligence")
        title.setObjectName("PageTitle")
        description = QLabel(
            "Inspect automatic best-available outcomes, provider provenance, "
            "version/date gaps, and source fallbacks. Rare mistakes can still be "
            "corrected in the metadata editor. Personal provider credentials are "
            "never shown here."
        )
        description.setObjectName("MutedLabel")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Filter"))
        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("MetadataIntelligenceFilter")
        for label, value in REVIEW_FILTERS:
            self.filter_combo.addItem(label, value)
        default_index = self.filter_combo.findData(None)
        if default_index >= 0:
            self.filter_combo.setCurrentIndex(default_index)
        self.filter_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.filter_combo)
        self.show_incomplete_checkbox = QCheckBox("Include incomplete outcomes")
        self.show_incomplete_checkbox.setObjectName("MetadataShowIncomplete")
        self.show_incomplete_checkbox.setToolTip(
            "Include Applied with Gaps when an audit filter is selected."
        )
        self.show_incomplete_checkbox.toggled.connect(self.refresh)
        controls.addWidget(self.show_incomplete_checkbox)
        controls.addStretch(1)
        for text, callback, object_name in (
            ("Pause", self.pause_job, "GhostButton"),
            ("Resume", self.resume_job, "GhostButton"),
            ("Cancel", self.cancel_job, "DangerButton"),
            ("Refresh", self.refresh, "PrimaryButton"),
        ):
            button = QPushButton(text)
            button.setObjectName(object_name)
            button.clicked.connect(callback)
            controls.addWidget(button)
        layout.addLayout(controls)

        self.summary = QLabel()
        self.summary.setObjectName("StatusLine")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 12)
        self.table.setObjectName("MetadataIntelligenceReviewTable")
        self.table.setHorizontalHeaderLabels(
            [
                "State",
                "Current Metadata",
                "YouTube Title Hint",
                "Uploader Provenance",
                "Discogs Proposal",
                "MusicBrainz",
                "Duration",
                "Version",
                "Release Choices",
                "Artwork",
                "Proposed Fields",
                "Decision Detail",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate(
            (180, 190, 190, 165, 190, 190, 175, 115, 170, 190, 175, 175)
        ):
            self.table.setColumnWidth(column, width)
        self.table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.table.doubleClicked.connect(self._edit_selected)
        self.table.itemSelectionChanged.connect(self._populate_field_choices)
        layout.addWidget(self.table, 1)

        field_group = QGroupBox("Optional Manual Correction")
        field_group.setObjectName("MetadataReviewFieldGroup")
        field_group_layout = QVBoxLayout(field_group)
        field_group_layout.setContentsMargins(10, 8, 10, 8)
        field_group_layout.setSpacing(6)
        self.field_choice_hint = QLabel(
            "Select a legacy pending row to choose individual proposed fields. "
            "Confirmed values are locked and retain metadata history."
        )
        self.field_choice_hint.setObjectName("MutedLabel")
        self.field_choice_hint.setWordWrap(True)
        field_group_layout.addWidget(self.field_choice_hint)
        field_choice_widget = QWidget()
        self.field_choice_layout = QGridLayout(field_choice_widget)
        self.field_choice_layout.setContentsMargins(0, 0, 0, 0)
        self.field_choice_layout.setHorizontalSpacing(12)
        self.field_choice_layout.setVerticalSpacing(4)
        field_group_layout.addWidget(field_choice_widget)
        self.field_checks: dict[str, QCheckBox] = {}
        self.apply_fields_button = QPushButton("Apply Selected Fields")
        self.apply_fields_button.setObjectName("PrimaryButton")
        self.apply_fields_button.setEnabled(False)
        self.apply_fields_button.clicked.connect(self._apply_selected_fields)
        field_actions = QHBoxLayout()
        field_actions.addStretch(1)
        field_actions.addWidget(self.apply_fields_button)
        field_group_layout.addLayout(field_actions)
        layout.addWidget(field_group)

        footer = QHBoxLayout()
        self.discogs_attribution_label = QLabel(
            '<a href="https://www.discogs.com/">Data provided by Discogs</a>'
        )
        self.discogs_attribution_label.setObjectName("MutedLabel")
        self.discogs_attribution_label.setTextFormat(Qt.TextFormat.RichText)
        self.discogs_attribution_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.discogs_attribution_label.setOpenExternalLinks(True)
        self.discogs_attribution_label.setAccessibleName("Data provided by Discogs")
        footer.addWidget(self.discogs_attribution_label)
        footer.addStretch(1)
        edit = QPushButton("Edit Selected Track")
        edit.setObjectName("PrimaryButton")
        edit.clicked.connect(self._edit_selected)
        close = QPushButton("Close")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.accept)
        footer.addWidget(edit)
        footer.addWidget(close)
        layout.addLayout(footer)

    def _columns(self, table: str) -> set[str]:
        try:
            return {str(row[1]) for row in self.db.conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            return set()

    def _latest_job(self):
        try:
            return self.db.conn.execute(
                "SELECT * FROM metadata_intelligence_jobs "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        except Exception:
            return None

    def _items(self, job_id: object | None) -> list:
        if job_id is None:
            return []
        columns = self._columns("metadata_intelligence_items")
        if not columns:
            return []
        state_column = "state" if "state" in columns else "status"
        clauses = ["job_id=?"]
        values: list[object] = [job_id]
        selected = self.filter_combo.currentData()
        if selected:
            if selected in {
                "applied",
                "applied_with_gaps",
                "source_fallback",
                "no_match",
                "failed",
                "skipped",
            }:
                clauses.append(f"{state_column}=?")
                values.append(selected)
            elif selected == "needs_review":
                states = "'review','ready'"
                if self.show_incomplete_checkbox.isChecked():
                    states += ",'applied_with_gaps'"
                clauses.append(f"{state_column} IN ({states})")
            elif "review_reason" in columns:
                clauses.append("review_reason=?")
                values.append(selected)
        try:
            return list(
                self.db.conn.execute(
                    "SELECT * FROM metadata_intelligence_items WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY id",
                    tuple(values),
                ).fetchall()
            )
        except Exception:
            return []

    @staticmethod
    def _row_value(row, *names: str) -> object:
        keys = set(row.keys())
        for name in names:
            if name in keys:
                return row[name]
        return None

    @staticmethod
    def _summary_text(value: object) -> str:
        if not isinstance(value, Mapping):
            return _plain(value)
        parts: list[str] = []
        artist = _plain(value.get("artist"), "")
        title = _plain(value.get("title"), "")
        album = _plain(value.get("album"), "")
        if artist and title:
            parts.append(f"{artist} — {title}")
        elif title or artist:
            parts.append(title or artist)
        if album:
            parts.append(album)
        score = value.get("score")
        if score not in (None, ""):
            parts.append(f"score {score}")
        return " • ".join(parts) if parts else "—"

    @staticmethod
    def _duration_text(current: Mapping[str, object], discogs: Mapping[str, object], musicbrainz: Mapping[str, object]) -> str:
        parts: list[str] = []
        for label, source in (
            ("Local", current),
            ("Discogs", discogs),
            ("MusicBrainz", musicbrainz),
        ):
            value = source.get("duration_seconds")
            if value not in (None, ""):
                parts.append(f"{label}: {value}s")
        return " • ".join(parts) if parts else "—"

    def refresh(self, *_args) -> None:
        job = self._latest_job()
        if job is None:
            self.summary.setText("No metadata-intelligence job has been started.")
            self.table.setRowCount(0)
            return
        job_id = self._row_value(job, "id")
        items = self._items(job_id)
        status = _plain(self._row_value(job, "status"), "created")
        state_counts = {
            str(row["state"]): int(row["count"])
            for row in self.db.conn.execute(
                "SELECT state,COUNT(*) AS count FROM metadata_intelligence_items "
                "WHERE job_id=? GROUP BY state",
                (job_id,),
            ).fetchall()
        }
        pending = (
            state_counts.get("queued", 0)
            + state_counts.get("analyzing", 0)
            + state_counts.get("review", 0)
            + state_counts.get("ready", 0)
        )
        self.summary.setText(
            f"Job: {status.replace('_', ' ').title()}  •  "
            f"Pending: {pending}  •  Applied: {state_counts.get('applied', 0)}  •  "
            f"Applied with Gaps: {state_counts.get('applied_with_gaps', 0)}  •  "
            f"Source Fallback: {state_counts.get('source_fallback', 0)}  •  "
            f"Failed: {state_counts.get('failed', 0)}  •  "
            f"No Match: {state_counts.get('no_match', 0)}  •  Visible: {len(items)}"
        )
        self.table.setRowCount(len(items))
        state_columns = self._columns("metadata_intelligence_items")
        state_name = "state" if "state" in state_columns else "status"
        for row_index, item in enumerate(items):
            hints = _decoded(self._row_value(item, "parsed_hints"))
            proposal = _decoded(
                self._row_value(item, "field_proposal", "proposed_patch")
            )
            agreement = _plain(self._row_value(item, "provider_agreement"), "unknown")
            current = proposal.get("_current", {})
            discogs = proposal.get("_discogs", {})
            musicbrainz = proposal.get("_musicbrainz", {})
            artwork = proposal.get("_artwork", {})
            current = current if isinstance(current, Mapping) else {}
            discogs = discogs if isinstance(discogs, Mapping) else {}
            musicbrainz = musicbrainz if isinstance(musicbrainz, Mapping) else {}
            artwork = artwork if isinstance(artwork, Mapping) else {}
            proposed_fields = [
                name
                for name in EDITABLE_METADATA_FIELDS
                if name in proposal and proposal[name] not in (None, "")
            ]
            current_art = "Present" if current.get("artwork") else "Gap"
            candidate_art = (
                "candidate available"
                if artwork.get("candidate_available")
                else "no candidate"
            )
            state = str(self._row_value(item, state_name) or "")
            review_detail = _plain(self._row_value(item, "review_reason"))
            if state in {"applied_with_gaps", "source_fallback"}:
                decision = classify_stored_review_evidence(
                    parsed_hints=self._row_value(item, "parsed_hints"),
                    field_proposal=self._row_value(
                        item, "field_proposal", "proposed_patch"
                    ),
                    field_confidence=self._row_value(item, "field_confidence"),
                    provider_agreement=self._row_value(item, "provider_agreement"),
                    review_reason=self._row_value(item, "review_reason"),
                )
                gap_labels = [
                    _GAP_LABELS.get(gap, gap.replace("_", " ").title())
                    for gap in decision.secondary_gaps
                ]
                if gap_labels:
                    review_detail = " • ".join(gap_labels)
                elif state == "source_fallback":
                    review_detail = "Strong source-title fallback"
            values = (
                _STATE_LABELS.get(state, state.replace("_", " ").title()),
                self._summary_text(current),
                _plain(hints.get("raw_title") or hints.get("title")),
                _plain(hints.get("uploader")),
                self._summary_text(discogs),
                f"{self._summary_text(musicbrainz)} • {agreement.replace('_', ' ')}",
                self._duration_text(current, discogs, musicbrainz),
                _plain(proposal.get("version_type") or hints.get("version_type")),
                _plain(discogs.get("album") or proposal.get("album")),
                f"Current: {current_art} • Discogs: {candidate_art} • {_plain(artwork.get('result'), 'pending')}",
                ", ".join(name.replace("_", " ").title() for name in proposed_fields) or "—",
                review_detail,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                if column == 0:
                    cell.setData(
                        Qt.ItemDataRole.UserRole,
                        int(self._row_value(item, "track_id") or 0),
                    )
                    cell.setData(
                        int(Qt.ItemDataRole.UserRole) + 1,
                        int(self._row_value(item, "id") or 0),
                    )
                self.table.setItem(row_index, column, cell)
        self._populate_field_choices()

    def _selected_track_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return int(value) if value else None

    def _selected_item_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = (
            item.data(int(Qt.ItemDataRole.UserRole) + 1)
            if item is not None
            else None
        )
        return int(value) if value else None

    def _refresh_discogs_attribution(self) -> None:
        item_id = self._selected_item_id()
        row = None
        if item_id is not None:
            try:
                row = self.db.conn.execute(
                    "SELECT discogs_release_id,discogs_master_id,field_proposal "
                    "FROM metadata_intelligence_items WHERE id=?",
                    (item_id,),
                ).fetchone()
            except Exception:
                row = None
        proposal = _decoded(row["field_proposal"] if row is not None else None)
        discogs = proposal.get("_discogs", {})
        discogs = discogs if isinstance(discogs, Mapping) else {}
        url = _discogs_attribution_url(
            discogs.get("provider_reference")
            or discogs.get("artist_provider_reference"),
            release_id=(row["discogs_release_id"] if row is not None else None),
            master_id=(row["discogs_master_id"] if row is not None else None),
            artist_id=discogs.get("discogs_artist_id") or discogs.get("artist_id"),
        )
        self.discogs_attribution_label.setText(
            f'<a href="{url}">Data provided by Discogs</a>'
        )

    def _clear_field_choices(self) -> None:
        for checkbox in self.field_checks.values():
            self.field_choice_layout.removeWidget(checkbox)
            checkbox.deleteLater()
        self.field_checks.clear()

    def _populate_field_choices(self) -> None:
        self._clear_field_choices()
        self._refresh_discogs_attribution()
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1:
            self.field_choice_hint.show()
            self.apply_fields_button.setEnabled(False)
            return
        item_id = self._selected_item_id()
        row = self.db.conn.execute(
            "SELECT state,field_proposal FROM metadata_intelligence_items WHERE id=?",
            (item_id,),
        ).fetchone() if item_id is not None else None
        proposal = _decoded(row["field_proposal"] if row is not None else None)
        for name in EDITABLE_METADATA_FIELDS:
            value = proposal.get(name)
            if value in (None, "") or isinstance(value, (Mapping, list, tuple)):
                continue
            checkbox = QCheckBox(f"{name.replace('_', ' ').title()}: {_plain(value)}")
            checkbox.setObjectName("MetadataReviewFieldChoice")
            checkbox.setChecked(True)
            self.field_checks[name] = checkbox
            index = len(self.field_checks) - 1
            self.field_choice_layout.addWidget(checkbox, index // 3, index % 3)
        reviewable = row is not None and str(row["state"]) in {"review", "ready"}
        self.field_choice_hint.setVisible(not bool(self.field_checks))
        self.apply_fields_button.setEnabled(reviewable and bool(self.field_checks))

    def _apply_selected_fields(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None or self.service is None:
            return
        selected = [name for name, checkbox in self.field_checks.items() if checkbox.isChecked()]
        try:
            result = self.service.apply_review_fields(item_id, selected)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Metadata Intelligence",
                sanitize_error_text(exc),
            )
            return
        self.review_applied.emit(int(result.track_id))
        self.refresh()

    def _edit_selected(self, *_args) -> None:
        track_id = self._selected_track_id()
        if track_id is None:
            QMessageBox.information(self, "Metadata Intelligence", "Select one item first.")
            return
        self.edit_track_requested.emit(track_id)

    def _job_action(self, action: str) -> None:
        job = self._latest_job()
        if job is None or self.service is None:
            return
        callback = getattr(self.service, f"{action}_job", None)
        if callback is None:
            return
        job_id = str(self._row_value(job, "id") or "")
        job_kind = str(self._row_value(job, "job_kind") or "")
        if not job_id or not job_kind:
            return
        try:
            callback(job_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Metadata Intelligence",
                sanitize_error_text(exc),
            )
            return
        if action == "resume":
            self.resume_requested.emit(job_id, job_kind)
        self.refresh()

    def pause_job(self) -> None:
        self._job_action("pause")

    def resume_job(self) -> None:
        self._job_action("resume")

    def cancel_job(self) -> None:
        answer = QMessageBox.question(
            self,
            "Cancel metadata job?",
            "Cancel queued work safely? Existing metadata and media are retained.",
        )
        if answer == QMessageBox.Yes:
            self._job_action("cancel")


__all__ = ["MetadataIntelligenceDialog", "REVIEW_FILTERS"]
