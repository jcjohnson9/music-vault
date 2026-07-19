from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Sequence, TypeVar


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB  # noqa: E402
from music_vault.core.library_browser import (  # noqa: E402
    BrowserKind,
    BrowserSummaryCache,
    browser_revision,
    open_readonly_database,
    query_album_summaries,
    query_album_tracks,
    query_artist_summaries,
    query_artist_track_sections,
)
from music_vault.metadata.canonical_albums import (  # noqa: E402
    required_canonical_media_indexes,
    seed_existing_canonical_albums,
)
from music_vault.metadata.review_reclassification import (  # noqa: E402
    reclassify_stored_review_items,
)
from music_vault.ui.media_grid import (  # noqa: E402
    MediaGridModel,
    MediaGridView,
    MediaImageState,
    MediaItem,
    MediaKind,
)
from music_vault.ui.thumbnail_cache import ThumbnailCache  # noqa: E402


DEFAULT_TRACK_COUNTS = (300, 1000, 5000)
SCALE_SHAPES = {
    300: (100, 200),
    1000: (300, 600),
    5000: (1000, 2000),
}
RENDER_WIDTH = 1100
RENDER_HEIGHT = 720
ARTWORK_COUNT = 24
RECLASSIFICATION_BATCH_SIZE = 250
ALBUM_SUMMARY_SQL_STATEMENT_LIMIT = 4
ARTIST_SUMMARY_SQL_STATEMENT_LIMIT = 9
ALBUM_TRACK_SQL_STATEMENT_LIMIT = 2
ARTIST_SECTION_SQL_STATEMENT_LIMIT = 7
T = TypeVar("T")


@contextlib.contextmanager
def _network_guard():
    """Block Python network entry points for the bounded synthetic profile."""

    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_urlopen = urllib.request.urlopen

    def blocked(name: str):
        def reject(*_args, **_kwargs):
            attempts.append(name)
            raise RuntimeError(f"Synthetic media-browser profile blocked {name}")

        return reject

    socket.socket.connect = blocked("socket.connect")  # type: ignore[method-assign]
    socket.socket.connect_ex = blocked("socket.connect_ex")  # type: ignore[method-assign]
    socket.getaddrinfo = blocked("socket.getaddrinfo")  # type: ignore[assignment]
    urllib.request.urlopen = blocked("urllib.request.urlopen")  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = original_getaddrinfo  # type: ignore[assignment]
        urllib.request.urlopen = original_urlopen  # type: ignore[assignment]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile Music Vault media browsers using synthetic temporary data only."
    )
    parser.add_argument(
        "--tracks",
        type=int,
        action="append",
        help="Synthetic track count; may be repeated. Defaults to 300, 1000, and 5000.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Optional caller-selected path for a sanitized JSON result.",
    )
    return parser.parse_args(argv)


def _timed(function: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter_ns()
    value = function()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return value, round(elapsed_ms, 3)


def _timed_sql(
    conn: sqlite3.Connection,
    function: Callable[[], T],
) -> tuple[T, float, int]:
    statements: list[str] = []

    def trace(statement: str) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith("SELECT") or normalized.startswith("WITH"):
            statements.append(statement)

    conn.set_trace_callback(trace)
    try:
        value, elapsed_ms = _timed(function)
    finally:
        conn.set_trace_callback(None)
    return value, elapsed_ms, len(statements)


def _shape_for(track_count: int) -> tuple[int, int]:
    if track_count <= 0:
        raise ValueError("Synthetic track counts must be positive.")
    return SCALE_SHAPES.get(
        track_count,
        (max(1, track_count // 3), max(1, (track_count * 2) // 3)),
    )


def _generate_artwork(path: Path, index: int) -> None:
    palette = (
        ("#1DB954", "#18324A"),
        ("#5485E8", "#31245A"),
        ("#E6A83A", "#673044"),
        ("#24C77A", "#244665"),
        ("#8B65D9", "#176B66"),
        ("#D96855", "#29445B"),
    )
    first, second = palette[index % len(palette)]
    image = QImage(256, 256, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#0A0F15"))
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 256, 256)
    gradient.setColorAt(0.0, QColor(first))
    gradient.setColorAt(1.0, QColor(second))
    painter.fillRect(QRectF(0, 0, 256, 256), gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 80))
    inset = 24 + index % 18
    painter.drawEllipse(QRectF(inset, inset, 256 - inset * 2, 256 - inset * 2))
    painter.end()
    if not image.save(str(path), "PNG"):
        raise RuntimeError("Synthetic artwork generation failed.")


def _review_evidence(
    index: int, artist_index: int
) -> tuple[str, str, str, str, str]:
    title = f"Synthetic Track {index:05d}"
    artist = f"Synthetic Artist {artist_index:04d}"
    common_current = {
        "title": title,
        "artist": artist,
        "version_type": "studio",
    }
    confidence = {"title": 96.0, "artist": 96.0, "version_type": 94.0}
    scenario = index % 4
    if scenario == 0:
        proposal = {
            "_current": {**common_current, "album": "Synthetic Album", "release_date": "2001", "artwork": True},
            "title": title,
            "artist": artist,
            "version_type": "live",
            "_reasons": {"version_type": ["version_identity_conflict"]},
        }
        return "{}", json.dumps(proposal), json.dumps(confidence), "conflict", "version_conflict"
    if scenario == 1:
        proposal = {
            "_current": {**common_current, "album": "", "release_date": "", "artwork": False},
            "title": title,
            "artist": artist,
            "_artwork": {"candidate_available": False},
        }
        return "{}", json.dumps(proposal), json.dumps(confidence), "agreed", "album_ambiguity"
    if scenario == 2:
        hints = {"title": title, "artist": artist, "pattern": "artist-title"}
        proposal = {
            "_current": {**common_current, "album": "", "release_date": "", "artwork": False},
            "title": title,
            "artist": artist,
            "_artwork": {"candidate_available": False},
        }
        return json.dumps(hints), json.dumps(proposal), json.dumps(confidence), "none", "youtube_exclusive"
    proposal = {
        "_current": {**common_current, "album": "Synthetic Album", "release_date": "2001", "artwork": True},
        "title": title,
        "artist": artist,
        "version_type": "studio",
        "_artwork": {"candidate_available": False},
    }
    return "{}", json.dumps(proposal), json.dumps(confidence), "agreed", ""


def _seed_database(root: Path, track_count: int) -> tuple[Path, dict[str, object]]:
    album_count, artist_count = _shape_for(track_count)
    artwork_dir = root / "artwork"
    artwork_dir.mkdir(parents=True)
    for index in range(ARTWORK_COUNT):
        _generate_artwork(artwork_dir / f"placeholder_{index:02d}.png", index)

    database = root / "music_vault.sqlite3"
    started = time.perf_counter_ns()
    db = MusicVaultDB(database, backup_dir=root / "backups")
    rows = []
    for index in range(track_count):
        album_index = index % album_count
        artist_index = index % artist_count
        edition_cycle = (index // album_count) % 3
        album_title = f"Synthetic Album {album_index:04d}"
        if edition_cycle == 1:
            album_title += " (Deluxe Edition)"
        elif edition_cycle == 2:
            album_title += " - Reissue"
        cover = None
        if album_index % 11:
            cover = str((artwork_dir / f"placeholder_{album_index % ARTWORK_COUNT:02d}.png").resolve())
        rows.append(
            (
                str((root / "synthetic_media" / f"track_{index:05d}.synthetic-audio").resolve()),
                f"Synthetic Track {index:05d}",
                f"Synthetic Artist {artist_index:04d}",
                album_title,
                f"Synthetic Artist {album_index % artist_count:04d}",
                str(1980 + (album_index + edition_cycle * 7) % 40),
                120.0 + index % 240,
                cover,
                f"synthetic-master-{album_index:04d}",
                "local",
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
            )
        )
    with db.conn:
        db.conn.executemany(
            """
            INSERT INTO tracks (
                path, title, artist, album, album_artist, year,
                duration_seconds, cover_path, discogs_master_id, source_kind,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        track_ids = [int(row[0]) for row in db.conn.execute("SELECT id FROM tracks ORDER BY id")]
        timestamp = "2026-01-01T00:00:00Z"
        artist_rows = [
            (
                f"Synthetic Artist {index:04d}",
                f"synthetic artist {index:04d}",
                f"synthetic artist {index:04d}",
                "group" if index % 50 == 0 else "person",
                f"synthetic-discogs-{index:04d}",
                timestamp,
                timestamp,
            )
            for index in range(artist_count)
        ]
        db.conn.executemany(
            """
            INSERT INTO artists (
                display_name, normalized_name, sort_name, entity_type,
                discogs_artist_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            artist_rows,
        )
        artist_ids = [int(row[0]) for row in db.conn.execute("SELECT id FROM artists ORDER BY id")]
        alias_rows = [
            (
                artist_ids[index],
                f"SYNTHETIC ARTIST {index:04d}",
                f"synthetic artist {index:04d}",
                "display_variant",
                "synthetic_profile",
                100.0,
                timestamp,
            )
            for index in range(0, artist_count, 7)
        ]
        db.conn.executemany(
            """
            INSERT INTO artist_aliases (
                artist_id, alias_name, normalized_alias, alias_kind,
                provenance, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            alias_rows,
        )
        relationship_rows = []
        for group_index in range(0, artist_count, 50):
            for member_index in (group_index + 1, group_index + 2):
                if member_index < artist_count:
                    relationship_rows.append(
                        (
                            artist_ids[member_index],
                            artist_ids[group_index],
                            "member_of",
                            "synthetic_profile",
                            100.0,
                            timestamp,
                            timestamp,
                        )
                    )
        db.conn.executemany(
            """
            INSERT INTO artist_relationships (
                subject_artist_id, related_artist_id, relationship_kind,
                provenance, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            relationship_rows,
        )
        credit_rows = []
        for index, track_id in enumerate(track_ids):
            primary_index = index % artist_count
            credit_rows.append(
                (
                    track_id,
                    artist_ids[primary_index],
                    "primary",
                    0,
                    "",
                    "synthetic_profile",
                    100.0,
                    timestamp,
                    timestamp,
                )
            )
            if artist_count > 1 and index % 9 == 0:
                credit_rows.append(
                    (
                        track_id,
                        artist_ids[(primary_index + 1) % artist_count],
                        "featured",
                        1,
                        "feat.",
                        "synthetic_profile",
                        100.0,
                        timestamp,
                        timestamp,
                    )
                )
            if artist_count > 2 and index % 13 == 0:
                credit_rows.append(
                    (
                        track_id,
                        artist_ids[(primary_index + 2) % artist_count],
                        "collaborator",
                        2 if index % 9 == 0 else 1,
                        "x",
                        "synthetic_profile",
                        100.0,
                        timestamp,
                        timestamp,
                    )
                )
        db.conn.executemany(
            """
            INSERT INTO track_artist_credits (
                track_id, artist_id, role, credit_order, join_phrase,
                provenance, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            credit_rows,
        )
        seed_existing_canonical_albums(db.conn)

        job_id = f"synthetic-browser-profile-{track_count}"
        db.conn.execute(
            """
            INSERT INTO metadata_intelligence_jobs (
                id, job_kind, status, total_items, created_at, updated_at
            ) VALUES (?, 'existing_library', 'ready', ?, ?, ?)
            """,
            (job_id, track_count, timestamp, timestamp),
        )
        item_rows = []
        for index, track_id in enumerate(track_ids):
            hints, proposal, confidence, agreement, reason = _review_evidence(
                index, index % artist_count
            )
            item_rows.append(
                (
                    job_id,
                    track_id,
                    "review",
                    "synthetic_scale_profile",
                    hints,
                    proposal,
                    confidence,
                    agreement,
                    reason or None,
                    timestamp,
                    timestamp,
                )
            )
        db.conn.executemany(
            """
            INSERT INTO metadata_intelligence_items (
                job_id, track_id, state, reason, parsed_hints,
                field_proposal, field_confidence, provider_agreement,
                review_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            item_rows,
        )

    reclassification, reclassification_ms = _timed(
        lambda: reclassify_stored_review_items(
            db,
            job_id=job_id,
            batch_size=RECLASSIFICATION_BATCH_SIZE,
            apply=True,
        )
    )
    schema = int(db.conn.execute("PRAGMA user_version").fetchone()[0])
    integrity = str(db.conn.execute("PRAGMA integrity_check").fetchone()[0])
    counts = {
        "canonical_albums": int(db.conn.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0]),
        "album_memberships": int(db.conn.execute("SELECT COUNT(*) FROM track_album_memberships").fetchone()[0]),
        "canonical_artists": int(db.conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]),
        "artist_aliases": int(db.conn.execute("SELECT COUNT(*) FROM artist_aliases").fetchone()[0]),
        "artist_relationships": int(db.conn.execute("SELECT COUNT(*) FROM artist_relationships").fetchone()[0]),
        "featured_credits": int(db.conn.execute("SELECT COUNT(*) FROM track_artist_credits WHERE role='featured'").fetchone()[0]),
        "collaboration_credits": int(db.conn.execute("SELECT COUNT(*) FROM track_artist_credits WHERE role='collaborator'").fetchone()[0]),
        "edition_memberships": int(db.conn.execute("SELECT COUNT(*) FROM track_album_memberships WHERE edition_label IS NOT NULL").fetchone()[0]),
    }
    required_indexes = set(required_canonical_media_indexes())
    present_indexes = {
        str(row[0])
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    db.close()
    if schema != CURRENT_SCHEMA_VERSION or integrity != "ok":
        raise RuntimeError("Synthetic schema validation failed.")
    seed_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    return database, {
        "requested_albums": album_count,
        "requested_artists": artist_count,
        "seed_ms": seed_ms,
        "schema_version": schema,
        "integrity": integrity,
        "counts": counts,
        "required_index_count": len(required_indexes),
        "required_indexes_present": required_indexes <= present_indexes,
        "review_reclassification_ms": reclassification_ms,
        "review_reclassification": reclassification.to_dict(),
    }


def _album_items(summaries) -> tuple[MediaItem, ...]:
    return tuple(
        MediaItem(
            key=summary.browser_key,
            kind=MediaKind.ALBUM,
            title=summary.album_title,
            subtitle=(
                f"{summary.album_artist} · {summary.track_count} tracks"
                + (f" · {summary.canonical_year}" if summary.canonical_year else "")
            ),
            artwork_path=summary.representative_cover_path,
            image_state=(
                MediaImageState.LOADING
                if summary.representative_cover_path
                else MediaImageState.MISSING
            ),
        )
        for summary in summaries
    )


def _artist_items(summaries) -> tuple[MediaItem, ...]:
    return tuple(
        MediaItem(
            key=summary.browser_key,
            kind=MediaKind.ARTIST,
            title=summary.display_name,
            subtitle=f"{summary.track_count} tracks",
            artwork_path=None,
            image_state=MediaImageState.MISSING,
        )
        for summary in summaries
    )


def _process_until(app: QApplication, predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    app.processEvents()
    return predicate()


def _render_stage(
    app: QApplication,
    model: MediaGridModel,
) -> tuple[MediaGridView, float, int, int, tuple[str, ...]]:
    started = time.perf_counter_ns()
    view = MediaGridView()
    view.resize(RENDER_WIDTH, RENDER_HEIGHT)
    view.setModel(model)
    view.show()
    app.processEvents()
    view.viewport().grab()
    app.processEvents()
    elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    card_widgets = sum(
        1 for widget in view.findChildren(QWidget) if widget.objectName() == "BrowserCard"
    )
    descendants = len(view.findChildren(QWidget))
    return view, elapsed_ms, card_widgets, descendants, view.visible_item_keys(near_rows=1)


def _thumbnail_stage(
    app: QApplication,
    items: Sequence[MediaItem],
    visible_keys: Sequence[str],
) -> dict[str, object]:
    by_key = {item.key: item for item in items}
    sources = [
        str(by_key[key].artwork_path)
        for key in visible_keys
        if key in by_key and by_key[key].artwork_path
    ]
    cache = ThumbnailCache(max_bytes=16 * 1024 * 1024, max_workers=3)
    cache.request_visible(sources, 156, 1.0)
    finished = _process_until(app, lambda: cache.pending_count == 0, 10.0)
    if not finished:
        cache.close()
        raise RuntimeError("Synthetic thumbnail work did not finish normally.")
    # A second visible-range request measures the actual LRU revisit path.
    cache.request_visible(sources, 156, 1.0)
    app.processEvents()
    stats = asdict(cache.stats)
    cache.close()
    return {
        "visible_keys": len(visible_keys),
        "visible_sources": len(sources),
        "offscreen_sources_requested": 0,
        "stats": stats,
    }


def _cached_revisit(
    cache: BrowserSummaryCache,
    kind: BrowserKind,
    revision,
    summaries,
) -> tuple[float, dict[str, int]]:
    token = cache.token(kind, revision)
    if cache.get(kind, revision) is not None:
        raise RuntimeError("Fresh synthetic summary cache was unexpectedly populated.")
    if not cache.put(token, summaries):
        raise RuntimeError("Synthetic summary cache rejected a current result.")
    cached, elapsed_ms = _timed(lambda: cache.get(kind, revision))
    if cached is None or len(cached) != len(summaries):
        raise RuntimeError("Synthetic summary cache revisit failed.")
    return elapsed_ms, asdict(cache.stats)


def _profile_dataset(app: QApplication, track_count: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="MusicVault_Browser_Profile_") as temporary:
        root = Path(temporary)
        database, seed = _seed_database(root, track_count)
        requested_albums = int(seed["requested_albums"])
        requested_artists = int(seed["requested_artists"])

        with open_readonly_database(database) as conn:
            revision = browser_revision(conn)
            albums, album_query_ms, album_query_statements = _timed_sql(
                conn, lambda: query_album_summaries(conn)
            )
            artists, artist_query_ms, artist_query_statements = _timed_sql(
                conn, lambda: query_artist_summaries(conn)
            )
            album_tracks, album_track_query_ms, album_track_query_statements = _timed_sql(
                conn, lambda: query_album_tracks(conn, albums[0].key)
            )
            section_artist = next(
                (
                    summary
                    for summary in artists
                    if summary.group_appearance_track_count
                    or summary.featured_track_count
                    or summary.collaboration_track_count
                ),
                artists[0],
            )
            artist_sections, artist_section_query_ms, artist_section_query_statements = (
                _timed_sql(
                    conn,
                    lambda: query_artist_track_sections(conn, section_artist.key),
                )
            )
            plan_rows = conn.execute(
                "EXPLAIN QUERY PLAN SELECT track_id FROM track_album_memberships "
                "WHERE canonical_album_id=?",
                (albums[0].key.canonical_album_id,),
            ).fetchall()
            album_membership_plan = " ".join(str(row[3]) for row in plan_rows)

        album_items, album_model_prepare_ms = _timed(lambda: _album_items(albums))
        artist_items, artist_model_prepare_ms = _timed(lambda: _artist_items(artists))
        album_model, album_model_create_ms = _timed(lambda: MediaGridModel(album_items))
        artist_model, artist_model_create_ms = _timed(lambda: MediaGridModel(artist_items))

        album_view, album_render_ms, album_cards, album_descendants, album_visible = (
            _render_stage(app, album_model)
        )
        artist_view, artist_render_ms, artist_cards, artist_descendants, artist_visible = (
            _render_stage(app, artist_model)
        )
        thumbnails = _thumbnail_stage(app, album_items, album_visible)

        summary_cache = BrowserSummaryCache()
        album_revisit_ms, album_cache_stats = _cached_revisit(
            summary_cache, BrowserKind.ALBUMS, revision, albums
        )
        artist_revisit_ms, artist_cache_stats = _cached_revisit(
            summary_cache, BrowserKind.ARTISTS, revision, artists
        )

        album_view.close()
        artist_view.close()
        album_view.deleteLater()
        artist_view.deleteLater()
        album_model.deleteLater()
        artist_model.deleteLater()
        app.processEvents()

        if revision.track_count != track_count:
            raise RuntimeError("Synthetic track count changed during profiling.")
        if len(albums) != requested_albums or len(artists) != requested_artists:
            raise RuntimeError("Synthetic browser cardinality did not match its design.")
        if album_cards or artist_cards:
            raise RuntimeError("The media grid created eager BrowserCard widgets.")
        counts = seed["counts"]
        if (
            counts["canonical_albums"] != requested_albums
            or counts["album_memberships"] != track_count
            or counts["canonical_artists"] != requested_artists
        ):
            raise RuntimeError("Synthetic canonical browser seed did not reconcile.")
        # Schema capability probes and the bounded canonical identity, alias,
        # relationship, and aggregate summary queries stay constant as card
        # counts grow. A per-card query would make these totals scale with
        # results.
        if (
            album_query_statements > ALBUM_SUMMARY_SQL_STATEMENT_LIMIT
            or artist_query_statements > ARTIST_SUMMARY_SQL_STATEMENT_LIMIT
        ):
            raise RuntimeError("Synthetic browser summaries used per-card SQL queries.")
        if (
            album_track_query_statements > ALBUM_TRACK_SQL_STATEMENT_LIMIT
            or artist_section_query_statements > ARTIST_SECTION_SQL_STATEMENT_LIMIT
        ):
            raise RuntimeError("Synthetic browser details used per-track SQL queries.")
        if "idx_track_album_memberships_album" not in album_membership_plan:
            raise RuntimeError("Canonical album lookup did not use its membership index.")

        return {
            "tracks": track_count,
            "requested_albums": requested_albums,
            "requested_artists": requested_artists,
            "actual_albums": len(albums),
            "actual_artists": len(artists),
            "seed_ms": seed["seed_ms"],
            "schema_version": seed["schema_version"],
            "integrity": seed["integrity"],
            "canonical": {
                **counts,
                "required_index_count": seed["required_index_count"],
                "required_indexes_present": seed["required_indexes_present"],
                "album_membership_query_uses_index": True,
            },
            "review_reclassification": {
                **seed["review_reclassification"],
                "elapsed_ms": seed["review_reclassification_ms"],
                "batch_size": RECLASSIFICATION_BATCH_SIZE,
            },
            "albums": {
                "summary_query_ms": album_query_ms,
                "summary_sql_statement_count": album_query_statements,
                "item_prepare_ms": album_model_prepare_ms,
                "model_create_ms": album_model_create_ms,
                "first_render_ms": album_render_ms,
                "cached_revisit_ms": album_revisit_ms,
                "model_rows": album_model.rowCount(),
                "card_widget_count": album_cards,
                "qwidget_descendants": album_descendants,
                "visible_item_count": len(album_visible),
                "first_album_track_count": len(album_tracks),
                "track_query_ms": album_track_query_ms,
                "track_query_sql_statement_count": album_track_query_statements,
                "summary_cache": album_cache_stats,
                "thumbnails": thumbnails,
            },
            "artists": {
                "summary_query_ms": artist_query_ms,
                "summary_sql_statement_count": artist_query_statements,
                "item_prepare_ms": artist_model_prepare_ms,
                "model_create_ms": artist_model_create_ms,
                "first_render_ms": artist_render_ms,
                "cached_revisit_ms": artist_revisit_ms,
                "model_rows": artist_model.rowCount(),
                "card_widget_count": artist_cards,
                "qwidget_descendants": artist_descendants,
                "visible_item_count": len(artist_visible),
                "section_query_ms": artist_section_query_ms,
                "section_query_sql_statement_count": artist_section_query_statements,
                "section_track_counts": {
                    "tracks": len(artist_sections.tracks),
                    "featured_on": len(artist_sections.featured_on),
                    "collaborations": len(artist_sections.collaborations),
                    "group_appearances": len(artist_sections.group_appearances),
                },
                "summary_cache": artist_cache_stats,
                "thumbnails": {
                    "visible_keys": len(artist_visible),
                    "visible_sources": 0,
                    "offscreen_sources_requested": 0,
                    "stats": {
                        "requests": 0,
                        "hits": 0,
                        "misses": 0,
                        "coalesced": 0,
                        "decodes": 0,
                    },
                },
            },
        }


def run_profile(track_counts: Sequence[int]) -> dict[str, object]:
    app = QApplication.instance() or QApplication([])
    with _network_guard() as network_attempts:
        datasets = [_profile_dataset(app, int(track_count)) for track_count in track_counts]
    if network_attempts:
        raise RuntimeError("The synthetic media-browser profile attempted network access.")
    return {
        "schema_version": 1,
        "profile": "Music Vault media browsers",
        "synthetic_only": True,
        "network_used": False,
        "network_attempt_count": 0,
        "credential_read_count": 0,
        "runtime_database_read_count": 0,
        "media_file_write_count": 0,
        "timing_variance_is_failure": False,
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "render_size": {"width": RENDER_WIDTH, "height": RENDER_HEIGHT},
        "datasets": datasets,
    }


def _print_human(payload: dict[str, object]) -> None:
    print("Music Vault synthetic media-browser profile")
    print("Synthetic temporary data only; network disabled; timing variance is informational.")
    for dataset in payload["datasets"]:  # type: ignore[index]
        print(
            f"\n{dataset['tracks']:,} tracks | "
            f"{dataset['actual_albums']:,} albums | {dataset['actual_artists']:,} artists"
        )
        canonical = dataset["canonical"]
        reclassification = dataset["review_reclassification"]
        print(
            "  Canonical "
            f"albums {canonical['canonical_albums']:,} / artists "
            f"{canonical['canonical_artists']:,} / aliases "
            f"{canonical['artist_aliases']:,} / relationships "
            f"{canonical['artist_relationships']:,}"
        )
        print(
            "  Review    "
            f"{reclassification['scanned']:,} scanned in "
            f"{reclassification['elapsed_ms']:.3f} ms / "
            f"{reclassification['applied_with_gaps']:,} gaps / "
            f"{reclassification['source_fallback']:,} source fallback / "
            f"{reclassification['needs_review']:,} critical review"
        )
        for label in ("albums", "artists"):
            result = dataset[label]
            thumbnails = result["thumbnails"]
            stats = thumbnails["stats"]
            print(
                f"  {label.title():7} query {result['summary_query_ms']:8.3f} ms | "
                f"items {result['item_prepare_ms']:8.3f} ms | "
                f"model {result['model_create_ms']:8.3f} ms | "
                f"first render {result['first_render_ms']:8.3f} ms | "
                f"cached revisit {result['cached_revisit_ms']:8.3f} ms"
            )
            print(
                f"           rows {result['model_rows']:,} | card QWidgets "
                f"{result['card_widget_count']} | visible {result['visible_item_count']} | "
                f"thumbnail requests {stats['requests']} / decodes {stats['decodes']} / "
                f"hits {stats['hits']}"
            )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    track_counts = tuple(args.tracks or DEFAULT_TRACK_COUNTS)
    try:
        payload = run_profile(track_counts)
        _print_human(payload)
        if args.json is not None:
            _write_json(args.json, payload)
            print("\nSanitized JSON profile written to the caller-selected path.")
        return 0
    except Exception as exc:
        print(
            f"Media-browser profiler structural failure: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
