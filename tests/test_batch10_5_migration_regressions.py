from __future__ import annotations

from pathlib import Path

from music_vault.core import paths as runtime_paths
from music_vault.core.db import MusicVaultDB
from tools.dev import run_batch10_3_source_migration_proof as source_proof


def test_schema6_migration_preserves_credit_roles_and_groups_album_editions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Lock the two cross-service regressions found during Batch 10.5 review."""

    runtime = (tmp_path / "synthetic-runtime").resolve()
    data = runtime / "data"
    backups = data / "backups"
    data.mkdir(parents=True)
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# synthetic migration marker\n", encoding="utf-8")

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_DISABLE_NETWORK", "1")
    monkeypatch.setattr(runtime_paths, "_configured_data_directory", None)
    runtime_paths._resolved_project_root.cache_clear()

    database = data / "music_vault.sqlite3"
    source_proof._create_synthetic_schema6(database, backups, runtime)
    migrated = MusicVaultDB(database, backup_dir=backups)
    try:
        base_track_id = int(
            migrated.conn.execute(
                "SELECT id FROM tracks WHERE album='Fixture Record'"
            ).fetchone()[0]
        )
        credits = [
            tuple(row)
            for row in migrated.conn.execute(
                """
                SELECT artist.display_name, credit.role, credit.credit_order
                FROM track_artist_credits AS credit
                JOIN artists AS artist ON artist.id=credit.artist_id
                WHERE credit.track_id=?
                ORDER BY credit.credit_order, credit.id
                """,
                (base_track_id,),
            ).fetchall()
        ]
        assert credits == [
            ("Fixture Ensemble", "primary", 0),
            ("Fixture Perspective", "featured", 1),
        ]
        assert migrated.conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT artist_id
                FROM track_artist_credits
                WHERE track_id=?
                GROUP BY artist_id
                HAVING COUNT(DISTINCT role) > 1
            )
            """,
            (base_track_id,),
        ).fetchone()[0] == 0

        album_rows = migrated.conn.execute(
            """
            SELECT album.id, COUNT(membership.track_id) AS track_count,
                   COUNT(DISTINCT COALESCE(
                       NULLIF(membership.discogs_release_id, ''),
                       NULLIF(membership.edition_label, ''),
                       'base'
                   )) AS edition_count
            FROM canonical_albums AS album
            JOIN track_album_memberships AS membership
              ON membership.canonical_album_id=album.id
            WHERE album.normalized_title='fixture record'
            GROUP BY album.id
            """
        ).fetchall()
        assert len(album_rows) == 1
        assert tuple(album_rows[0])[1:] == (2, 2)
        assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        migrated.close()

    reopened = MusicVaultDB(database, backup_dir=backups)
    try:
        assert reopened.migration_performed is False
        assert reopened.conn.execute(
            """
            SELECT COUNT(DISTINCT canonical_album_id)
            FROM track_album_memberships
            WHERE track_id IN (
                SELECT id FROM tracks
                WHERE album IN ('Fixture Record', 'Fixture Record (Deluxe Edition)')
            )
            """
        ).fetchone()[0] == 1
    finally:
        reopened.close()
        runtime_paths._resolved_project_root.cache_clear()
