from __future__ import annotations

from pathlib import Path

from music_vault.core import library_browser
from music_vault.core.db import MusicVaultDB
from music_vault.core.library_browser import (
    ArtistKey,
    browser_revision,
    query_artist_summaries,
    query_artist_track_sections,
    query_artist_tracks,
)


def _track(db: MusicVaultDB, root: Path, name: str, artist: str | None) -> int:
    path = root / f"{name}.synthetic-audio"
    db.upsert_track(path, title=name, artist=artist, album="Synthetic Album")
    return int(
        db.conn.execute(
            "SELECT id FROM tracks WHERE path = ?", (str(path.resolve()),)
        ).fetchone()[0]
    )


def _artist(
    db: MusicVaultDB,
    name: str,
    *,
    entity_type: str = "person",
) -> int:
    cursor = db.conn.execute(
        """
        INSERT INTO artists(
            display_name, normalized_name, sort_name, entity_type,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (name, library_browser.normalize_identity(name), name, entity_type),
    )
    return int(cursor.lastrowid)


def _credit(
    db: MusicVaultDB,
    track_id: int,
    artist_id: int,
    role: str,
    order: int,
    join_phrase: str = "",
) -> None:
    db.conn.execute(
        """
        INSERT INTO track_artist_credits(
            track_id, artist_id, role, credit_order, join_phrase,
            provenance, confidence, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, 'synthetic_provider', 100.0,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (track_id, artist_id, role, order, join_phrase),
    )


def _structured_library(tmp_path: Path) -> tuple[MusicVaultDB, dict[str, int]]:
    db = MusicVaultDB(tmp_path / "browser.sqlite3", backup_dir=tmp_path / "backups")

    ordinary = _track(db, tmp_path, "ordinary", "The Synthetic Duo")
    featured = _track(
        db,
        tmp_path,
        "featured",
        "The Synthetic Duo feat. Featured Guest",
    )
    collaboration = _track(
        db,
        tmp_path,
        "collaboration",
        "The Synthetic Duo & Joint Partner",
    )
    missing = _track(db, tmp_path, "missing", None)
    literal = _track(db, tmp_path, "literal", "Unknown Artist")
    performer_only = _track(db, tmp_path, "performer", "Uploader Provenance")

    # ``upsert_track`` deliberately creates one conservative primary credit so
    # every newly materialized artist string has a structured fallback.  This
    # fixture replaces those fallback rows with an explicit synthetic provider
    # graph; clear the generated graph first so its entities do not distort the
    # browser counts being exercised here.
    db.conn.execute("DELETE FROM track_artist_credits")
    db.conn.execute("DELETE FROM artists")

    ensemble = _artist(db, "The Synthetic Duo", entity_type="duo")
    guest = _artist(db, "Featured Guest")
    partner = _artist(db, "Joint Partner", entity_type="group")
    literal_unknown = _artist(db, "Unknown Artist", entity_type="unknown")
    performer = _artist(db, "Credits-Only Performer")

    _credit(db, ordinary, ensemble, "primary", 0)
    _credit(db, featured, ensemble, "primary", 0, " feat. ")
    _credit(db, featured, guest, "featured", 1)
    _credit(db, collaboration, ensemble, "primary", 0, " & ")
    _credit(db, collaboration, partner, "collaborator", 1)
    _credit(db, literal, literal_unknown, "primary", 0)
    _credit(db, performer_only, performer, "performer", 0)
    db.conn.commit()
    return db, {
        "ordinary": ordinary,
        "featured": featured,
        "collaboration": collaboration,
        "missing": missing,
        "literal": literal,
    }


def test_artist_summaries_use_structured_roles_and_distinct_counts(tmp_path: Path):
    db, ids = _structured_library(tmp_path)
    try:
        summaries = {item.key.normalized_name: item for item in query_artist_summaries(db.conn)}

        primary = summaries["the synthetic duo"]
        assert primary.display_name == "The Synthetic Duo"
        assert primary.entity_type == "duo"
        assert primary.track_count == 3
        assert primary.primary_track_count == 3
        assert primary.featured_track_count == 0
        assert primary.collaboration_track_count == 0

        featured = summaries["featured guest"]
        assert featured.track_count == 0
        assert featured.featured_track_count == 1
        assert featured.collaboration_track_count == 0

        collaborator = summaries["joint partner"]
        assert collaborator.track_count == 0
        assert collaborator.featured_track_count == 0
        assert collaborator.collaboration_track_count == 1

        assert "uploader provenance" not in summaries
        assert "credits-only performer" not in summaries
        assert summaries[""].display_name == "Unknown Artist"
        assert summaries[""].track_count == 1
        assert summaries["unknown artist"].track_count == 1
        assert summaries[""].browser_key != summaries["unknown artist"].browser_key
        assert ids["missing"] != ids["literal"]
    finally:
        db.close()


def test_artist_track_sections_keep_featured_and_collaborator_out_of_primary(
    tmp_path: Path,
):
    db, ids = _structured_library(tmp_path)
    try:
        primary = query_artist_track_sections(db.conn, ArtistKey("the synthetic duo"))
        assert {row["id"] for row in primary.tracks} == {
            ids["ordinary"],
            ids["featured"],
            ids["collaboration"],
        }
        assert primary.featured_on == ()
        assert primary.collaborations == ()

        featured = query_artist_track_sections(db.conn, ArtistKey("featured guest"))
        assert featured.tracks == ()
        assert [row["id"] for row in featured.featured_on] == [ids["featured"]]
        assert featured.collaborations == ()
        assert query_artist_tracks(db.conn, ArtistKey("featured guest")) == ()

        collaborator = query_artist_track_sections(db.conn, ArtistKey("joint partner"))
        assert collaborator.tracks == ()
        assert collaborator.featured_on == ()
        assert [row["id"] for row in collaborator.collaborations] == [
            ids["collaboration"]
        ]
    finally:
        db.close()


def test_artist_image_keys_remain_normalized_name_compatible(tmp_path: Path):
    db, _ = _structured_library(tmp_path)
    try:
        key = ArtistKey("featured guest")
        summaries = query_artist_summaries(
            db.conn,
            image_states={key.browser_key: "resolved"},
        )
        guest = next(item for item in summaries if item.key == key)
        assert guest.browser_key == ArtistKey("featured guest").browser_key
        assert guest.image_state == "resolved"
    finally:
        db.close()


def test_artist_browser_revision_and_queries_use_credit_indexes(tmp_path: Path):
    db, _ = _structured_library(tmp_path)
    try:
        revision = browser_revision(db.conn)
        assert revision.artist_count == 5
        assert revision.artist_credit_count == 7

        plan_rows = db.conn.execute(
            "EXPLAIN QUERY PLAN " + library_browser._ARTIST_TRACKS_V6_SQL,
            (1, None, None, None, "featured guest", None, "featured guest"),
        ).fetchall()
        plan = "\n".join(str(row[3]) for row in plan_rows)
        assert "idx_artist_credits_artist_role_track" in plan
        assert "idx_artist_credits_track_order" in plan

        extra_artist = _artist(db, "Revision Artist")
        extra_track = _track(db, tmp_path, "revision", "Revision Artist")
        seeded_credit = db.conn.execute(
            "SELECT artist_id FROM track_artist_credits WHERE track_id=?",
            (extra_track,),
        ).fetchone()
        assert seeded_credit is not None
        assert int(seeded_credit[0]) == extra_artist
        db.conn.commit()
        assert browser_revision(db.conn) != revision
    finally:
        db.close()
