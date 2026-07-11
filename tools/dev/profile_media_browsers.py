from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
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
    query_artist_summaries,
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
T = TypeVar("T")


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


def _seed_database(root: Path, track_count: int) -> tuple[Path, int, int, float]:
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
        cover = None
        if album_index % 11:
            cover = str((artwork_dir / f"placeholder_{album_index % ARTWORK_COUNT:02d}.png").resolve())
        rows.append(
            (
                str((root / "synthetic_media" / f"track_{index:05d}.synthetic-audio").resolve()),
                f"Synthetic Track {index:05d}",
                f"Synthetic Artist {artist_index:04d}",
                f"Synthetic Album {album_index:04d}",
                f"Synthetic Artist {album_index % artist_count:04d}",
                str(1980 + album_index % 40),
                120.0 + index % 240,
                cover,
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
                duration_seconds, cover_path, source_kind, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    schema = int(db.conn.execute("PRAGMA user_version").fetchone()[0])
    integrity = str(db.conn.execute("PRAGMA integrity_check").fetchone()[0])
    db.close()
    if schema != CURRENT_SCHEMA_VERSION or integrity != "ok":
        raise RuntimeError("Synthetic schema validation failed.")
    seed_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    return database, album_count, artist_count, seed_ms


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
        database, requested_albums, requested_artists, seed_ms = _seed_database(
            root, track_count
        )

        with open_readonly_database(database) as conn:
            revision = browser_revision(conn)
            albums, album_query_ms = _timed(lambda: query_album_summaries(conn))
            artists, artist_query_ms = _timed(lambda: query_artist_summaries(conn))

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

        return {
            "tracks": track_count,
            "requested_albums": requested_albums,
            "requested_artists": requested_artists,
            "actual_albums": len(albums),
            "actual_artists": len(artists),
            "seed_ms": seed_ms,
            "albums": {
                "summary_query_ms": album_query_ms,
                "item_prepare_ms": album_model_prepare_ms,
                "model_create_ms": album_model_create_ms,
                "first_render_ms": album_render_ms,
                "cached_revisit_ms": album_revisit_ms,
                "model_rows": album_model.rowCount(),
                "card_widget_count": album_cards,
                "qwidget_descendants": album_descendants,
                "visible_item_count": len(album_visible),
                "summary_cache": album_cache_stats,
                "thumbnails": thumbnails,
            },
            "artists": {
                "summary_query_ms": artist_query_ms,
                "item_prepare_ms": artist_model_prepare_ms,
                "model_create_ms": artist_model_create_ms,
                "first_render_ms": artist_render_ms,
                "cached_revisit_ms": artist_revisit_ms,
                "model_rows": artist_model.rowCount(),
                "card_widget_count": artist_cards,
                "qwidget_descendants": artist_descendants,
                "visible_item_count": len(artist_visible),
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
    datasets = [_profile_dataset(app, int(track_count)) for track_count in track_counts]
    return {
        "schema_version": 1,
        "profile": "Music Vault media browsers",
        "synthetic_only": True,
        "network_used": False,
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
