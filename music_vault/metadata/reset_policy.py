"""Read-only eligibility for resetting a field to previously observed facts.

An observation (or an evidence bundle) is not an acceptance decision. Catalogue
facts need a still-applicable effective-state witness; unresolved facts never
gain authority merely because they are newer or have a higher provider rank.
No provider requests, file access, or identity reconstruction occur here.
"""
from __future__ import annotations

import json
import sqlite3


_LOCAL = frozenset({"embedded", "filename", "youtube", "youtube_thumbnail", "youtube_title_parsed"})
_CATALOGUE = frozenset({
    "musicbrainz", "musicbrainz_high_confidence", "discogs", "discogs_high_confidence",
    "cover_art_archive", "cover_art_archive_high_confidence",
})
_IDENTITY_COLUMNS = (
    "musicbrainz_recording_id", "musicbrainz_release_id", "discogs_release_id",
    "discogs_master_id", "discogs_track_position", "recording_group_key",
    "version_type", "version_label",
)


def is_local_reset_provider(provider: str) -> bool:
    return str(provider).strip().casefold() in _LOCAL


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _has_current_identity(track, provider, field):
    if provider.startswith("discogs"):
        return bool(track.get("discogs_release_id") or track.get("discogs_master_id"))
    column = "musicbrainz_recording_id" if field == "title" else "musicbrainz_release_id"
    return bool(track.get(column))


def observation_is_reset_eligible(
    conn: sqlite3.Connection, track_id: int, observation: sqlite3.Row, *, provenance: str,
) -> bool:
    """Accept local fallback or a compatible, non-undone catalogue acceptance.

Catalogue artist resets deliberately remain unsupported: a scalar observation
cannot restore structured recording credits. They fall back to local evidence
or remain unresolved instead of flattening an accepted provider credit graph.
Legacy non-journal acceptance is supported only for an unversioned MusicBrainz
title with its exact, still-current recording identity.
"""
    field = observation["field_name"]
    provider = str(observation["provider"]).strip().casefold()
    if provider in _LOCAL:
        return not (provider.startswith("youtube") and field in {"release_date", "original_release_date"})
    if provider not in _CATALOGUE or field == "artist":
        return False
    track = dict(conn.execute("SELECT * FROM tracks WHERE id=?", (int(track_id),)).fetchone())
    if not _has_current_identity(track, provider, field):
        return False
    witnesses = conn.execute(
        "SELECT * FROM track_metadata_history WHERE track_id=? AND field_name=? "
        "AND new_value=? AND new_provenance=? AND new_provider_reference IS ? "
        "AND new_confidence IS ? AND new_is_manual=0 AND new_is_locked=0 "
        "AND actor<>'user' ORDER BY id DESC",
        (int(track_id), field, observation["value"], provenance,
         observation["provider_reference"], observation["confidence"]),
    ).fetchall()
    journal_available = _has_table(conn, "metadata_materializations")
    for witness in witnesses:
        if any(word in str(witness["actor"]).casefold() for word in ("undo", "rollback")):
            continue
        group = witness["change_group_id"]
        # Legacy Undo has no structured journal but still names its target.
        if conn.execute("SELECT 1 FROM track_metadata_history WHERE track_id=? AND reason=? LIMIT 1",
                        (int(track_id), "undo:" + group)).fetchone():
            continue
        journal = conn.execute("SELECT * FROM metadata_materializations WHERE id=? AND track_id=?",
                               (group, int(track_id))).fetchone() if journal_available else None
        if journal is not None:
            if journal["undone_at"] is not None:
                continue
            try:
                before, after = json.loads(journal["before_json"]), json.loads(journal["after_json"])
                previous = next((row for row in before["track_metadata_fields"] if row["field_name"] == field), None)
                accepted = next(row for row in after["track_metadata_fields"] if row["field_name"] == field)
                state_keys = ("value", "provenance", "provider_reference", "confidence", "is_manual", "is_locked")
                if ((previous is not None and all(previous.get(key) == accepted.get(key) for key in state_keys))
                        or accepted["value"] != observation["value"]
                        or accepted["provenance"] != provenance
                        or accepted["provider_reference"] != observation["provider_reference"]
                        or accepted["confidence"] != observation["confidence"]
                        or accepted["is_manual"] or accepted["is_locked"]):
                    continue
                if any(track.get(name) != after["track"].get(name) for name in _IDENTITY_COLUMNS):
                    continue
                context = [dict(row) for row in conn.execute(
                    "SELECT * FROM track_release_context WHERE track_id=? ORDER BY rowid", (int(track_id),))]
                # Ignore timestamps, but never carry a fact into a different
                # release-family/provider association.
                identity_keys = ("musicbrainz_release_group_id", "discogs_release_id", "discogs_master_id", "provider_release_family_id")
                projection = lambda rows: [tuple(row.get(key) for key in identity_keys) for row in rows]
                if projection(context) != projection(after["track_release_context"]):
                    continue
            except (KeyError, TypeError, ValueError, StopIteration):
                continue
            return True
        if (field == "title" and provider in {"musicbrainz", "musicbrainz_high_confidence"}
                and observation["provider_reference"] == track.get("musicbrainz_recording_id")
                and track.get("version_type") in (None, "unknown") and not track.get("version_label")
                and conn.execute(
                    "SELECT 1 FROM track_metadata_history WHERE track_id=? AND id>? "
                    "AND field_name IN ('version_type','version_label') LIMIT 1",
                    (int(track_id), witness["id"]),
                ).fetchone() is None):
            return True
    return False
