from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit

from music_vault.core import paths
from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.service import MetadataService
from music_vault.ui.artist_credit_editor import ArtistCreditEditor
from music_vault.ui.metadata_editor import MetadataEditorDialog


@pytest.fixture
def intelligence_editor_context(tmp_path, monkeypatch, qapp):
    runtime = tmp_path / "runtime"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    paths._resolved_project_root.cache_clear()
    db = MusicVaultDB(runtime / "data" / "music_vault.sqlite3")
    media = runtime / "synthetic-track.mp3"
    media.write_bytes(b"synthetic media fixture")
    track_id = db.upsert_track(
        media,
        title="Synthetic Song",
        artist="Synthetic Unit",
        album="Synthetic Release",
        source_kind="youtube",
        source_video_id="abcdefghijk",
        source_upload_date="2026-01-02",
    )
    metadata = MetadataService(db)
    yield db, metadata, track_id, runtime
    db.close()
    paths._resolved_project_root.cache_clear()


def test_editor_exposes_original_date_and_constrained_version_fields(
    intelligence_editor_context,
):
    db, metadata, track_id, _runtime = intelligence_editor_context
    dialog = MetadataEditorDialog(metadata, track_id)
    emitted = []
    dialog.metadata_changed.connect(emitted.append)

    assert set(dialog.intelligence_field_editors) == {
        "original_release_date",
        "version_type",
        "version_label",
    }
    available_types = {
        dialog.version_type_editor.value_combo.itemData(index)
        for index in range(dialog.version_type_editor.value_combo.count())
    }
    assert {"studio", "live", "remix", "youtube_exclusive", "unknown"} <= available_types

    dialog.original_release_date_editor.value_edit.setText("1984-03")
    dialog.version_type_editor.value_combo.setCurrentIndex(
        dialog.version_type_editor.value_combo.findData("live")
    )
    dialog.version_label_editor.value_edit.setText("Live at Synthetic Hall")
    dialog.save_manual_changes()

    snapshot = metadata.snapshot(track_id)
    assert snapshot.value("original_release_date") == "1984-03"
    assert snapshot.value("version_type") == "live"
    assert snapshot.value("version_label") == "Live at Synthetic Hall"
    assert all(
        snapshot.fields[name].is_manual and snapshot.fields[name].is_locked
        for name in ("original_release_date", "version_type", "version_label")
    )
    changed_history_fields = {
        row[0]
        for row in db.conn.execute(
            "SELECT field_name FROM track_metadata_history WHERE track_id=?",
            (track_id,),
        )
    }
    assert {"original_release_date", "version_type", "version_label"} <= changed_history_fields
    assert emitted and emitted[-1].changed


def test_compact_credit_editor_orders_roles_locks_and_preserves_provider_ids(
    intelligence_editor_context,
):
    db, metadata, track_id, _runtime = intelligence_editor_context
    credit_service = ArtistCreditService(db)
    credit_service.replace_track_credits(
        track_id,
        (
            ArtistCreditInput(
                "Synthetic Duo",
                role="primary",
                entity_type="duo",
                discogs_artist_id="101",
                musicbrainz_artist_id="synthetic-mbid",
            ),
        ),
        provenance="discogs",
        provider_reference="https://www.discogs.com/artist/101",
        confidence=98,
    )
    dialog = MetadataEditorDialog(metadata, track_id)
    editor = dialog.artist_credit_editor
    assert isinstance(editor, ArtistCreditEditor)
    assert editor.table.rowCount() == 1
    assert editor.table.columnWidth(editor.NAME_COLUMN) >= 200
    assert editor.table.columnWidth(editor.ENTITY_COLUMN) >= 130
    assert editor.table.columnWidth(editor.ROLE_COLUMN) >= 130
    assert editor.table.columnWidth(editor.JOIN_COLUMN) >= 140
    assert editor.table.cellWidget(0, editor.DISCOGS_COLUMN).text() == "101"
    assert editor.table.cellWidget(0, editor.MUSICBRAINZ_COLUMN).text() == "synthetic-mbid"

    editor.add_credit()
    featured_name = editor.table.cellWidget(1, editor.NAME_COLUMN)
    featured_role = editor.table.cellWidget(1, editor.ROLE_COLUMN)
    featured_entity = editor.table.cellWidget(1, editor.ENTITY_COLUMN)
    featured_join = editor.table.cellWidget(1, editor.JOIN_COLUMN)
    assert isinstance(featured_name, QLineEdit)
    assert isinstance(featured_role, QComboBox)
    featured_name.setText("Featured Voice")
    featured_role.setCurrentIndex(featured_role.findData("featured"))
    featured_entity.setCurrentIndex(featured_entity.findData("person"))
    featured_join.setText(" feat. ")

    editor.add_credit()
    collaborator_name = editor.table.cellWidget(2, editor.NAME_COLUMN)
    collaborator_role = editor.table.cellWidget(2, editor.ROLE_COLUMN)
    collaborator_join = editor.table.cellWidget(2, editor.JOIN_COLUMN)
    assert isinstance(collaborator_name, QLineEdit)
    assert isinstance(collaborator_role, QComboBox)
    collaborator_name.setText("Collaborating Group")
    collaborator_role.setCurrentIndex(collaborator_role.findData("collaborator"))
    collaborator_join.setText(" with ")
    editor.table.selectRow(2)
    editor.move_selected_credit(-1)

    dialog.save_manual_changes()
    stored = credit_service.track_credits(track_id)
    assert [credit.artist.display_name for credit in stored] == [
        "Synthetic Duo",
        "Collaborating Group",
        "Featured Voice",
    ]
    assert [credit.role for credit in stored] == ["primary", "collaborator", "featured"]
    assert [credit.credit_order for credit in stored] == [0, 1, 2]
    assert all(credit.is_manual and credit.is_locked for credit in stored)
    assert stored[0].artist.discogs_artist_id == "101"
    assert stored[0].artist.musicbrainz_artist_id == "synthetic-mbid"
    assert metadata.snapshot(track_id).value("artist") == (
        "Synthetic Duo with Collaborating Group feat. Featured Voice"
    )


def test_credit_editor_rejects_a_credit_set_without_primary(
    intelligence_editor_context,
):
    _db, metadata, track_id, _runtime = intelligence_editor_context
    dialog = MetadataEditorDialog(metadata, track_id)
    editor = dialog.artist_credit_editor
    original_artist = metadata.snapshot(track_id).value("artist")
    role = editor.table.cellWidget(0, editor.ROLE_COLUMN)
    assert isinstance(role, QComboBox)
    role.setCurrentIndex(role.findData("featured"))

    dialog.save_manual_changes()

    assert "At least one primary artist credit" in dialog.validation_label.text()
    assert metadata.snapshot(track_id).value("artist") == original_artist
    assert ArtistCreditService(metadata.conn).track_credits(track_id)[0].role == "primary"


def test_plain_manual_artist_edit_rebuilds_one_locked_primary_credit(
    intelligence_editor_context,
):
    _db, metadata, track_id, _runtime = intelligence_editor_context
    dialog = MetadataEditorDialog(metadata, track_id)
    dialog.field_editors["artist"].value_edit.setText("Manually Corrected Group")
    dialog.save_manual_changes()

    credits = ArtistCreditService(metadata.conn).track_credits(track_id)
    assert len(credits) == 1
    assert credits[0].artist.display_name == "Manually Corrected Group"
    assert credits[0].artist.entity_type == "unknown"
    assert credits[0].role == "primary"
    assert credits[0].is_manual and credits[0].is_locked
    assert metadata.snapshot(track_id).fields["artist"].is_locked


def test_sources_show_read_only_discogs_release_context_agreement_and_normal_link(
    intelligence_editor_context,
):
    db, metadata, track_id, _runtime = intelligence_editor_context
    timestamp = "2026-01-02T03:04:05Z"
    with db.conn:
        db.conn.execute(
            "UPDATE tracks SET discogs_release_id='2468',discogs_master_id='1357',"
            "discogs_track_position='A1' WHERE id=?",
            (track_id,),
        )
        db.conn.execute(
            """
            INSERT INTO track_release_context (
                track_id,discogs_release_id,discogs_master_id,release_title,
                release_country,release_format,catalog_number,label_name,
                release_date,original_release_date,provider_reference,confidence,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                track_id,
                "2468",
                "1357",
                "Synthetic Release Context",
                "US",
                "Vinyl",
                "SYN-42",
                "Synthetic Label",
                "2004",
                "1984",
                "https://www.discogs.com/release/2468",
                97,
                timestamp,
            ),
        )
        db.conn.execute(
            "INSERT INTO metadata_intelligence_jobs "
            "(id,job_kind,status,created_at,updated_at) VALUES "
            "('synthetic-ui','existing_library','ready',?,?)",
            (timestamp, timestamp),
        )
        db.conn.execute(
            "INSERT INTO metadata_intelligence_items "
            "(job_id,track_id,state,reason,provider_agreement,created_at,updated_at) "
            "VALUES ('synthetic-ui',?,'review','synthetic','agreed',?,?)",
            (track_id, timestamp, timestamp),
        )

    dialog = MetadataEditorDialog(metadata, track_id)
    labels = dialog.source_context_labels
    assert labels["discogs_release_id"].text() == "2468"
    assert labels["discogs_master_id"].text() == "1357"
    assert labels["discogs_track_position"].text() == "A1"
    assert labels["label_context"].text() == "Synthetic Label • SYN-42"
    assert "Synthetic Release Context" in labels["release_context"].text()
    assert labels["provider_agreement"].text() == "Agreed"
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels.values())
    assert "Data provided by Discogs" in dialog.discogs_attribution_label.text()
    assert "https://www.discogs.com/release/2468" in dialog.discogs_attribution_label.text()
    assert dialog.discogs_attribution_label.openExternalLinks()
    assert isinstance(dialog.discogs_attribution_label, QLabel)
