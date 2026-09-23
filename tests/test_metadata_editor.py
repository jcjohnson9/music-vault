from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from music_vault.core import paths
from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artwork import prepare_artwork_bytes
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.providers import ProviderArtistCredit
from music_vault.metadata.service import MetadataService
from music_vault.ui.metadata_editor import MetadataEditorDialog, _PendingCandidateApply
from music_vault.ui.metadata_tasks import MetadataTaskResult


@pytest.fixture
def editor_context(tmp_path, monkeypatch, qapp):
    runtime = tmp_path / "runtime"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    paths._resolved_project_root.cache_clear()
    db = MusicVaultDB(runtime / "data" / "music_vault.sqlite3")
    media = runtime / "track.synthetic"
    media.write_bytes(b"synthetic")
    track_id = db.upsert_track(
        media,
        title="Synthetic Title",
        artist="Synthetic Artist",
        album="Synthetic Album",
        album_artist="Synthetic Album Artist",
        year="2001",
        source_kind="youtube",
        source_video_id="abcdefghijk",
        source_upload_date="2024-03-02",
    )
    service = MetadataService(db)
    dialog = MetadataEditorDialog(service, track_id)
    yield dialog, service, db, track_id, runtime
    dialog.close()
    db.close()
    paths._resolved_project_root.cache_clear()


def test_editor_displays_six_fields_provenance_lock_and_source_context(editor_context):
    dialog, service, _db, track_id, _runtime = editor_context
    assert set(dialog.field_editors) == {
        "title",
        "artist",
        "album",
        "album_artist",
        "release_date",
    }
    assert dialog.artwork_editor is not None
    assert all(editor.provenance_badge.text() for editor in dialog.field_editors.values())
    assert dialog.source_upload_date_label.text() == "2024-03-02"
    assert "audio files" in dialog.file_writeback_note.text()
    assert str(service.snapshot(track_id).path) not in dialog.sources_tab.findChildren(type(dialog.source_upload_date_label))[0].text()


def test_invalid_title_and_release_date_are_rejected_without_history(editor_context):
    dialog, service, db, track_id, _runtime = editor_context
    initial_history = db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0]
    dialog.field_editors["title"].value_edit.clear()
    dialog.save_manual_changes()
    assert "Title cannot be empty" in dialog.validation_label.text()
    dialog.field_editors["title"].value_edit.setText("Synthetic Title")
    dialog.field_editors["release_date"].value_edit.setText("2023-02-29")
    dialog.save_manual_changes()
    assert "invalid" in dialog.validation_label.text().casefold()
    assert service.snapshot(track_id).value("release_date") is None
    assert db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0] == initial_history


def test_manual_save_emits_once_locks_fields_and_no_change_does_not_emit(editor_context):
    dialog, service, _db, track_id, _runtime = editor_context
    emitted = []
    dialog.metadata_changed.connect(emitted.append)
    dialog.field_editors["album"].value_edit.setText("Corrected Album")
    dialog.save_manual_changes()
    assert len(emitted) == 1
    state = service.snapshot(track_id).fields["album"]
    assert state.value == "Corrected Album" and state.is_manual and state.is_locked

    second = MetadataEditorDialog(service, track_id)
    second_emitted = []
    second.metadata_changed.connect(second_emitted.append)
    second.save_manual_changes()
    assert second_emitted == []


def test_no_change_save_preserves_raw_legacy_whitespace(editor_context):
    _dialog, service, db, track_id, _runtime = editor_context
    raw_album = "  Legacy Album Value  "
    with db.conn:
        db.conn.execute(
            "UPDATE tracks SET album=?, metadata_updated_at=? WHERE id=?",
            (raw_album, "2000-01-01T00:00:00Z", track_id),
        )
        db.conn.execute(
            "UPDATE track_metadata_fields SET value=?, updated_at=? "
            "WHERE track_id=? AND field_name='album'",
            (raw_album, "2000-01-01T00:00:00Z", track_id),
        )
    history_before = db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0]

    no_change = MetadataEditorDialog(service, track_id)
    emitted = []
    no_change.metadata_changed.connect(emitted.append)
    no_change.save_manual_changes()

    snapshot = service.snapshot(track_id)
    assert emitted == []
    assert snapshot.value("album") == raw_album
    assert snapshot.metadata_updated_at == "2000-01-01T00:00:00Z"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0] == history_before


def test_typing_after_clear_unlock_or_reset_supersedes_pending_action(editor_context):
    dialog, _service, _db, _track_id, _runtime = editor_context
    editor = dialog.field_editors["album"]
    for prepare in (editor._clear, editor._unlock, editor._reset):
        prepare()
        editor.value_edit.setText("Reconsidered Value")
        editor.value_edit.textEdited.emit("Reconsidered Value")
        action = editor.action_for_save()
        assert action is not None
        assert action.action == "set"
        assert action.value == "Reconsidered Value"


def test_cancel_with_prepared_artwork_creates_no_runtime_file(editor_context):
    dialog, _service, _db, _track_id, runtime = editor_context
    # A tiny valid PNG; preparing is in-memory and Cancel must not persist it.
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(0xFF1DB954)
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    png = bytes(encoded)
    dialog.artwork_editor.prepared_artwork = prepare_artwork_bytes(png, "image/png")
    dialog.reject()
    assert not (runtime / "data" / "covers" / "manual").exists()


def test_candidate_requires_selection_and_applies_only_checked_nonempty_fields(
    editor_context,
    monkeypatch,
):
    dialog, service, _db, track_id, _runtime = editor_context
    candidate = MetadataCandidate(
        title="Confirmed Title",
        artist="Confirmed Artist",
        album=None,
        release_date="1984",
        recording_id="recording",
        release_id=None,
        score=98,
    )
    dialog.set_candidates([candidate])
    dialog.apply_selected_candidate()
    assert "Select exactly one" in dialog.search_status.text()
    dialog.candidate_table.selectRow(0)
    for name, checkbox in dialog.candidate_field_checks.items():
        checkbox.setChecked(name in {"title", "album"})
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    dialog.apply_selected_candidate()
    snapshot = service.snapshot(track_id)
    assert snapshot.value("title") == "Confirmed Title"
    assert snapshot.value("album") == "Synthetic Album"
    assert snapshot.fields["title"].provenance == "musicbrainz_confirmed"


def test_search_callback_receives_title_artist_and_cancel_event(editor_context, qapp):
    dialog, _service, _db, _track_id, _runtime = editor_context
    calls = []

    class FakeProvider:
        def search(self, title, artist=None, *, cancel_event=None):
            calls.append((title, artist, cancel_event))
            return []

    dialog.musicbrainz_provider = FakeProvider()
    dialog.search_title.setText("Explicit Query Title")
    dialog.search_artist.setText("Explicit Query Artist")
    dialog.start_musicbrainz_search()
    for _ in range(50):
        qapp.processEvents()
        if calls and dialog._active_search_id is None:
            break
        QTest.qWait(5)

    assert len(calls) == 1
    assert calls[0][0:2] == ("Explicit Query Title", "Explicit Query Artist")
    assert calls[0][2] is not None
    assert dialog.search_status.text() == "No matching candidates were found."


@pytest.mark.parametrize("close_method", ["reject", "accept"])
def test_dialog_close_invalidates_queued_candidate_artwork_result(
    editor_context,
    close_method,
):
    dialog, service, _db, track_id, _runtime = editor_context
    candidate = MetadataCandidate(
        title="Must Not Apply After Close",
        artist="Synthetic Artist",
        album="Synthetic Album",
        release_date="2001",
        recording_id="recording",
        release_id="release",
        score=99,
    )
    dialog._pending_candidate = _PendingCandidateApply(
        candidate,
        {"title": candidate.title},
        True,
    )
    dialog._active_artwork_id = 41

    getattr(dialog, close_method)()
    dialog._task_completed(
        MetadataTaskResult(
            "candidate_artwork",
            41,
            value=None,
            error="artwork_provider_unavailable",
        )
    )

    snapshot = service.snapshot(track_id)
    assert snapshot.value("title") == "Synthetic Title"
    assert snapshot.musicbrainz_recording_id is None
    assert dialog._pending_candidate is None
    assert dialog._active_artwork_id is None


def test_candidate_apply_and_undo_refresh_every_editor_surface(
    editor_context,
    monkeypatch,
):
    dialog, service, _db, track_id, runtime = editor_context
    artwork_path = runtime / "candidate-artwork.png"
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(0xFF2BD576)
    assert image.save(str(artwork_path), "PNG")
    candidate = MetadataCandidate(
        title="Confirmed Current Title",
        artist="Confirmed Current Artist",
        album="Confirmed Current Album",
        release_date="1984-03-09",
        recording_id="recording-id",
        release_id="release-id",
        score=98,
        provider="MusicBrainz",
        artwork_available=True,
    )
    dialog.set_candidates([candidate])
    dialog.candidate_table.selectRow(0)
    pending = _PendingCandidateApply(
        candidate,
        {"title": candidate.title, "artist": candidate.artist},
        True,
    )

    dialog._commit_candidate(pending, str(artwork_path))

    assert dialog.candidate_table.horizontalHeaderItem(5).text() == "Provider"
    assert dialog.candidate_table.item(0, 5).text() == "MusicBrainz"
    assert dialog.field_editors["title"].value_edit.text() == candidate.title
    assert dialog.field_editors["title"].lock_badge.text() == "Locked"
    assert "Musicbrainz Confirmed" in dialog.field_editors["title"].provenance_badge.text()
    assert dialog.artwork_editor.state.value == str(artwork_path)
    assert "Cover Art Archive" in dialog.artwork_editor.status.text()
    assert "Locked" in dialog.artwork_editor.status.text()
    assert dialog.source_context_labels["musicbrainz_recording_id"].text() == "recording-id"
    # Title/artist/art selection does not authorize rebinding the album edition.
    assert service.snapshot(track_id).musicbrainz_release_id is None
    assert "Confirmed Current Title" in dialog.candidate_preview.text()
    assert dialog.history_table.rowCount() >= 1
    assert any(
        dialog.observations_table.item(row, 2).text() == "Musicbrainz"
        for row in range(dialog.observations_table.rowCount())
    )

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    dialog.undo_last_change()

    snapshot = service.snapshot(track_id)
    assert snapshot.value("title") == "Synthetic Title"
    assert snapshot.value("artwork") is None
    assert dialog.field_editors["title"].value_edit.text() == "Synthetic Title"
    assert dialog.field_editors["title"].lock_badge.text() == "Unlocked"
    assert dialog.artwork_editor.state.value is None
    assert "Unlocked" in dialog.artwork_editor.status.text()


def test_candidate_artwork_error_applies_text_and_reports_retention(editor_context):
    dialog, service, _db, track_id, _runtime = editor_context
    candidate = MetadataCandidate(
        title="Confirmed Without Artwork",
        artist="Synthetic Artist",
        album="Synthetic Album",
        release_date="2001",
        recording_id="recording",
        release_id="release",
        score=96,
        artwork_available=True,
    )
    dialog._pending_candidate = _PendingCandidateApply(
        candidate,
        {"title": candidate.title},
        True,
    )
    dialog._active_artwork_id = 71

    dialog._task_completed(
        MetadataTaskResult(
            "candidate_artwork",
            71,
            error="artwork_provider_unavailable",
        )
    )

    snapshot = service.snapshot(track_id)
    assert snapshot.value("title") == candidate.title
    assert snapshot.value("artwork") is None
    assert "artwork was unavailable" in dialog.search_status.text().casefold()
    assert "existing artwork was retained" in dialog.search_status.text().casefold()


def test_external_candidate_text_is_rendered_literally(editor_context):
    dialog, _service, _db, _track_id, _runtime = editor_context
    external_title = '<b>Literal title</b><img src="file:///private/cover.png">'
    external_artist = "<i>Literal artist</i>"
    candidate = MetadataCandidate(
        title=external_title,
        artist=external_artist,
        album="<u>Literal album</u>",
        release_date="2001",
        recording_id="recording",
        release_id=None,
        score=90,
    )

    dialog.set_candidates([candidate])
    dialog.candidate_table.selectRow(0)

    assert dialog.candidate_preview.textFormat() == Qt.TextFormat.PlainText
    assert external_title in dialog.candidate_preview.text()
    assert external_artist in dialog.candidate_preview.text()
    assert dialog.search_status.textFormat() == Qt.TextFormat.PlainText
    assert dialog.validation_label.textFormat() == Qt.TextFormat.PlainText
    assert all(
        label.textFormat() == Qt.TextFormat.PlainText
        for label in dialog.source_context_labels.values()
    )


def test_low_confidence_candidate_warns_and_cancel_changes_nothing(editor_context, monkeypatch):
    dialog, service, _db, track_id, _runtime = editor_context
    candidate = MetadataCandidate(
        title="Uncertain",
        artist="Maybe",
        album="Maybe Album",
        release_date=None,
        recording_id="recording",
        release_id=None,
        score=40,
    )
    dialog.set_candidates([candidate])
    dialog.candidate_table.selectRow(0)
    seen_titles = []

    def decline(_parent, title, *_args, **_kwargs):
        seen_titles.append(title)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    before = service.snapshot(track_id)
    dialog.apply_selected_candidate()
    assert seen_titles == ["Apply low-confidence candidate?"]
    assert service.snapshot(track_id).value("title") == before.value("title")


def test_history_and_confirmed_undo_are_exposed(editor_context, monkeypatch):
    dialog, service, _db, track_id, _runtime = editor_context
    service.apply_manual_patch(track_id, {"artist": "Manual Artist"})
    dialog.refresh_history()
    assert dialog.history_table.rowCount() >= 1
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    changes = []
    dialog.metadata_changed.connect(changes.append)
    dialog.undo_last_change()
    assert changes and changes[-1].changed
    assert service.snapshot(track_id).value("artist") == "Synthetic Artist"


def test_manual_save_rejects_external_change_without_losing_newer_metadata(editor_context):
    dialog, service, db, track_id, runtime = editor_context
    emitted = []
    dialog.metadata_changed.connect(emitted.append)
    dialog.field_editors["title"].value_edit.setText("Pending edit")
    service.apply_manual_patch(track_id, {"album": "Newer correction"})
    before = tuple(db.conn.iterdump())
    dialog.save_manual_changes()
    assert "changed while the editor was open" in dialog.validation_label.text()
    assert tuple(db.conn.iterdump()) == before
    assert emitted == []
    assert (runtime / "track.synthetic").read_bytes() == b"synthetic"


def test_async_candidate_result_rejects_revision_changed_after_confirmation(editor_context, monkeypatch):
    dialog, service, db, track_id, _runtime = editor_context
    candidate = MetadataCandidate("Candidate title", "Candidate artist", "Candidate album", "1995", "rec", "rel", 98)
    dialog.set_candidates([candidate])
    dialog.candidate_table.selectRow(0)
    dialog.candidate_field_checks["artwork"].setChecked(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(dialog.task_runner, "submit", lambda *args, **kwargs: 101)
    emitted = []
    dialog.metadata_changed.connect(emitted.append)
    dialog.apply_selected_candidate()
    assert dialog._pending_candidate.expected_fingerprint == dialog._metadata_revision
    service.apply_manual_patch(track_id, {"title": "Newer protected title"})
    before = tuple(db.conn.iterdump())
    dialog._task_completed(MetadataTaskResult("candidate_artwork", 101, value=None, error="unavailable"))
    assert "changed after your review" in dialog.search_status.text()
    assert tuple(db.conn.iterdump()) == before
    assert emitted == []
    assert dialog.search_button.isEnabled()


def test_artwork_only_confirmation_preserves_song_and_release_identity(editor_context):
    dialog, service, db, track_id, runtime = editor_context
    with db.conn:
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='original-recording', musicbrainz_release_id='original-release' WHERE id=?", (track_id,))
    dialog._refresh_editor_state()
    candidate = MetadataCandidate("Unselected title", "Unselected artist", "Unselected album", "1999", "other-recording", "other-release", 95, release_group_id="other-family")
    art = runtime / "selected-cover.png"
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(0xFF2BD576)
    assert image.save(str(art), "PNG")
    dialog._commit_candidate(_PendingCandidateApply(candidate, {}, True, dialog._metadata_revision), str(art))
    after = service.snapshot(track_id)
    assert after.value("artwork") == str(art)
    assert after.musicbrainz_recording_id == "original-recording"
    assert after.musicbrainz_release_id == "original-release"
    assert after.value("title") == "Synthetic Title"
    context = db.conn.execute("SELECT musicbrainz_release_group_id FROM track_release_context WHERE track_id=?", (track_id,)).fetchone()
    assert context is None or context[0] is None


def test_late_candidate_does_not_discard_unsaved_manual_draft(editor_context):
    dialog, _service, db, _track_id, _runtime = editor_context
    candidate = MetadataCandidate("Candidate title", "Candidate artist", None, None, "rec", "rel", 99)
    pending = _PendingCandidateApply(candidate, {"title": candidate.title}, True, dialog._metadata_revision)
    dialog.field_editors["album"].value_edit.setText("Unsaved album draft")
    before = tuple(db.conn.iterdump())
    dialog._commit_candidate(pending, None)
    assert "edits were retained" in dialog.search_status.text()
    assert dialog.field_editors["album"].value_edit.text() == "Unsaved album draft"
    assert tuple(db.conn.iterdump()) == before


def test_candidate_confirmation_does_not_begin_with_unsaved_edits(editor_context, monkeypatch):
    dialog, _service, db, _track_id, _runtime = editor_context
    candidate = MetadataCandidate("Candidate title", "Candidate artist", None, None, "rec", "rel", 99)
    dialog.set_candidates([candidate])
    dialog.candidate_table.selectRow(0)
    dialog.field_editors["album"].value_edit.setText("Keep draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("Do not confirm over unsaved edits"))
    before = tuple(db.conn.iterdump())
    dialog.apply_selected_candidate()
    assert "unsaved edits" in dialog.search_status.text()
    assert tuple(db.conn.iterdump()) == before


def test_confirmed_structured_credits_reach_service_and_undo(editor_context, monkeypatch):
    dialog, service, db, track_id, _runtime = editor_context
    before = [tuple(row) for row in db.conn.execute("SELECT * FROM track_artist_credits WHERE track_id=? ORDER BY rowid", (track_id,))]
    candidate = MetadataCandidate(
        "Unselected title", "Stage Name", None, None, "selected-recording", None, 99,
        artist_credits=(ProviderArtistCredit("Stage Name", artist_id="canonical-artist", provider="musicbrainz", canonical_name="Canonical Name", credited_as="Stage Name", entity_type="person"),),
    )
    dialog._commit_candidate(_PendingCandidateApply(candidate, {"artist": "Stage Name"}, False, dialog._metadata_revision), None)
    row = db.conn.execute("SELECT a.musicbrainz_artist_id, c.credited_as FROM track_artist_credits c JOIN artists a ON a.id=c.artist_id WHERE c.track_id=?", (track_id,)).fetchone()
    assert tuple(row) == ("canonical-artist", "Stage Name")
    editor = dialog.artist_credit_editor
    assert editor.table.cellWidget(0, editor.NAME_COLUMN).text() == "Stage Name"
    assert editor.display_artist() == "Stage Name"
    assert not editor.is_dirty()
    credit = editor.credit_inputs()[0]
    assert (credit.display_name, credit.credited_as, credit.musicbrainz_artist_id) == ("Canonical Name", "Stage Name", "canonical-artist")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    dialog.undo_last_change()
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM track_artist_credits WHERE track_id=? ORDER BY rowid", (track_id,))] == before
    assert service.snapshot(track_id).value("artist") == "Synthetic Artist"
