"""Bounded, offline Batch 10.1 metadata-intelligence UI review.

The harness uses synthetic in-memory job data and fake artist credits only. It
does not read a runtime database or credential, instantiate a provider, or make
a network request. Captures are deleted after validation unless explicitly
retained under TEMP or the ignored repository ``.ui-review`` directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PREFIX = "MusicVault_Batch10_1_UI_Review_"
OWNER_MARKER = ".music_vault_batch10_1_review_owner.json"
DISCOGS_NOTICE = (
    "This application uses Discogs’ API but is not affiliated with, sponsored or "
    "endorsed by Discogs. “Discogs” is a trademark of Zink Media, LLC."
)


@dataclass(frozen=True)
class ReviewScene:
    name: str
    width: int
    height: int
    purpose: str


SCENES = (
    ReviewScene("discogs_settings", 1280, 720, "Personal-token setup and attribution"),
    ReviewScene("metadata_consent", 1280, 720, "Provider/privacy consent"),
    ReviewScene("job_summary", 1920, 900, "Resumable existing-library summary"),
    ReviewScene("provider_agreement", 1920, 900, "Strong provider agreement"),
    ReviewScene("provider_disagreement", 1920, 900, "Provider disagreement routed to review"),
    ReviewScene("structured_credits", 1440, 900, "Ordered primary and featured credits"),
    ReviewScene("unofficial_live", 1280, 720, "Separate original and version dates"),
    ReviewScene("youtube_exclusive", 1920, 900, "Honest online-only fallback"),
    ReviewScene("missing_art", 1280, 720, "Gap-only Discogs artwork candidate"),
    ReviewScene("artist_featured_on", 1440, 900, "Tracks and Featured On hierarchy"),
)

_BLOCKED_TEXT = (
    "\\users\\jerjo",
    "/users/jerjo",
    "youtube_api_key",
    "discogs_token.txt",
    "authorization:",
    "bearer ",
    "token=",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture ten sanitized Batch 10.1 metadata-intelligence states with "
            "no credential, live data, provider request, or retained output by default."
        )
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-captures", action="store_true")
    parser.add_argument("--offscreen", action="store_true")
    return parser.parse_args(argv)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _output_directory(requested: Path | None) -> tuple[Path, str]:
    if requested is None:
        output = Path(tempfile.mkdtemp(prefix=OUTPUT_PREFIX)).resolve()
    else:
        output = requested.expanduser().resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        review = (PROJECT_ROOT / ".ui-review").resolve()
        if not _is_relative_to(output, review) and not (
            _is_relative_to(output, temp) and output.name.startswith(OUTPUT_PREFIX)
        ):
            raise ValueError("Output is allowed only in TEMP or .ui-review/.")
        if output.exists() and any(output.iterdir()):
            raise ValueError("Refusing to use a non-empty review directory.")
        output.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    (output / OWNER_MARKER).write_text(
        json.dumps({"schema_version": 1, "token": token}) + "\n",
        encoding="utf-8",
    )
    return output, token


def _owned_output(output: Path, token: str) -> Path:
    resolved = output.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    review = (PROJECT_ROOT / ".ui-review").resolve()
    if resolved.is_symlink() or not (
        (_is_relative_to(resolved, temp) and resolved.name.startswith(OUTPUT_PREFIX))
        or _is_relative_to(resolved, review)
    ):
        raise RuntimeError("Refusing to clean an unverified review directory.")
    try:
        marker = json.loads((resolved / OWNER_MARKER).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Review ownership marker is unavailable.") from exc
    if marker.get("token") != token:
        raise RuntimeError("Review ownership marker does not match this run.")
    return resolved


def _install_network_guard() -> list[str]:
    attempts: list[str] = []
    blocked = {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "urllib.Request",
        "http.client.connect",
    }

    def audit(event: str, _args: tuple[object, ...]) -> None:
        if event in blocked:
            attempts.append(event)
            raise RuntimeError(f"Batch 10.1 review blocked network event: {event}")

    sys.addaudithook(audit)
    return attempts


def _synthetic_job_db() -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE metadata_intelligence_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE metadata_intelligence_items (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            parsed_hints TEXT,
            field_proposal TEXT,
            provider_agreement TEXT,
            current_snapshot TEXT,
            review_reason TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO metadata_intelligence_jobs VALUES (?, ?, ?)",
        ("synthetic-job", "paused", "2026-01-01T00:00:00Z"),
    )
    rows = (
        (
            1,
            101,
            "applied",
            {"raw_title": "Harbor Lights — Glass Horizon (Official Audio)", "uploader": "Synthetic Records"},
            {
                "_current": {"artist": "Synthetic Records", "title": "Harbor Lights", "album": "", "duration_seconds": 218, "artwork": True},
                "_discogs": {"artist": "Glass Horizon", "title": "Harbor Lights", "album": "Measured Distance", "duration_seconds": 218, "score": 98},
                "_musicbrainz": {"artist": "Glass Horizon", "title": "Harbor Lights", "duration_seconds": 219, "score": 95},
                "_artwork": {"candidate_available": True, "result": "existing valid artwork preserved"},
                "artist": "Glass Horizon",
                "title": "Harbor Lights",
                "album": "Measured Distance",
                "release_date": "2004",
                "version_type": "studio",
            },
            "agreed",
            {"display": "Harbor Lights — Glass Horizon"},
            None,
        ),
        (
            2,
            102,
            "review",
            {"raw_title": "Northern Current — Signal Bloom (Long Mix)", "uploader": "Archive Channel"},
            {
                "_current": {"artist": "Archive Channel", "title": "Signal Bloom (Long Mix)", "album": "", "duration_seconds": 364, "artwork": False},
                "_discogs": {"artist": "Northern Current", "title": "Signal Bloom", "album": "Longer Signals", "duration_seconds": 363, "score": 93},
                "_musicbrainz": {"artist": "Northern Current", "title": "Signal Bloom", "album": "Signal Bloom", "duration_seconds": 241, "score": 91},
                "_artwork": {"candidate_available": False, "result": "awaiting review"},
                "version_type": "extended",
            },
            "conflict",
            {"display": "Signal Bloom (Long Mix) — Northern Current"},
            "provider_disagreement",
        ),
        (
            3,
            103,
            "review",
            {"raw_title": "Cedar Signal live at Winter Hall", "uploader": "Concert Archive"},
            {
                "_current": {"artist": "Concert Archive", "title": "Cedar Signal live at Winter Hall", "album": "", "duration_seconds": 287, "artwork": False},
                "_discogs": {"artist": "Cedar Signal", "title": "Winter Signal", "album": "", "score": 82},
                "_musicbrainz": {"artist": "Cedar Signal", "title": "Winter Signal", "album": "Studio Signals", "score": 88},
                "_artwork": {"candidate_available": False, "result": "no official live artwork"},
                "original_release_date": "1998",
                "version_type": "live",
                "version_label": "Live at Winter Hall — Concert Recording",
            },
            "partial",
            {"display": "Cedar Signal — Live at Winter Hall"},
            "date_ambiguity",
        ),
        (
            4,
            104,
            "review",
            {"raw_title": "Quiet Relay — Rooftop Session", "uploader": "Quiet Relay"},
            {
                "_current": {"artist": "Quiet Relay", "title": "Rooftop Session", "album": "", "duration_seconds": 202, "artwork": True},
                "_discogs": {},
                "_musicbrainz": {},
                "_artwork": {"candidate_available": False, "result": "existing artwork preserved"},
                "artist": "Quiet Relay",
                "title": "Rooftop Session",
                "version_type": "youtube_exclusive",
            },
            "no_match",
            {"display": "Rooftop Session — Quiet Relay"},
            "youtube_exclusive",
        ),
    )
    conn.executemany(
        """
        INSERT INTO metadata_intelligence_items (
            id, job_id, track_id, state, parsed_hints, field_proposal,
            provider_agreement, current_snapshot, review_reason
        ) VALUES (?, 'synthetic-job', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item_id,
                track_id,
                state,
                json.dumps(hints),
                json.dumps(proposal),
                agreement,
                json.dumps(current),
                reason,
            )
            for item_id, track_id, state, hints, proposal, agreement, current, reason in rows
        ],
    )
    conn.commit()
    return SimpleNamespace(conn=conn)


def _mark(widget) -> None:
    widget.setProperty("reviewGeometry", True)


def _label(text: str, *, name: str = "", wrap: bool = True):
    from PySide6.QtWidgets import QLabel

    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    _mark(label)
    return label


def _card(title: str, body: str):
    from PySide6.QtWidgets import QFrame, QVBoxLayout

    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(8)
    layout.addWidget(_label(title, name="CardTitle"))
    layout.addWidget(_label(body, name="MutedLabel"))
    _mark(card)
    return card


def _shell(title: str, subtitle: str):
    from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

    window = QWidget()
    window.setObjectName("AppRoot")
    layout = QVBoxLayout(window)
    layout.setContentsMargins(34, 28, 34, 28)
    layout.setSpacing(14)
    header = QFrame()
    header.setObjectName("HeroHeader")
    header_layout = QVBoxLayout(header)
    header_layout.setContentsMargins(22, 18, 22, 18)
    header_layout.addWidget(_label(title, name="PageTitle"))
    header_layout.addWidget(_label(subtitle, name="MutedLabel"))
    layout.addWidget(header)
    _mark(header)
    return window, layout


def _settings_scene():
    from PySide6.QtWidgets import (
        QCheckBox,
        QGridLayout,
        QHBoxLayout,
        QLineEdit,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    window, layout = _shell(
        "Automatic Metadata Intelligence",
        "Discogs-first catalogue matching with MusicBrainz corroboration and local review.",
    )
    panel = QWidget()
    panel_layout = QVBoxLayout(panel)
    panel_layout.setSpacing(12)
    status = QGridLayout()
    status.addWidget(_label("Discogs provider", name="SectionLabel"), 0, 0)
    status.addWidget(_label("Personal token not configured", name="StatusLine"), 0, 1)
    token = QLineEdit()
    token.setObjectName("DiscogsTokenField")
    token.setEchoMode(QLineEdit.EchoMode.Password)
    token.setPlaceholderText("Paste a personal token — never shown after saving")
    token.setText("")
    _mark(token)
    status.addWidget(token, 1, 0, 1, 2)
    actions = QHBoxLayout()
    for text, name in (
        ("Save Token", "PrimaryButton"),
        ("Remove Token", "GhostButton"),
        ("Test Connection", "GhostButton"),
        ("Open Token Setup Guide", "GhostButton"),
    ):
        button = QPushButton(text)
        button.setObjectName(name)
        _mark(button)
        actions.addWidget(button)
    status.addLayout(actions, 2, 0, 1, 2)
    panel_layout.addLayout(status)
    for text, checked in (
        ("Enable Metadata Intelligence", False),
        ("Use Discogs as primary automatic catalogue authority", False),
        ("Use MusicBrainz as secondary corroboration/fallback", True),
        ("Fill true artwork gaps from Discogs", False),
        ("Write approved high-confidence text tags with verified backups", False),
    ):
        box = QCheckBox(text)
        box.setChecked(checked)
        _mark(box)
        panel_layout.addWidget(box)
    panel_layout.addWidget(
        _label(DISCOGS_NOTICE, name="MutedLabel")
    )
    panel_layout.addWidget(
        _label('<a href="https://www.discogs.com/">Data provided by Discogs</a>', name="MutedLabel")
    )
    layout.addWidget(panel, 1)
    return window


def _consent_scene():
    from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton

    window, layout = _shell(
        "Enable Metadata Intelligence?",
        "Review exactly what leaves this computer before opting in.",
    )
    layout.addWidget(
        _card(
            "Sent only for an analyzed track",
            "Normalized title, artist, album, duration, and version hints. YouTube uploader "
            "and upload date remain source provenance.",
        )
    )
    layout.addWidget(
        _card(
            "Never sent or exported",
            "Audio bytes, playlists, filesystem paths, YouTube API key, personal token, "
            "browser cookies, or a bulk library inventory.",
        )
    )
    agreement = QCheckBox(
        "I understand Discogs is optional, terms can change, and uncertain fields stay in review."
    )
    _mark(agreement)
    layout.addWidget(agreement)
    actions = QHBoxLayout()
    actions.addStretch(1)
    for text, name in (("Keep Disabled", "GhostButton"), ("Accept and Enable", "PrimaryButton")):
        button = QPushButton(text)
        button.setObjectName(name)
        _mark(button)
        actions.addWidget(button)
    layout.addLayout(actions)
    return window


def _dashboard_scene(db, filter_value: str | None):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog

    wrapper = QWidget()
    wrapper.setObjectName("AppRoot")
    wrapper_layout = QVBoxLayout(wrapper)
    wrapper_layout.setContentsMargins(0, 0, 0, 0)
    dialog = MetadataIntelligenceDialog(db, parent=wrapper)
    dialog.setWindowFlags(Qt.WindowType.Widget)
    wrapper_layout.addWidget(dialog)
    if filter_value is None:
        # Exercise one real filter transition before returning to All Items so
        # the native table/header backing stores are fully initialized.
        dialog.filter_combo.setCurrentIndex(1)
        dialog.filter_combo.setCurrentIndex(0)
    else:
        index = dialog.filter_combo.findData(filter_value)
        if index >= 0:
            dialog.filter_combo.setCurrentIndex(index)
    dialog.refresh()
    _mark(dialog.summary)
    _mark(dialog.filter_combo)
    _mark(dialog.table)
    return wrapper


class _SyntheticCreditService:
    def track_credits(self, _track_id: int):
        from music_vault.metadata.artist_credits import Artist, TrackArtistCredit

        primary = Artist(1, "Glass Horizon", "glass horizon", "glass horizon", "band", "7001", None)
        featured = Artist(2, "The Quiet Current", "the quiet current", "quiet current, the", "duo", "7002", None)
        return (
            TrackArtistCredit(1, 101, primary, "primary", 0, "", "discogs_high_confidence", "synthetic-release", 98.0, False, False),
            TrackArtistCredit(2, 101, featured, "featured", 1, "feat.", "discogs_high_confidence", "synthetic-release", 98.0, False, False),
        )


def _credits_scene():
    from music_vault.ui.artist_credit_editor import ArtistCreditEditor

    window, layout = _shell(
        "Trusted Metadata — Structured Credits",
        "Provider structure is preserved; manual saves become authoritative and locked.",
    )
    editor = ArtistCreditEditor(_SyntheticCreditService(), 101)
    _mark(editor)
    _mark(editor.table)
    layout.addWidget(editor, 1)
    layout.addWidget(
        _label(
            "Display: Glass Horizon feat. The Quiet Current  •  Featured artist appears under Featured On.",
            name="StatusLine",
        )
    )
    return window


def _details_scene(kind: str):
    from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QWidget

    titles = {
        "unofficial_live": (
            "Unofficial Live — Review Details",
            "Version identity is preserved without pretending the concert upload is an official release.",
        ),
        "missing_art": (
            "Artwork Gap — Discogs Candidate",
            "Only a validated front image may fill a true gap; existing valid artwork always wins.",
        ),
    }
    window, layout = _shell(*titles[kind])
    grid = QGridLayout()
    if kind == "unofficial_live":
        values = (
            ("Title", "Cedar Signal — Live at Winter Hall"),
            ("Primary artist", "Cedar Signal (group)"),
            ("Version type", "Live"),
            ("Version label", "Live at Winter Hall — Concert Recording"),
            ("Version release date / Year", "Not available — intentionally blank"),
            ("Original song release date", "1998"),
            ("Album", "Not assigned — no official live release"),
            ("Uploader provenance", "Concert Archive — not treated as an artist"),
        )
    else:
        values = (
            ("Current artwork", "Missing — verified true gap"),
            ("Discogs proposal", "Front image from accepted synthetic release"),
            ("Validation", "JPEG • bounded dimensions • decoded successfully"),
            ("Storage", "Private content-addressed runtime cover"),
            ("Automatic embedding", "Disabled — image is not written into media"),
            ("Attribution", "Data provided by Discogs"),
        )
    for row, (name, value) in enumerate(values):
        grid.addWidget(_label(name, name="SectionLabel"), row, 0)
        grid.addWidget(_label(value, name="StatusLine"), row, 1)
    panel = QWidget()
    panel.setLayout(grid)
    _mark(panel)
    layout.addWidget(panel, 1)
    if kind == "missing_art":
        note = QProgressBar()
        note.setRange(0, 100)
        note.setValue(98)
        note.setFormat("Candidate confidence: 98% — gap-only eligible")
        _mark(note)
        layout.addWidget(note)
        actions = QHBoxLayout()
        actions.addStretch(1)
        for text, name in (("Keep Empty", "GhostButton"), ("Accept Gap Fill", "PrimaryButton")):
            button = QPushButton(text)
            button.setObjectName(name)
            _mark(button)
            actions.addWidget(button)
        layout.addLayout(actions)
    return window


def _artist_scene():
    from PySide6.QtWidgets import QHeaderView, QTabWidget, QTableWidget, QTableWidgetItem

    window, layout = _shell(
        "The Quiet Current",
        "Duo  •  2 primary tracks  •  2 featured appearances  •  1 collaboration",
    )
    tabs = QTabWidget()
    tabs.setObjectName("ArtistRoleTabs")
    for label, rows in (
        (
            "Tracks",
            (
                ("Paper Constellations", "Afterimage", "Studio", "2004"),
                ("Borrowed Weather", "Afterimage", "Acoustic", "2005"),
            ),
        ),
        (
            "Featured On",
            (
                ("Harbor Lights", "Glass Horizon feat. The Quiet Current", "Studio", "2007"),
                ("Signal Bloom", "Northern Current feat. The Quiet Current", "Extended", "2012"),
            ),
        ),
        (
            "Collaborations",
            (("Measured Distance", "The Quiet Current & Glass Horizon", "Studio", "2008"),),
        ),
    ):
        table = QTableWidget(len(rows), 4)
        table.setHorizontalHeaderLabels(("Title", "Credit", "Version", "Year"))
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(3, 90)
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                table.setItem(row_index, column, QTableWidgetItem(value))
        _mark(table)
        tabs.addTab(table, label)
    tabs.setCurrentIndex(1)
    _mark(tabs)
    layout.addWidget(tabs, 1)
    layout.addWidget(
        _label(
            "Featured appearances stay outside the artist’s primary release count and do not change playback context.",
            name="MutedLabel",
        )
    )
    return window


def _scene_widget(scene: ReviewScene, db):
    if scene.name == "discogs_settings":
        return _settings_scene()
    if scene.name == "metadata_consent":
        return _consent_scene()
    if scene.name == "job_summary":
        return _dashboard_scene(db, None)
    if scene.name == "provider_agreement":
        return _dashboard_scene(db, "applied")
    if scene.name == "provider_disagreement":
        return _dashboard_scene(db, "provider_disagreement")
    if scene.name == "structured_credits":
        return _credits_scene()
    if scene.name in {"unofficial_live", "missing_art"}:
        return _details_scene(scene.name)
    if scene.name == "youtube_exclusive":
        return _dashboard_scene(db, "youtube_exclusive")
    if scene.name == "artist_featured_on":
        return _artist_scene()
    raise ValueError(f"Unsupported review scene: {scene.name}")


def _widget_texts(widget) -> list[str]:
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QLineEdit, QTableWidget

    texts: list[str] = []
    for child in (widget, *widget.findChildren(QObject)):
        if isinstance(child, QLabel):
            texts.append(child.text())
        elif isinstance(child, QAbstractButton):
            texts.append(child.text())
        elif isinstance(child, QLineEdit):
            texts.extend((child.text(), child.placeholderText()))
        elif isinstance(child, QComboBox):
            texts.extend(child.itemText(index) for index in range(child.count()))
        elif isinstance(child, QTableWidget):
            for row in range(child.rowCount()):
                for column in range(child.columnCount()):
                    item = child.item(row, column)
                    if item is not None:
                        texts.append(item.text())
    return texts


def _validate_scene(widget, scene: ReviewScene) -> dict[str, object]:
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QLineEdit, QWidget

    texts = _widget_texts(widget)
    flattened = "\n".join(texts).casefold()
    found = [needle for needle in _BLOCKED_TEXT if needle in flattened]
    if found:
        raise RuntimeError(f"Scene {scene.name} exposed blocked text category.")
    credential_fields = widget.findChildren(QLineEdit, "DiscogsTokenField")
    if credential_fields and any(
        field.text() or field.echoMode() != QLineEdit.EchoMode.Password
        for field in credential_fields
    ):
        raise RuntimeError("Discogs token field is not blank and masked.")

    checked = [
        child
        for child in widget.findChildren(QWidget)
        if bool(child.property("reviewGeometry")) and child.isVisible()
    ]
    root_rect = QRect(0, 0, widget.width(), widget.height())
    clipped: list[str] = []
    for child in checked:
        top_left = child.mapTo(widget, child.rect().topLeft())
        mapped = QRect(top_left, child.size())
        if not root_rect.contains(mapped):
            clipped.append(child.objectName() or type(child).__name__)
    if clipped:
        raise RuntimeError(f"Scene {scene.name} has clipped review widgets: {clipped[:4]}")
    return {
        "visible_review_widget_count": len(checked),
        "clipped_review_widget_count": len(clipped),
        "visible_text_sha256": hashlib.sha256(
            "\n".join(texts).encode("utf-8")
        ).hexdigest(),
        "credential_field_count": len(credential_fields),
        "credential_text_present": False,
    }


def _capture(app, scene: ReviewScene, output: Path, db) -> dict[str, object]:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QWidget

    widget = _scene_widget(scene, db)
    widget.resize(scene.width, scene.height)
    widget.show()
    widget.ensurePolished()
    for child in widget.findChildren(QWidget):
        child.ensurePolished()
    if widget.layout() is not None:
        widget.layout().activate()
    app.processEvents()
    widget.repaint()
    QTest.qWait(150)
    app.processEvents()
    metrics = _validate_scene(widget, scene)
    destination = output / f"batch10-1_{scene.name}_{scene.width}x{scene.height}.png"
    if scene.name == "job_summary":
        # Rendering the multi-row QTableWidget directly can let its native
        # viewport overpaint sibling widgets on Windows.  The polished backing
        # store is deterministic for this aggregate scene.
        pixmap = widget.grab()
    else:
        pixmap = QPixmap(widget.size())
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        widget.render(painter, QPoint(0, 0))
        painter.end()
    if pixmap.isNull() or not pixmap.save(str(destination), "PNG"):
        widget.close()
        raise RuntimeError(f"Could not capture review scene: {scene.name}")
    app.processEvents()
    widget.close()
    app.processEvents()
    payload = destination.read_bytes()
    if len(payload) < 1024:
        raise RuntimeError(f"Review capture is unexpectedly small: {scene.name}")
    return {
        "scene": scene.name,
        "purpose": scene.purpose,
        "file": destination.name,
        "width": scene.width,
        "height": scene.height,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "metrics": metrics,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.offscreen:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    project_text = str(PROJECT_ROOT)
    if project_text not in sys.path:
        sys.path.insert(0, project_text)

    attempts = _install_network_guard()
    output, owner_token = _output_directory(args.output)
    complete = False
    db = None
    try:
        from PySide6.QtWidgets import QApplication
        from music_vault.ui.theme import application_stylesheet

        app = QApplication.instance() or QApplication(["music-vault-batch10-1-review"])
        app.setApplicationName("Music Vault Batch 10.1 Synthetic Review")
        app.setStyleSheet(application_stylesheet())
        db = _synthetic_job_db()
        captures = [_capture(app, scene, output, db) for scene in SCENES]
        if len(captures) != 10 or {item["scene"] for item in captures} != {
            scene.name for scene in SCENES
        }:
            raise RuntimeError("The Batch 10.1 review matrix is incomplete.")
        if attempts:
            raise RuntimeError("The Batch 10.1 review attempted provider/network access.")
        manifest = {
            "schema_version": 1,
            "application": "Music Vault",
            "review_kind": "batch10_1_discogs_metadata_intelligence",
            "status": "complete",
            "capture_count": len(captures),
            "scenes": [scene.name for scene in SCENES],
            "captures": captures,
            "synthetic_only": True,
            "in_memory_database": True,
            "provider_mode": "fake_local_only",
            "network_attempt_count": 0,
            "youtube_api_key_read": False,
            "discogs_token_read": False,
            "personal_data_read": False,
            "captures_retained": bool(args.keep_captures),
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": "complete",
                    "capture_count": len(captures),
                    "scenes": manifest["scenes"],
                    "synthetic_only": True,
                    "network_attempt_count": 0,
                    "captures_retained": bool(args.keep_captures),
                },
                indent=2,
            )
        )
        complete = True
        return 0
    finally:
        if db is not None:
            db.conn.close()
        if complete and not args.keep_captures:
            shutil.rmtree(_owned_output(output, owner_token))
        elif not complete:
            print(f"Batch 10.1 review output retained for diagnosis: {output}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
