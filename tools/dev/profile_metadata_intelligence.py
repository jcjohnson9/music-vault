from __future__ import annotations

"""Batch 10.1 metadata-intelligence scale profile using temporary data only."""

import argparse
import inspect
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB  # noqa: E402
from music_vault.core.library_browser import (  # noqa: E402
    query_artist_summaries,
    query_artist_track_sections,
)
from music_vault.core.playlist_membership import PlaylistMembershipService  # noqa: E402
from music_vault.core.sync_result import utc_now  # noqa: E402
from music_vault.core.sync_sources import SyncSourceService  # noqa: E402
from music_vault.metadata.intelligence import MetadataIntelligenceService  # noqa: E402
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore  # noqa: E402
from music_vault.metadata.providers.discogs import (  # noqa: E402
    MAX_BACKOFF_SECONDS,
    RAW_CACHE_MAX_AGE_SECONDS,
    DiscogsProvider,
)
from tools.dev.synthetic_metadata_providers import (  # noqa: E402
    SyntheticDiscogsProvider,
    SyntheticMusicBrainzProvider,
    SyntheticTokenStore,
)


T = TypeVar("T")
PROFILE_CASES = (("300_tracks", 300), ("1000_tracks", 1_000))
SOURCE_COUNT = 3
MAX_CASE_SECONDS = 120.0
MAX_ARTIST_QUERY_MS = 5_000.0
MAX_STORED_SUMMARY_BYTES_PER_ITEM = 32 * 1024
MAX_PROVIDER_QUERIES_PER_TRACK = 6


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Profile Batch 10.1 with injected offline providers and temporary "
            "schema-v6 data. No token, network, media, or live library is used."
        )
    )
    parser.add_argument(
        "--case",
        choices=tuple(name for name, _count in PROFILE_CASES),
        action="append",
        help="Run selected scale case(s); defaults to 300 and 1,000 tracks.",
    )
    parser.add_argument("--json", type=Path, help="Optional sanitized JSON output.")
    return parser.parse_args(argv)


def _timed(function: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter_ns()
    result = function()
    return result, round((time.perf_counter_ns() - started) / 1_000_000, 3)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_json_path(path: Path) -> Path:
    destination = path.expanduser().resolve()
    allowed = (
        Path(tempfile.gettempdir()).resolve(),
        (PROJECT_ROOT / ".ui-review").resolve(),
    )
    if not any(_is_relative_to(destination, parent) for parent in allowed):
        raise ValueError("Profile JSON is allowed only in TEMP or .ui-review/.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _seed_tracks(db: MusicVaultDB, root: Path, count: int) -> list[int]:
    track_ids: list[int] = []
    with db.conn:
        for index in range(count):
            track_ids.append(
                db.upsert_track(
                    root / "synthetic-media" / f"scale-{index:05d}.synthetic-audio",
                    title=f"Synthetic Scale Unit - Scale Signal {index:05d}",
                    artist="Synthetic Scale Unit",
                    album="Imported Placeholder",
                    duration_seconds=180.0 + index % 60,
                    source_kind="youtube",
                    source_video_id=f"scale{index:06d}",
                    commit=False,
                )
            )
    return track_ids


def _seed_overlapping_sources(db: MusicVaultDB, track_ids: Sequence[int]) -> int:
    memberships = PlaylistMembershipService(db)
    sources = SyncSourceService(db, membership_service=memberships)
    timestamp = utc_now()
    for source_index in range(SOURCE_COUNT):
        playlist_id = db.create_playlist(f"Synthetic Profile Playlist {source_index + 1}")
        source = sources.create_source(
            f"PLMETADATAPROFILE{source_index:02d}",
            label=f"Synthetic Profile Source {source_index + 1}",
            destination_kind="playlist",
            destination_playlist_id=playlist_id,
        )
        rows = [
            (
                source.id,
                f"synthetic-item-{source_index:02d}-{position:05d}",
                f"scale{position:06d}",
                position,
                "Synthetic source item",
                "available",
                int(track_id),
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            )
            for position, track_id in enumerate(track_ids)
        ]
        with db.conn:
            db.conn.executemany(
                """
                INSERT INTO sync_source_items (
                    source_id, source_item_id, video_id, source_position,
                    source_title, availability_status, track_id, first_seen_at,
                    last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            memberships.set_source_origins(
                source.id,
                playlist_id,
                ((track_id, position) for position, track_id in enumerate(track_ids)),
                commit=False,
            )
    return len(track_ids) * SOURCE_COUNT


def _settings() -> dict[str, object]:
    return {
        "metadata_intelligence_enabled": True,
        "metadata_discogs_enabled": True,
        "metadata_musicbrainz_secondary_enabled": True,
        "metadata_writeback_enabled": False,
        "metadata_fill_missing_artwork_enabled": False,
        "metadata_scan_existing_after_setup": False,
        "metadata_intelligence_consent_version": 1,
        "metadata_discogs_consent_version": 1,
    }


def _production_provider_guards() -> dict[str, bool]:
    request_source = inspect.getsource(DiscogsProvider._request_json)
    return {
        "production_rate_limiter_waits_before_request": "rate_limiter.wait" in request_source,
        "production_backoff_is_bounded": MAX_BACKOFF_SECONDS <= 60.0,
        "raw_memory_cache_is_bounded_to_six_hours": RAW_CACHE_MAX_AGE_SECONDS <= 6 * 60 * 60,
    }


def profile_case(root: Path, *, name: str, track_count: int) -> dict[str, object]:
    case_started = time.perf_counter()
    database_path = root / "data" / "music_vault.sqlite3"
    backup_dir = root / "data" / "backups"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    db = MusicVaultDB(database_path, backup_dir=backup_dir)
    discogs = SyntheticDiscogsProvider(latency_seconds=0.0002)
    musicbrainz = SyntheticMusicBrainzProvider(latency_seconds=0.0002)
    try:
        track_ids, track_seed_ms = _timed(lambda: _seed_tracks(db, root, track_count))
        expected_memberships, source_seed_ms = _timed(
            lambda: _seed_overlapping_sources(db, track_ids)
        )
        store = MetadataIntelligenceJobStore(db)
        job_id, job_create_ms = _timed(store.create_existing_library_job)
        initial_total = store.aggregate_counts(job_id)["total"]

        # Simulate an interrupted worker before closing the creator connection.
        claimed = store.claim_next_item(job_id)
        if claimed is None:
            raise RuntimeError("Synthetic job did not expose a claimable item.")
        db.close()

        reopened = MusicVaultDB(database_path, backup_dir=backup_dir)
        try:
            recovered = MetadataIntelligenceJobStore(reopened).recover_interrupted(job_id)
        finally:
            reopened.close()
        if recovered != 1:
            raise RuntimeError("Interrupted synthetic job was not persisted and recovered.")

        service_handle = MusicVaultDB(database_path, backup_dir=backup_dir)
        service = MetadataIntelligenceService(
            service_handle,
            _settings(),
            token_store=SyntheticTokenStore(),
            discogs_provider_factory=lambda _token: discogs,
            musicbrainz_provider_factory=lambda: musicbrainz,
        )
        worker_result: dict[str, object] = {}
        worker_error: list[BaseException] = []

        def run_worker() -> None:
            try:
                worker_result["thread_id"] = threading.get_ident()
                worker_result["result"] = service.analyze_existing_library()
            except BaseException as exc:  # pragma: no cover - reported as gate failure
                worker_error.append(exc)

        worker = threading.Thread(target=run_worker, name=f"metadata-profile-{name}")
        main_thread_id = threading.get_ident()
        started = time.perf_counter_ns()
        worker.start()
        heartbeat_count = 0
        while worker.is_alive():
            heartbeat_count += 1
            time.sleep(0.002)
        worker.join()
        process_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
        service_handle.close()
        if worker_error:
            raise RuntimeError("Synthetic metadata worker failed.") from worker_error[0]

        checked = MusicVaultDB(database_path, backup_dir=backup_dir)
        try:
            summary = MetadataIntelligenceJobStore(checked).job_summary(job_id)
            item_stats = checked.conn.execute(
                """
                SELECT COUNT(*) AS item_count,
                       COUNT(DISTINCT track_id) AS distinct_tracks,
                       MAX(attempt_count) AS max_attempts,
                       MAX(LENGTH(parsed_hints) + LENGTH(field_proposal)
                           + LENGTH(field_confidence)) AS max_summary_bytes
                FROM metadata_intelligence_items WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            membership_count = int(
                checked.conn.execute("SELECT COUNT(*) FROM sync_source_items").fetchone()[0]
            )
            origin_count = int(
                checked.conn.execute(
                    "SELECT COUNT(*) FROM playlist_track_origins "
                    "WHERE origin_kind='sync_source'"
                ).fetchone()[0]
            )
            materialized_count = int(
                checked.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
            )
            provider_cache_count = int(
                checked.conn.execute("SELECT COUNT(*) FROM metadata_provider_cache").fetchone()[0]
            )
            raw_marker_count = int(
                checked.conn.execute(
                    """
                    SELECT COUNT(*) FROM metadata_intelligence_items
                    WHERE lower(field_proposal) LIKE '%raw_response%'
                       OR lower(field_proposal) LIKE '%authorization%'
                       OR lower(field_proposal) LIKE '%token%'
                    """
                ).fetchone()[0]
            )
            summaries, artist_summary_ms = _timed(
                lambda: query_artist_summaries(checked.conn)
            )
            if not summaries:
                raise RuntimeError("Structured artist summaries were not generated.")
            _sections, artist_sections_ms = _timed(
                lambda: query_artist_track_sections(checked.conn, summaries[0].key)
            )
            integrity = str(checked.conn.execute("PRAGMA integrity_check").fetchone()[0])
            schema = int(checked.conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            checked.close()

        elapsed_seconds = time.perf_counter() - case_started
        metrics: dict[str, object] = {
            "name": name,
            "track_count": track_count,
            "source_count": SOURCE_COUNT,
            "source_membership_count": membership_count,
            "overlapping_membership_count": membership_count - track_count,
            "playlist_origin_count": origin_count,
            "materialized_playlist_track_count": materialized_count,
            "job_item_count": int(item_stats["item_count"]),
            "job_distinct_track_count": int(item_stats["distinct_tracks"]),
            "job_total_before_processing": int(initial_total),
            "job_status": summary.status,
            "job_max_attempt_count": int(item_stats["max_attempts"] or 0),
            "discogs_query_count": len(discogs.calls),
            "musicbrainz_query_count": len(musicbrainz.calls),
            "maximum_parallel_discogs_calls": discogs.maximum_parallel_calls,
            "max_persisted_summary_bytes": int(item_stats["max_summary_bytes"] or 0),
            "provider_cache_row_count": provider_cache_count,
            "raw_or_secret_marker_count": raw_marker_count,
            "worker_thread_isolated": worker_result.get("thread_id") != main_thread_id,
            "main_thread_heartbeat_count": heartbeat_count,
            "artist_summary_query_ms": artist_summary_ms,
            "artist_sections_query_ms": artist_sections_ms,
            "schema_version": schema,
            "integrity": integrity,
            "track_seed_ms": track_seed_ms,
            "source_seed_ms": source_seed_ms,
            "job_create_ms": job_create_ms,
            "metadata_process_ms": process_ms,
            "elapsed_seconds": round(elapsed_seconds, 3),
        }
        metrics.update(_production_provider_guards())
        checks = {
            "schema_is_current": schema == CURRENT_SCHEMA_VERSION,
            "integrity_ok": integrity == "ok",
            "one_item_per_canonical_track": (
                int(item_stats["item_count"]) == track_count
                and int(item_stats["distinct_tracks"]) == track_count
            ),
            "source_memberships_not_multiplied_into_jobs": initial_total == track_count,
            "source_memberships_preserved": (
                membership_count == expected_memberships
                and origin_count == expected_memberships
                and materialized_count == expected_memberships
            ),
            "bounded_provider_queries_per_track": (
                track_count <= len(discogs.calls)
                <= track_count * MAX_PROVIDER_QUERIES_PER_TRACK
                and track_count <= len(musicbrainz.calls)
                <= track_count * MAX_PROVIDER_QUERIES_PER_TRACK
            ),
            "provider_calls_are_sequential": discogs.maximum_parallel_calls <= 1,
            "job_persistence_recovered": int(item_stats["max_attempts"] or 0) == 2,
            "job_completed": summary.total_items == track_count
            and summary.status in {"complete", "complete_with_issues"},
            "bounded_persisted_summaries": (
                int(item_stats["max_summary_bytes"] or 0)
                <= MAX_STORED_SUMMARY_BYTES_PER_ITEM
            ),
            "no_raw_or_secret_markers": raw_marker_count == 0,
            "no_raw_provider_cache": provider_cache_count == 0,
            "worker_connection_kept_ui_thread_available": (
                bool(metrics["worker_thread_isolated"]) and heartbeat_count > 0
            ),
            "artist_queries_bounded": (
                artist_summary_ms <= MAX_ARTIST_QUERY_MS
                and artist_sections_ms <= MAX_ARTIST_QUERY_MS
            ),
            "production_rate_limit_guard_present": all(
                _production_provider_guards().values()
            ),
            "case_time_bounded": elapsed_seconds <= MAX_CASE_SECONDS,
        }
        metrics["checks"] = checks
        if not all(checks.values()):
            failed = sorted(name for name, passed in checks.items() if not passed)
            raise RuntimeError(
                "Synthetic metadata scale case failed aggregate checks: "
                + ",".join(failed)
            )
        return metrics
    finally:
        try:
            db.close()
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selected = set(args.case or (name for name, _count in PROFILE_CASES))
    roots: list[Path] = []
    try:
        results: list[dict[str, object]] = []
        for name, count in PROFILE_CASES:
            if name not in selected:
                continue
            root = Path(tempfile.mkdtemp(prefix=f"MusicVault_Batch10_1_Profile_{name}_"))
            roots.append(root)
            results.append(profile_case(root, name=name, track_count=count))
        payload = {
            "schema_version": 1,
            "status": "complete",
            "synthetic_only": True,
            "network_attempt_count": 0,
            "secret_file_read_count": 0,
            "media_file_write_count": 0,
            "cases": results,
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.json is not None:
            _safe_json_path(args.json).write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=False)


if __name__ == "__main__":
    os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"
    raise SystemExit(main())
