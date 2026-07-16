from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB  # noqa: E402
from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator  # noqa: E402
from music_vault.core.playlist_membership import PlaylistMembershipService  # noqa: E402
from music_vault.core.sync_result import (  # noqa: E402
    MultiSourceSyncResult,
    SyncResult,
    utc_now,
)
from music_vault.core.sync_sources import SyncSourceService  # noqa: E402
from music_vault.ui.sync_center import SyncCenterWidget  # noqa: E402


T = TypeVar("T")
PROFILE_CASES = (
    ("1_source_100_items", 1, 100, 100),
    ("10_sources_1000_unique", 10, 1000, 1200),
    ("50_sources_5000_memberships", 50, 2500, 5000),
)
MAX_CASE_SECONDS = 60.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Profile persistent multiple-source structures with temporary, "
            "synthetic SQLite data only. No network or media scan is used."
        )
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Optional sanitized JSON output (temporary or below .ui-review only).",
    )
    parser.add_argument(
        "--case",
        choices=tuple(case[0] for case in PROFILE_CASES),
        action="append",
        help="Run selected scale case(s); defaults to all required cases.",
    )
    return parser.parse_args(argv)


def _timed(function: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter_ns()
    result = function()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return result, round(elapsed_ms, 3)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_json_path(path: Path) -> Path:
    destination = path.expanduser().resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    review = (PROJECT_ROOT / ".ui-review").resolve()
    if not _is_relative_to(destination, temp) and not _is_relative_to(
        destination, review
    ):
        raise ValueError("Profile JSON is allowed only in TEMP or below .ui-review/.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _insert_tracks(db: MusicVaultDB, root: Path, unique_count: int) -> list[int]:
    rows = [
        (
            str((root / "synthetic_media" / f"video_{index:05d}.synthetic-audio").resolve()),
            f"Synthetic Scale Track {index:05d}",
            "Synthetic Scale Artist",
            "Synthetic Scale Album",
            "youtube",
            f"scale{index:06d}",
            "2026-07-15T00:00:00Z",
            "2026-07-15T00:00:00Z",
        )
        for index in range(unique_count)
    ]
    with db.conn:
        db.conn.executemany(
            """
            INSERT INTO tracks (
                path, title, artist, album, source_kind, source_video_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        track_rows = db.conn.execute(
            "SELECT id, source_video_id FROM tracks ORDER BY id"
        ).fetchall()
        timestamp = utc_now()
        db.conn.executemany(
            """
            INSERT INTO source_track_identities (
                source_kind, external_track_id, track_id, first_seen_at, updated_at
            ) VALUES ('youtube', ?, ?, ?, ?)
            """,
            [
                (str(row["source_video_id"]), int(row["id"]), timestamp, timestamp)
                for row in track_rows
            ],
        )
    return [int(row["id"]) for row in track_rows]


def _source_sizes(source_count: int, membership_count: int) -> list[int]:
    base, remainder = divmod(membership_count, source_count)
    return [base + (1 if index < remainder else 0) for index in range(source_count)]


def profile_case(
    app: QApplication,
    root: Path,
    *,
    name: str,
    source_count: int,
    unique_video_count: int,
    membership_count: int,
) -> dict[str, object]:
    case_started = time.perf_counter()
    db, open_ms = _timed(
        lambda: MusicVaultDB(root / "music_vault.sqlite3", backup_dir=root / "backups")
    )
    membership = PlaylistMembershipService(db)
    sources = SyncSourceService(db, membership_service=membership)
    try:
        track_ids, track_seed_ms = _timed(
            lambda: _insert_tracks(db, root, unique_video_count)
        )
        source_rows: list[object] = []
        source_memberships: dict[int, list[tuple[int, int]]] = {}
        item_rows: list[tuple[object, ...]] = []
        timestamp = utc_now()
        cursor = 0

        def seed_sources() -> None:
            nonlocal cursor
            for source_index, item_count in enumerate(
                _source_sizes(source_count, membership_count)
            ):
                playlist_id = db.create_playlist(
                    f"Synthetic Scale Playlist {source_index:03d}"
                )
                source = sources.create_source(
                    f"PLSCALE{source_index:06d}",
                    label=f"Synthetic Source {source_index:03d}",
                    destination_kind="playlist",
                    destination_playlist_id=playlist_id,
                )
                source_rows.append(source)
                positions: list[tuple[int, int]] = []
                for position in range(item_count):
                    track_index = cursor % unique_video_count
                    cursor += 1
                    track_id = track_ids[track_index]
                    video_id = f"scale{track_index:06d}"
                    positions.append((track_id, position))
                    item_rows.append(
                        (
                            source.id,
                            f"scale-item-{source_index:03d}-{position:06d}",
                            video_id,
                            position,
                            "Synthetic Scale Item",
                            "available",
                            track_id,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                        )
                    )
                source_memberships[source.id] = positions
            with db.conn:
                db.conn.executemany(
                    """
                    INSERT INTO sync_source_items (
                        source_id, source_item_id, video_id, source_position,
                        source_title, availability_status, track_id, first_seen_at,
                        last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    item_rows,
                )

        _, source_seed_ms = _timed(seed_sources)

        def materialize_all() -> None:
            for source in source_rows:
                membership.set_source_origins(
                    source.id,
                    int(source.destination_playlist_id),
                    source_memberships[source.id],
                )

        _, materialize_ms = _timed(materialize_all)

        def reconcile_all() -> None:
            for source in source_rows:
                membership.reconcile_source(source.id)

        _, reconcile_ms = _timed(reconcile_all)

        widget = SyncCenterWidget()
        widget.resize(1280, 720)

        def render_sources() -> None:
            widget.set_sources(sources.list_active())
            widget.apply_review_state(
                "sources",
                sources=sources.list_active(),
                summary={"enabled_sources": source_count},
                activity=tuple(f"Synthetic activity {index}" for index in range(250)),
            )
            widget.show()
            app.processEvents()
            widget.grab()
            app.processEvents()

        _, source_render_ms = _timed(render_sources)
        per_source_widgets = sum(
            widget.source_list.indexWidget(widget.source_list.model().index(row, 0))
            is not None
            for row in range(widget.source_list.count())
        )
        bounded_activity_lines = int(widget.source_activity.document().blockCount())
        widget.close()

        outcomes = [
            SyncResult(
                status="complete",
                playlist_id=None,
                playlist_title=None,
                visible_item_count=len(source_memberships[source.id]),
                existing_count=len(source_memberships[source.id]),
                saved_source_id=source.id,
            )
            for source in source_rows
        ]
        aggregate, aggregate_ms = _timed(
            lambda: MultiSourceSyncResult.from_outcomes(
                outcomes,
                selected_source_count=source_count,
                started_at=utc_now(),
                batch_token="synthetic-scale-batch",
            )
        )

        query_plan = tuple(
            str(row[3])
            for row in db.conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT track_id, MIN(source_position)
                FROM sync_source_items
                WHERE source_id=? AND removed_at IS NULL AND track_id IS NOT NULL
                GROUP BY track_id
                ORDER BY MIN(source_position), track_id
                """,
                (int(source_rows[0].id),),
            )
        )
        indexes = {
            str(row["name"])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        required_indexes = {
            "idx_sync_sources_active_order",
            "idx_sync_source_items_source_position",
            "idx_sync_source_items_present",
            "idx_playlist_origins_source",
            "idx_playlist_origins_playlist_order",
            "idx_source_track_identities_track",
        }
        materialized_count = int(
            db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
        )
        origin_count = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM playlist_track_origins "
                "WHERE origin_kind='sync_source'"
            ).fetchone()[0]
        )
        source_item_count = int(
            db.conn.execute("SELECT COUNT(*) FROM sync_source_items").fetchone()[0]
        )
        distinct_identity_count = int(
            db.conn.execute(
                "SELECT COUNT(DISTINCT video_id) FROM sync_source_items"
            ).fetchone()[0]
        )
        integrity = str(db.conn.execute("PRAGMA integrity_check").fetchone()[0])
        schema = int(db.conn.execute("PRAGMA user_version").fetchone()[0])
        elapsed_seconds = time.perf_counter() - case_started

        metrics = {
            "name": name,
            "source_count": source_count,
            "unique_video_count": unique_video_count,
            "membership_row_count": source_item_count,
            "overlapping_membership_count": source_item_count - distinct_identity_count,
            "materialized_track_count": materialized_count,
            "source_origin_count": origin_count,
            "schema_version": schema,
            "integrity": integrity,
            "open_ms": open_ms,
            "track_seed_ms": track_seed_ms,
            "source_seed_ms": source_seed_ms,
            "playlist_materialize_ms": materialize_ms,
            "source_reconcile_ms": reconcile_ms,
            "source_list_render_ms": source_render_ms,
            "batch_aggregate_ms": aggregate_ms,
            "aggregate_visible_count": aggregate.total_visible,
            "per_source_widget_count": per_source_widgets,
            "bounded_activity_line_count": bounded_activity_lines,
            "indexed_source_query": any(
                "idx_sync_source_items" in detail for detail in query_plan
            ),
            "required_indexes_present": required_indexes.issubset(indexes),
            "elapsed_seconds": round(elapsed_seconds, 3),
        }
        if not (
            schema == CURRENT_SCHEMA_VERSION == 6
            and integrity == "ok"
            and source_item_count == membership_count
            and distinct_identity_count == unique_video_count
            and origin_count == materialized_count
            and per_source_widgets == 0
            and bounded_activity_lines <= 100
            and metrics["indexed_source_query"] is True
            and metrics["required_indexes_present"] is True
            and elapsed_seconds <= MAX_CASE_SECONDS
        ):
            raise RuntimeError(f"Synthetic scale case failed structural checks: {name}")
        return metrics
    finally:
        db.close()


def _global_scan_structure() -> dict[str, bool]:
    run_source = inspect.getsource(MultiSourceSyncOrchestrator._run)
    one_source = inspect.getsource(MultiSourceSyncOrchestrator._run_one_source)
    return {
        "one_batch_global_media_scan": run_source.count("scan_existing_downloads(") == 1,
        "shared_media_index_passed_per_source": "media_index=media_index" in run_source,
        "known_download_index_injected": "known_downloads=" in one_source,
        "sequential_source_loop": "for index, source in enumerate" in run_source,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selected = set(args.case or (case[0] for case in PROFILE_CASES))
    cases = [case for case in PROFILE_CASES if case[0] in selected]
    app = QApplication.instance() or QApplication([])
    roots: list[Path] = []
    try:
        results: list[dict[str, object]] = []
        for name, sources, unique, memberships in cases:
            root = Path(tempfile.mkdtemp(prefix=f"MusicVault_Batch10_Profile_{name}_"))
            roots.append(root)
            results.append(
                profile_case(
                    app,
                    root,
                    name=name,
                    source_count=sources,
                    unique_video_count=unique,
                    membership_count=memberships,
                )
            )
        structure = _global_scan_structure()
        if any(value is not True for value in structure.values()):
            raise RuntimeError("Multi-source orchestration lost its bounded global-scan shape.")
        payload = {
            "schema_version": 1,
            "status": "complete",
            "synthetic_only": True,
            "network_attempt_count": 0,
            "media_file_scan_count": 0,
            "cases": results,
            "structure": structure,
        }
        encoded = json.dumps(payload, indent=2) + "\n"
        if args.json is not None:
            _safe_json_path(args.json).write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    finally:
        for root in roots:
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
