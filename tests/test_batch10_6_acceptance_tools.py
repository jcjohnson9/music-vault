from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import requests

from music_vault.core import db as db_module
from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_credits import ArtistCreditService
from music_vault.metadata.ensemble import recording_group_key
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.orientation_repair import (
    ORIENTATION_REPAIR_MARKER,
    OrientationRepairError,
    OrientationResolution,
    RepairArtistCredit,
    apply_orientation_repair,
    discover_orientation_repair_targets,
    require_exact_orientation_repair_target,
)
from music_vault.metadata.service import MetadataService
from tools.dev import batch10_6_acceptance as gate


RAW_TITLE = "Anthem of the Republic - The Cosmic Assembly (1978)"
WRONG_TITLE = "The Cosmic Assembly"
WRONG_ARTIST = "Anthem of the Republic"
CORRECT_TITLE = "Anthem of the Republic"
CORRECT_ARTIST = "The Cosmic Assembly"


@contextmanager
def _schema7_database_runtime():
    """Build genuine historical schema-7 fixtures for the Batch 10.6 gate."""
    original_version = db_module.CURRENT_SCHEMA_VERSION
    original_create = db_module.create_media_quality_schema
    original_seed = db_module.seed_existing_track_media_quality
    db_module.CURRENT_SCHEMA_VERSION = 7
    db_module.create_media_quality_schema = lambda _connection: None
    db_module.seed_existing_track_media_quality = lambda _connection, *_track_ids: None
    try:
        yield
    finally:
        db_module.CURRENT_SCHEMA_VERSION = original_version
        db_module.create_media_quality_schema = original_create
        db_module.seed_existing_track_media_quality = original_seed


def _add_target(
    db: MusicVaultDB,
    root: Path,
    *,
    index: int = 1,
    acceptance_qualified: bool = True,
) -> int:
    media = root / "synthetic media" / f"track-{index}.synthetic-audio"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(f"fictional-audio-{index}".encode("ascii"))
    cover = root / "data" / "covers" / f"cover-{index}.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(f"fictional-cover-{index}".encode("ascii"))
    if acceptance_qualified:
        raw = (
            RAW_TITLE
            if index == 1
            else f"Fictional Anthem {index} - Fictional Group {index} (1978)"
        )
    else:
        raw = f"Fictional Anthem {index} - Fictional Group {index}"
    wrong_title = WRONG_TITLE if index == 1 else f"Fictional Group {index}"
    wrong_artist = WRONG_ARTIST if index == 1 else f"Fictional Anthem {index}"
    video_id = f"synthetic{index:02d}"
    track_id = db.upsert_track(
        media,
        title=raw,
        cover_path=str(cover),
        duration_seconds=222.0,
        source_kind="youtube",
        source_video_id=video_id,
        source_upload_date="2020-01-01",
    )
    MetadataService(db).record_source_observations(
        track_id,
        provider="youtube_title_parsed",
        values={"title": wrong_title, "artist": wrong_artist},
        provider_reference=video_id,
        confidence=70.0,
        apply_effective=True,
        reason="synthetic_source_fallback",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "source_fallback",
        parsed_hints={
            "raw_title": raw,
            "title": wrong_title,
            "artist": wrong_artist,
            "pattern": "artist_dash_title",
            **(
                {"year": 1978}
                if acceptance_qualified
                else {}
            ),
        },
        field_proposal={
            "_current": {"title": wrong_title, "artist": wrong_artist},
            "_discogs": {},
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={},
        provider_agreement="none",
        review_reason="provider_adjudication_deferred",
        file_write_result="not_requested",
        artwork_result="not_requested",
    )
    return track_id


def _runtime(
    tmp_path: Path,
    *,
    targets: int = 1,
) -> tuple[Path, Path, Path, tuple[int, ...]]:
    root = tmp_path / "synthetic Batch 10.6 runtime"
    data = root / "data"
    cache = data / "artist_images"
    cache.mkdir(parents=True)
    database = data / "music_vault.sqlite3"
    with _schema7_database_runtime():
        db = MusicVaultDB(database, backup_dir=data / "backups")
        db.conn.execute(
            "INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)",
            ("batch10_5_metadata_acceptance_repair_v1", "1"),
        )
        target_ids = tuple(
            _add_target(db, root, index=index + 1) for index in range(targets)
        )
        extra = root / "synthetic media" / "unrelated.synthetic-audio"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"unrelated-fictional-audio")
        unrelated_id = db.upsert_track(
            extra,
            title="Unrelated Fictional Song",
            artist="Unrelated Fictional Artist",
            album="Unrelated Fictional Album",
        )
        playlist_id = db.create_playlist("Synthetic Preservation Playlist")
        db.add_track_to_playlist(playlist_id, unrelated_id)
        if target_ids:
            db.add_track_to_playlist(playlist_id, target_ids[0])
        db.close()
    (cache / "index.json").write_text(
        json.dumps({"schema_version": 1, "entries": {}, "aliases": {}}),
        encoding="utf-8",
    )
    return root, database, cache, target_ids


def _resolution() -> OrientationResolution:
    return OrientationResolution(
        provider="discogs",
        selected_orientation="right_is_artist",
        title=CORRECT_TITLE,
        artist=CORRECT_ARTIST,
        coherent=True,
        confidence=97.0,
        field_confidences=tuple(
            (name, 97.0)
            for name in (
                "title",
                "artist",
                "artist_credits",
                "album",
                "album_artist",
                "release_date",
                "original_release_date",
                "version_type",
                "discogs_release_id",
                "discogs_master_id",
                "discogs_track_position",
                "provider_release_family_id",
                "release_country",
                "release_format",
                "label_name",
            )
        ),
        orientation_evaluated_count=2,
        orientation_reasons=("only_coherent_discogs_orientation",),
        provider_confirmed=True,
        requires_provider_adjudication=False,
        discogs_queries=2,
        musicbrainz_queries=0,
        provider_reference="https://www.discogs.com/release/987654",
        artist_credits=(
            RepairArtistCredit(
                CORRECT_ARTIST,
                role="primary",
                entity_type="group",
                provider_artist_id="456789",
            ),
        ),
        album="Fictional Catalogue Album",
        album_artist=CORRECT_ARTIST,
        release_date="1978",
        original_release_date="1978",
        version_type="studio",
        discogs_release_id="987654",
        discogs_master_id="876543",
        discogs_track_position="A1",
        provider_release_family_id="discogs:master:876543",
        release_country="US",
        release_format="Vinyl",
        label_name="Fictional Records",
    )


def _policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")


def _database_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_structural_discovery_selects_exactly_one_without_public_identity(
    tmp_path: Path,
) -> None:
    _root, database, _cache, target_ids = _runtime(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        report, targets = discover_orientation_repair_targets(connection)
    finally:
        connection.close()

    assert report.exact_target_count == 1
    assert report.candidate_items_inspected == 1
    assert report.strong_dash_items == 1
    assert report.crosswise_current_value_items == 1
    assert report.empty_provider_proposal_items == 1
    assert report.year_hint_items == 1
    assert report.version_qualified_items == 0
    assert report.acceptance_qualified_items == 1
    assert targets[0].track_id == target_ids[0]
    assert RAW_TITLE not in repr(targets[0])
    assert CORRECT_ARTIST not in repr(targets[0])


def test_structural_discovery_filters_many_broad_candidates_to_one(
    tmp_path: Path,
) -> None:
    root, database, _cache, target_ids = _runtime(tmp_path)
    db = MusicVaultDB(database, backup_dir=root / "data" / "backups")
    try:
        for index in range(2, 45):
            _add_target(
                db,
                root,
                index=index,
                acceptance_qualified=False,
            )
    finally:
        db.close()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        report, targets = discover_orientation_repair_targets(connection)
    finally:
        connection.close()

    assert report.candidate_items_inspected == 44
    assert report.strong_dash_items == 44
    assert report.crosswise_current_value_items == 44
    assert report.empty_provider_proposal_items == 44
    assert report.acceptance_qualified_items == 1
    assert report.exact_target_count == 1
    assert targets[0].track_id == target_ids[0]


@pytest.mark.parametrize("target_count", [0, 2])
def test_zero_or_multiple_targets_fail_closed(
    tmp_path: Path,
    target_count: int,
) -> None:
    _root, database, _cache, _ids = _runtime(tmp_path, targets=target_count)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        with pytest.raises(
            OrientationRepairError,
            match="orientation_repair_target_count_not_one",
        ):
            require_exact_orientation_repair_target(connection)
    finally:
        connection.close()


def test_dry_run_is_read_only_aggregate_only_and_provider_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _policy(monkeypatch)
    root, database, cache, _ids = _runtime(tmp_path)
    before = _database_hash(database)

    report = gate.run_dry_run(
        project_root=root,
        database=database,
        cache_root=cache,
    )

    encoded = json.dumps(report, sort_keys=True)
    assert report["dry_run"] is True
    assert report["source_runtime_unchanged"] is True
    assert report["proposal"]["exact_target_count"] == 1
    assert report["provider_requests"] == 0
    assert report["media_writes"] == 0
    assert _database_hash(database) == before
    assert RAW_TITLE not in encoded
    assert CORRECT_ARTIST not in encoded


def test_core_repair_is_one_transaction_and_marker_first_idempotent(
    tmp_path: Path,
) -> None:
    _root, database, _cache, target_ids = _runtime(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        target = require_exact_orientation_repair_target(connection)
        source_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM source_track_identities ORDER BY source_kind,external_track_id"
            )
        ]
        playlists_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM playlist_tracks ORDER BY playlist_id,track_id"
            )
        ]

        first = apply_orientation_repair(
            connection,
            target=target,
            resolution=_resolution(),
        )
        second = apply_orientation_repair(connection)

        row = connection.execute(
            "SELECT title,artist,album,cover_path,path FROM tracks WHERE id=?",
            (target_ids[0],),
        ).fetchone()
        credits = connection.execute(
            """
            SELECT artist.display_name,credit.role
            FROM track_artist_credits AS credit
            JOIN artists AS artist ON artist.id=credit.artist_id
            WHERE credit.track_id=? ORDER BY credit.credit_order,credit.id
            """,
            (target_ids[0],),
        ).fetchall()
        source_after = [
            tuple(item)
            for item in connection.execute(
                "SELECT * FROM source_track_identities ORDER BY source_kind,external_track_id"
            )
        ]
        playlists_after = [
            tuple(item)
            for item in connection.execute(
                "SELECT * FROM playlist_tracks ORDER BY playlist_id,track_id"
            )
        ]

        assert tuple(row[:3]) == (
            CORRECT_TITLE,
            CORRECT_ARTIST,
            "Fictional Catalogue Album",
        )
        assert row[3] is not None and row[4] is not None
        assert [tuple(item) for item in credits] == [(CORRECT_ARTIST, "primary")]
        assert connection.execute(
            "SELECT COUNT(*) FROM track_album_memberships WHERE track_id=?",
            (target_ids[0],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=?",
            (target_ids[0],),
        ).fetchone()[0] >= 2
        assert connection.execute(
            "SELECT COUNT(*) FROM app_meta WHERE key=?",
            (ORIENTATION_REPAIR_MARKER,),
        ).fetchone()[0] == 1
        evidence = json.loads(
            connection.execute(
                "SELECT parsed_hints FROM metadata_intelligence_items WHERE id=?",
                (target.item_id,),
            ).fetchone()[0]
        )["orientation"]
        assert evidence == {
            "confidence": 97.0,
            "discogs_queries": 2,
            "evaluated_count": 2,
            "musicbrainz_queries": 0,
            "provider_confirmed": True,
            "reasons": ["only_coherent_discogs_orientation"],
            "requires_provider_adjudication": False,
            "selected": "right_is_artist",
        }
        assert first.targets_repaired == 1
        assert first.history_rows_added >= 2
        assert second.no_op is True
        assert source_after == source_before
        assert playlists_after == playlists_before
    finally:
        connection.close()


def test_manual_identity_lock_fails_without_partial_write(
    tmp_path: Path,
) -> None:
    _root, database, _cache, target_ids = _runtime(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "UPDATE track_metadata_fields SET is_locked=1 "
        "WHERE track_id=? AND field_name='title'",
        (target_ids[0],),
    )
    connection.commit()
    target = require_exact_orientation_repair_target(connection)
    before = tuple(
        connection.execute(
            "SELECT title,artist,album FROM tracks WHERE id=?", (target_ids[0],)
        ).fetchone()
    )

    with pytest.raises(OrientationRepairError, match="blocked_by_lock"):
        apply_orientation_repair(
            connection,
            target=target,
            resolution=_resolution(),
        )

    after = tuple(
        connection.execute(
            "SELECT title,artist,album FROM tracks WHERE id=?", (target_ids[0],)
        ).fetchone()
    )
    assert after == before
    assert connection.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (ORIENTATION_REPAIR_MARKER,)
    ).fetchone()[0] == 0
    connection.close()


def test_low_confidence_secondary_fields_are_not_promoted(
    tmp_path: Path,
) -> None:
    _root, database, _cache, target_ids = _runtime(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    target = require_exact_orientation_repair_target(connection)
    resolution = replace(
        _resolution(),
        field_confidences=(
            ("title", 97.0),
            ("artist", 97.0),
            ("artist_credits", 97.0),
            ("album", 70.0),
            ("album_artist", 70.0),
            ("release_date", 70.0),
            ("original_release_date", 70.0),
            ("version_type", 70.0),
            ("discogs_release_id", 70.0),
            ("discogs_master_id", 70.0),
            ("discogs_track_position", 70.0),
            ("provider_release_family_id", 70.0),
        ),
    )

    result = apply_orientation_repair(
        connection,
        target=target,
        resolution=resolution,
    )
    row = connection.execute(
        """
        SELECT title,artist,album,release_date,version_type,
               discogs_release_id,discogs_master_id,recording_group_key
        FROM tracks WHERE id=?
        """,
        (target_ids[0],),
    ).fetchone()

    assert tuple(row) == (
        CORRECT_TITLE,
        CORRECT_ARTIST,
        None,
        None,
        None,
        None,
        None,
        recording_group_key(CORRECT_TITLE, CORRECT_ARTIST),
    )
    assert row["recording_group_key"] != recording_group_key(
        CORRECT_TITLE,
        CORRECT_ARTIST,
        master_id=_resolution().discogs_master_id,
    )
    assert result.canonical_album_memberships == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM track_release_context WHERE track_id=?",
        (target_ids[0],),
    ).fetchone()[0] == 0
    connection.close()


def test_release_context_preserves_existing_values_when_incoming_is_missing(
    tmp_path: Path,
) -> None:
    _root, database, _cache, target_ids = _runtime(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        INSERT INTO track_release_context(
            track_id,release_country,release_format,catalog_number,label_name,
            updated_at
        ) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
        """,
        (target_ids[0], "CA", "Cassette", "SYN-001", "Preserved Label"),
    )
    connection.commit()
    target = require_exact_orientation_repair_target(connection)
    resolution = replace(
        _resolution(),
        release_country=None,
        release_format=None,
        label_name=None,
    )

    apply_orientation_repair(
        connection,
        target=target,
        resolution=resolution,
    )
    context = connection.execute(
        """
        SELECT release_country,release_format,catalog_number,label_name
        FROM track_release_context WHERE track_id=?
        """,
        (target_ids[0],),
    ).fetchone()
    assert tuple(context) == ("CA", "Cassette", "SYN-001", "Preserved Label")
    connection.close()


def test_exception_rolls_back_metadata_credits_item_and_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, database, _cache, target_ids = _runtime(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    target = require_exact_orientation_repair_target(connection)
    before_track = tuple(
        connection.execute(
            "SELECT title,artist,album FROM tracks WHERE id=?", (target_ids[0],)
        ).fetchone()
    )
    before_state = connection.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (target.item_id,)
    ).fetchone()[0]

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic_credit_failure")

    monkeypatch.setattr(ArtistCreditService, "replace_track_credits", fail)
    with pytest.raises(RuntimeError, match="synthetic_credit_failure"):
        apply_orientation_repair(
            connection,
            target=target,
            resolution=_resolution(),
        )

    assert tuple(
        connection.execute(
            "SELECT title,artist,album FROM tracks WHERE id=?", (target_ids[0],)
        ).fetchone()
    ) == before_track
    assert connection.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (target.item_id,)
    ).fetchone()[0] == before_state
    assert connection.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (ORIENTATION_REPAIR_MARKER,)
    ).fetchone()[0] == 0
    connection.close()


def test_live_gate_uses_one_fake_target_creates_backup_and_reports_no_private_values(
    tmp_path: Path,
) -> None:
    root, database, cache, _ids = _runtime(tmp_path)
    calls: list[int] = []

    def fake_lookup(target):
        calls.append(target.track_id)
        return gate.TargetedLookupResult(
            resolution=_resolution(),
            request_count=4,
            discogs_orientation_searches=2,
            musicbrainz_searches=0,
        )

    result = gate.apply_live_repair(
        project_root=root,
        database=database,
        cache_root=cache,
        acknowledgement=gate.LIVE_ACKNOWLEDGEMENT,
        provider_lookup=fake_lookup,
    )
    backup = root / "data" / "backups" / result["database_backup"]["name"]
    encoded = json.dumps(result, sort_keys=True)

    assert len(calls) == 1
    assert result["tracks_looked_up"] == 1
    assert result["provider_requests"] == 4
    assert result["discogs_orientation_searches"] == 2
    assert result["repair"]["targets_repaired"] == 1
    assert result["second_run_no_op"] is True
    assert result["media_unchanged"] is True
    assert result["cover_files_unchanged"] is True
    assert result["portrait_cache_unchanged"] is True
    assert backup.is_file()
    assert backup.stat().st_size > 0
    assert RAW_TITLE not in encoded
    assert CORRECT_ARTIST not in encoded
    assert "synthetic Batch 10.6 runtime" not in encoded

    backup_count = len(list((root / "data" / "backups").glob("*.sqlite3")))

    def forbidden_lookup(_target):
        raise AssertionError("marker must prevent provider construction")

    second = gate.apply_live_repair(
        project_root=root,
        database=database,
        cache_root=cache,
        acknowledgement=gate.LIVE_ACKNOWLEDGEMENT,
        provider_lookup=forbidden_lookup,
    )
    assert second["no_op"] is True
    assert second["provider_requests"] == 0
    assert len(list((root / "data" / "backups").glob("*.sqlite3"))) == backup_count


def test_live_acknowledgement_is_checked_before_runtime_inspection(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(gate.Batch106Failure, match="acknowledgement_missing"):
        gate.apply_live_repair(
            project_root=missing,
            database=missing / "data" / "music_vault.sqlite3",
            cache_root=missing / "data" / "artist_images",
            acknowledgement="wrong",
            provider_lookup=lambda _target: _resolution(),
        )


def test_wrapper_defaults_to_dry_run_and_has_exact_apply_acknowledgement() -> None:
    source = Path("tools/dev/run_batch10_6_live_repair.ps1").read_text(
        encoding="utf-8"
    )
    assert '[string]$Mode = "DryRun"' in source
    assert "$AcknowledgeTargetedLookup" in source
    assert "$AcknowledgeLiveRepair" not in source
    assert "--acknowledge-targeted-lookup" in source
    assert gate.LIVE_ACKNOWLEDGEMENT in source
    assert "Get-Process -Name MusicVault" in source
    assert ".venv\\Scripts\\python.exe" in source
    assert "batch10_5_live_repair" not in source


def test_bounded_session_revalidates_redirect_style_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def fake_send(_self, request, **_kwargs):
        sent.append(str(request.url))
        return object()

    monkeypatch.setattr(requests.Session, "send", fake_send)
    counter = {"http": 0}
    session = gate._BoundedSession(counter)
    allowed = requests.Request(
        "GET", "https://api.discogs.com/database/search"
    ).prepare()
    forbidden_redirect = requests.Request(
        "GET", "https://example.invalid/redirected"
    ).prepare()

    session.send(allowed)
    assert counter["http"] == 1
    assert len(sent) == 1
    with pytest.raises(gate.Batch106Failure, match="endpoint_not_allowed"):
        session.send(forbidden_redirect)
    assert counter["http"] == 1
    assert len(sent) == 1
