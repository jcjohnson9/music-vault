"""Saved intelligence review choices remain bound to what the user saw."""
from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QMessageBox

from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.service import MetadataService
from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog
from test_batch10_1_intelligence_review_ui import (  # noqa: F401
    _create_job, _proposal, intelligence_review_context,
)


def _dialog(context):
    db, _runtime, add_track = context
    track_id = add_track(61)
    _job, items = _create_job(db, [track_id], [{
        "state": "review", "field_proposal": _proposal(title="Reviewed Synthetic Title"),
    }])
    service = MetadataIntelligenceService(db, {"metadata_intelligence_enabled": True})
    dialog = MetadataIntelligenceDialog(db, service=service)
    dialog.table.selectRow(0)
    for name, checkbox in dialog.field_checks.items():
        checkbox.setChecked(name == "title")
    return dialog, track_id, items[0]


def test_review_shows_current_values_but_does_not_fetch_providers(intelligence_review_context):
    dialog, _track_id, _item_id = _dialog(intelligence_review_context)
    try:
        assert "Current Title 61" in dialog.field_choice_hint.text()
        assert "Provider identities and audio-file tags are not imported" in dialog.field_choice_hint.text()
        assert dialog.field_choice_hint.isHidden() is False
        assert dialog._review_revision
        assert dialog.apply_fields_button.isEnabled()
    finally:
        dialog.close()


@pytest.mark.parametrize("change", ["proposal", "credit", "job"])
def test_changed_review_is_refused_without_any_apply_then_requires_refresh(
    intelligence_review_context, monkeypatch, change,
):
    db, _runtime, _add = intelligence_review_context
    dialog, track_id, item_id = _dialog(intelligence_review_context)
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: notices.append(args[2]))
    emitted = []
    dialog.review_applied.connect(emitted.append)
    try:
        with db.conn:
            if change == "proposal":
                db.conn.execute(
                    "UPDATE metadata_intelligence_items SET field_proposal=? WHERE id=?",
                    (json.dumps(_proposal(title="Not Previously Shown")), item_id),
                )
            elif change == "credit":
                db.conn.execute(
                    "UPDATE track_artist_credits SET credited_as='New Alias' WHERE track_id=?",
                    (track_id,),
                )
            else:
                db.conn.execute(
                    "UPDATE metadata_intelligence_jobs SET last_error='Synthetic changed job' "
                    "WHERE id=(SELECT job_id FROM metadata_intelligence_items WHERE id=?)",
                    (item_id,),
                )
        before = tuple(db.conn.iterdump())
        dialog._apply_selected_fields()
        assert notices and "Nothing was applied" in notices[0]
        assert emitted == []
        assert tuple(db.conn.iterdump()) == before
        assert not dialog.apply_fields_button.isEnabled()
        assert dialog._review_revision is None
        # A repeated click cannot silently recapture consent for a new value.
        dialog._apply_selected_fields()
        assert tuple(db.conn.iterdump()) == before
        dialog.refresh()
        dialog.table.selectRow(0)
        for name, checkbox in dialog.field_checks.items():
            checkbox.setChecked(name == "title")
        assert dialog.apply_fields_button.isEnabled()
        dialog._apply_selected_fields()
        assert emitted == [track_id]
        assert MetadataService(db).snapshot(track_id).value("title") == (
            "Not Previously Shown" if change == "proposal" else "Reviewed Synthetic Title"
        )
    finally:
        dialog.close()


def test_invalid_saved_proposal_disables_apply(intelligence_review_context):
    db, _runtime, _add = intelligence_review_context
    dialog, _track_id, item_id = _dialog(intelligence_review_context)
    try:
        with db.conn:
            db.conn.execute(
                "UPDATE metadata_intelligence_items SET field_proposal='{invalid' WHERE id=?",
                (item_id,),
            )
        dialog._populate_field_choices()
        assert not dialog.apply_fields_button.isEnabled()
        assert dialog._review_revision is None
        assert "unavailable" in dialog.field_choice_hint.text()
    finally:
        dialog.close()
