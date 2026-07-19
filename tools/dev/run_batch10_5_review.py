from __future__ import annotations

"""Bounded, offline Batch 10.5 production-surface UI review.

The review uses only fictional rows in a disposable TEMP project root.  It
exercises the real browser/detail/dialog surfaces while socket activity,
credential reads, and access to the live runtime are blocked.  Captures are
deleted by default.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.dev import run_batch10_3_review as base  # noqa: E402


OUTPUT_PREFIX = "MusicVault_Batch10_5_UI_Review_"
RUNTIME_PREFIX = "MusicVault_Batch10_5_UI_Runtime_"
OWNER_MARKER = ".music_vault_batch10_5_review_owner.json"
RUNTIME_OWNER_MARKER = ".music_vault_batch10_5_runtime_owner.json"
SYNTHETIC_MBID = "15151515-1515-4151-8151-151515151515"
SYNTHETIC_DISCOGS_ID = "10501"


@dataclass(frozen=True, slots=True)
class ReviewScene:
    name: str
    width: int
    height: int
    scale: float
    purpose: str
    required_terms: tuple[str, ...]


SCENES = (
    ReviewScene(
        "canonical_artist_grid",
        1280,
        720,
        1.0,
        "Artist grid contains one card for the complementary-ID cluster",
        ("Artists", "Glass Horizon"),
    ),
    ReviewScene(
        "preferred_cached_portrait",
        1920,
        1080,
        1.0,
        "Canonical artist uses the cached MusicBrainz-linked Wikimedia portrait",
        ("Artists", "Glass Horizon"),
    ),
    ReviewScene(
        "canonical_artist_tracks",
        1280,
        720,
        1.0,
        "Canonical cluster detail renders primary tracks",
        ("Glass Horizon", "Tracks", "Harbor Lights"),
    ),
    ReviewScene(
        "artist_featured_on",
        1280,
        720,
        1.0,
        "Canonical cluster detail renders Featured On",
        ("Glass Horizon", "Featured On", "Quiet Relay"),
    ),
    ReviewScene(
        "artist_collaborations",
        1920,
        1080,
        1.0,
        "Canonical cluster detail renders Collaborations",
        ("Glass Horizon", "Collaborations", "Parallel Current"),
    ),
    ReviewScene(
        "artist_group_appearances",
        1280,
        720,
        1.0,
        "Canonical cluster detail renders verified group appearances",
        ("Glass Horizon", "Group Appearances", "Assembly Signal"),
    ),
    ReviewScene(
        "metadata_zero_review",
        1280,
        720,
        1.0,
        "Metadata dashboard reports no ordinary pending-review outcome",
        ("Applied with Gaps", "Accepted Source Fallback", "Pending: 0"),
    ),
    ReviewScene(
        "singles_uncatalogued_150",
        1280,
        720,
        1.5,
        "One virtual Singles & Uncatalogued collection at 150 percent scale",
        ("Albums", "Singles & Uncatalogued"),
    ),
)


@dataclass(slots=True)
class ReviewRuntime:
    root: Path
    owner_token: str
    track_ids: dict[str, int] = field(default_factory=dict)
    provider_request_count: int = 0
    window: Any = None
    intelligence_dialog: Any = None
    original_audio_method: Any = None
    cache_snapshot: dict[str, str] = field(default_factory=dict)
    cover_snapshot: dict[int, str | None] = field(default_factory=dict)
    seed_evidence: dict[str, int] = field(default_factory=dict)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-captures", action="store_true")
    parser.add_argument("--offscreen", action="store_true")
    return parser.parse_args(argv)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _write_owner_marker(path: Path, marker_name: str) -> str:
    token = uuid.uuid4().hex
    (path / marker_name).write_text(
        json.dumps({"schema_version": 1, "token": token}) + "\n",
        encoding="utf-8",
    )
    return token


def _output_directory(requested: Path | None) -> tuple[Path, str]:
    if requested is None:
        output = Path(tempfile.mkdtemp(prefix=OUTPUT_PREFIX)).resolve()
    else:
        output = requested.expanduser().resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        ignored_review = (PROJECT_ROOT / ".ui-review").resolve()
        allowed = _is_relative_to(output, ignored_review) or (
            _is_relative_to(output, temp) and output.name.startswith(OUTPUT_PREFIX)
        )
        if not allowed:
            raise ValueError("Output is allowed only in TEMP or .ui-review/.")
        if output.exists() and any(output.iterdir()):
            raise ValueError("Refusing to use a non-empty review directory.")
        output.mkdir(parents=True, exist_ok=True)
    return output, _write_owner_marker(output, OWNER_MARKER)


def _owned_output(output: Path, token: str) -> Path:
    resolved = output.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    ignored_review = (PROJECT_ROOT / ".ui-review").resolve()
    if resolved.is_symlink() or not (
        (_is_relative_to(resolved, temp) and resolved.name.startswith(OUTPUT_PREFIX))
        or _is_relative_to(resolved, ignored_review)
    ):
        raise RuntimeError("Refusing to clean an unverified review directory.")
    try:
        marker = json.loads((resolved / OWNER_MARKER).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Review ownership marker is unavailable.") from exc
    if marker.get("token") != token:
        raise RuntimeError("Review ownership marker does not match this run.")
    return resolved


def _runtime_directory() -> ReviewRuntime:
    runtime = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX)).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if not _is_relative_to(runtime, temp) or _is_relative_to(runtime, PROJECT_ROOT):
        raise RuntimeError("Synthetic runtime did not resolve to an isolated TEMP root.")
    (runtime / "run.py").write_text("# synthetic Batch 10.5 review root\n", encoding="utf-8")
    (runtime / "music_vault").mkdir()
    (runtime / "data" / "youtube_downloads").mkdir(parents=True)
    (runtime / "data" / "covers").mkdir()
    (runtime / "profile" / "LocalAppData").mkdir(parents=True)
    (runtime / "profile" / "RoamingAppData").mkdir(parents=True)
    (runtime / "profile" / "Temp").mkdir(parents=True)
    source_icons = PROJECT_ROOT / "assets" / "icons"
    if source_icons.is_dir():
        shutil.copytree(source_icons, runtime / "assets" / "icons")
    return ReviewRuntime(runtime, _write_owner_marker(runtime, RUNTIME_OWNER_MARKER))


def _owned_runtime(runtime: ReviewRuntime) -> Path:
    resolved = runtime.root.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if (
        resolved.is_symlink()
        or not _is_relative_to(resolved, temp)
        or not resolved.name.startswith(RUNTIME_PREFIX)
        or _is_relative_to(resolved, PROJECT_ROOT)
    ):
        raise RuntimeError("Refusing to clean an unverified synthetic runtime.")
    marker = json.loads(
        (resolved / RUNTIME_OWNER_MARKER).read_text(encoding="utf-8")
    )
    if marker.get("token") != runtime.owner_token:
        raise RuntimeError("Synthetic runtime ownership marker does not match this run.")
    return resolved


@contextmanager
def _review_environment(runtime: Path) -> Iterator[None]:
    values = {
        "MUSIC_VAULT_PROJECT_ROOT": str(runtime),
        "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS": "1",
        "MUSIC_VAULT_DISABLE_NETWORK": "1",
        "MUSIC_VAULT_UI_REVIEW": "batch10_5_production_surface",
        "MUSIC_VAULT_ARTIST_IMAGE_PROVIDER": "synthetic",
        "HOME": str(runtime / "profile"),
        "USERPROFILE": str(runtime / "profile"),
        "LOCALAPPDATA": str(runtime / "profile" / "LocalAppData"),
        "APPDATA": str(runtime / "profile" / "RoamingAppData"),
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        from music_vault.core import paths

        paths._resolved_project_root.cache_clear()
        yield
    finally:
        from music_vault.core import paths

        paths._resolved_project_root.cache_clear()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _file_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed_batch10_5(runtime: ReviewRuntime) -> None:
    """Extend the proven Batch 10.3 fixture with Batch 10.5 edge cases."""

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    from music_vault.core.db import MusicVaultDB
    from music_vault.metadata.artist_credits import ArtistCreditService
    from music_vault.metadata.artist_images import (
        ArtistIdentity,
        ArtistImageCache,
        ArtistImageResult,
        ArtistImageStatus,
        SyntheticArtistImageProvider,
    )
    from music_vault.metadata.artist_relationships import ArtistRelationshipService
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    from music_vault.metadata.review_reclassification import best_available_reclassify

    root = runtime.root
    base._seed_runtime(runtime)
    db = MusicVaultDB(root / "data" / "music_vault.sqlite3")
    credits = ArtistCreditService(db)

    target_row = db.conn.execute(
        "SELECT id FROM artists WHERE normalized_name='glass horizon' ORDER BY id LIMIT 1"
    ).fetchone()
    if target_row is None:
        raise RuntimeError("Synthetic canonical artist fixture is unavailable.")
    target_id = int(target_row[0])
    db.conn.execute(
        "UPDATE artists SET entity_type='person',discogs_artist_id=? WHERE id=?",
        (SYNTHETIC_DISCOGS_ID, target_id),
    )
    duplicate_id = int(
        db.conn.execute(
            """
            INSERT INTO artists (
                display_name,normalized_name,sort_name,entity_type,
                musicbrainz_artist_id,created_at,updated_at
            ) VALUES ('Glass Horizon','glass horizon','Glass Horizon','person',?,
                      CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (SYNTHETIC_MBID,),
        ).lastrowid
    )
    db.conn.execute(
        """
        UPDATE track_artist_credits SET artist_id=?
        WHERE track_id=? AND role='featured' AND artist_id=?
        """,
        (duplicate_id, runtime.track_ids["quiet_relay"], target_id),
    )

    group_track_path = root / "data" / "youtube_downloads" / "assembly-signal.synthetic-audio"
    group_track_path.write_bytes(b"Music Vault synthetic Batch 10.5 fixture\n")
    group_track = db.upsert_track(
        group_track_path,
        title="Assembly Signal",
        artist="Harbor Assembly",
        album="Assembly Lines",
        album_artist="Harbor Assembly",
        source_kind="local",
    )
    runtime.track_ids["assembly_signal"] = group_track
    group = credits.upsert_artist("Harbor Assembly", entity_type="group")
    credits.replace_track_credits(
        group_track,
        ({"display_name": "Harbor Assembly", "role": "primary", "entity_type": "group"},),
        provenance="manual",
        provider_reference="manual:synthetic-batch10-5-review",
        confidence=100,
        is_manual=True,
        is_locked=True,
        actor="synthetic_ui_review",
        reason="synthetic_ui_fixture",
    )
    ArtistRelationshipService(db).record_manual_member_of(
        member_artist_id=target_id,
        group_artist_id=group.id,
        confirmation_reference="manual:synthetic-batch10-5-review",
    )

    for key, title, artist in (
        ("loose_signal_one", "Loose Signal One", "Glass Horizon"),
        ("loose_signal_two", "Loose Signal Two", "Harbor Assembly"),
    ):
        path = root / "data" / "youtube_downloads" / f"{key}.synthetic-audio"
        path.write_bytes(b"Music Vault synthetic Batch 10.5 fixture\n")
        runtime.track_ids[key] = db.upsert_track(
            path,
            title=title,
            artist=artist,
            album=None,
            source_kind="local",
        )

    backwards_path = root / "data" / "youtube_downloads" / "backwards.synthetic-audio"
    backwards_path.write_bytes(b"Music Vault synthetic Batch 10.5 fixture\n")
    backwards_id = db.upsert_track(
        backwards_path,
        title="Band Name",
        artist="Song Name",
        album=None,
        source_kind="youtube",
        source_video_id="batch10_5_backwards",
    )
    runtime.track_ids["backwards"] = backwards_id
    store = MetadataIntelligenceJobStore(db)
    backwards_job = store.create_existing_library_job([backwards_id])
    item = store.claim_next_item(backwards_job)
    if item is None:
        raise RuntimeError("Synthetic backwards-title item could not be claimed.")
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "raw_title": "Song Name - Band Name (1978)",
            "title": "Band Name",
            "artist": "Song Name",
            "pattern": "artist_dash_title",
            "year": 1978,
        },
        field_proposal={
            "_current": {"title": "Band Name", "artist": "Song Name"},
            "_discogs": {
                "title": "Song Name",
                "artist": "Band Name",
                "album": "Catalogue Record",
                "original_release_date": "1978",
                "score": 78,
                "provider_reference": "https://www.discogs.com/release/10501",
            },
            "_musicbrainz": {},
            "_sources": {},
            "_reasons": {"title": ["provider_value_conflict"]},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={
            "title": 78,
            "artist": 78,
            "album": 78,
            "original_release_date": 78,
        },
        provider_agreement="discogs_only",
        review_reason="title_ambiguity",
    )
    db.conn.commit()
    repair = best_available_reclassify(db, apply=True)
    # Keep one latest synthetic dashboard job with each normal terminal
    # acceptance outcome visible. The rows reuse fictional tracks and do not
    # invoke providers or write media metadata.
    dashboard_job = store.create_existing_library_job(
        [
            backwards_id,
            runtime.track_ids["review_gap"],
            runtime.track_ids["review_fallback"],
        ]
    )
    dashboard_items = [store.claim_next_item(dashboard_job) for _ in range(3)]
    if any(item is None for item in dashboard_items):
        raise RuntimeError("Synthetic terminal-outcome dashboard could not be seeded.")
    dashboard_current = {"title": "Synthetic Theme", "artist": "Review Ensemble"}
    for claimed, state in zip(
        dashboard_items,
        ("applied", "applied_with_gaps", "source_fallback"),
        strict=True,
    ):
        store.mark_item(
            claimed.id,
            state,
            parsed_hints=dashboard_current,
            field_proposal={
                "_current": dashboard_current,
                "_discogs": {"title": "Synthetic Theme", "score": 82}
                if state != "source_fallback"
                else {},
                "_musicbrainz": {},
                "_artwork": {"candidate_available": False},
                "_reasons": {},
            },
            field_confidence={"title": 82} if state == "applied" else {},
            provider_agreement=(
                "discogs_only" if state != "source_fallback" else "none"
            ),
            review_reason=(
                None
                if state == "applied"
                else ("album_unavailable" if state == "applied_with_gaps" else "source_fallback")
            ),
        )
    db.conn.execute(
        "INSERT OR REPLACE INTO app_meta(key,value) VALUES "
        "('batch10_5_metadata_acceptance_repair_v1','synthetic_acceptance_complete')"
    )
    db.conn.commit()
    runtime.seed_evidence = {
        "reclassified_count": int(repair.changed),
        "orientation_repair_count": int(repair.reversed_orientation_repairs),
        "remaining_review_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state IN ('review','ready')"
            ).fetchone()[0]
        ),
        "applied_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='applied'"
            ).fetchone()[0]
        ),
        "applied_with_gaps_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='applied_with_gaps'"
            ).fetchone()[0]
        ),
        "source_fallback_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='source_fallback'"
            ).fetchone()[0]
        ),
        "applied_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='applied'"
            ).fetchone()[0]
        ),
        "applied_with_gaps_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='applied_with_gaps'"
            ).fetchone()[0]
        ),
        "source_fallback_count": int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='source_fallback'"
            ).fetchone()[0]
        ),
    }

    cache = ArtistImageCache(root / "data" / "artist_images")
    mb_identity = ArtistIdentity.from_display_name(
        "Glass Horizon", musicbrainz_artist_id=SYNTHETIC_MBID
    )
    discogs_identity = ArtistIdentity.from_display_name(
        "Glass Horizon", discogs_artist_id=SYNTHETIC_DISCOGS_ID
    )
    low_identity = ArtistIdentity.from_display_name("Glass Horizon Legacy")
    canonical_identity = ArtistIdentity.from_display_name(
        "Glass Horizon",
        canonical_artist_id=target_id,
        musicbrainz_artist_id=SYNTHETIC_MBID,
        discogs_artist_id=SYNTHETIC_DISCOGS_ID,
        historical_aliases=("Glass Horizon Legacy",),
    )
    provider = SyntheticArtistImageProvider()
    mb_bytes = provider._portrait(mb_identity)
    discogs_bytes = provider._portrait(
        ArtistIdentity.from_display_name("Glass Horizon Discogs Portrait")
    )
    cache.store(
        ArtistImageResult(
            ArtistImageStatus.RESOLVED,
            mb_identity,
            matched_artist_name="Glass Horizon",
            musicbrainz_artist_id=SYNTHETIC_MBID,
            image_provider="MusicBrainz-linked Wikimedia",
            content_type="image/png",
            image_bytes=mb_bytes,
            portrait_kind="musicbrainz_wikimedia",
        )
    )
    cache.store(
        ArtistImageResult(
            ArtistImageStatus.RESOLVED,
            discogs_identity,
            matched_artist_name="Glass Horizon",
            discogs_artist_id=SYNTHETIC_DISCOGS_ID,
            image_provider="Discogs",
            content_type="image/png",
            image_bytes=discogs_bytes,
            portrait_kind="discogs",
        )
    )

    low = QImage(150, 150, QImage.Format.Format_ARGB32)
    low.fill(QColor("#334455"))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not low.save(buffer, "PNG"):
        raise RuntimeError("Synthetic low-resolution portrait could not be created.")
    buffer.close()
    low_bytes = bytes(payload)
    low_digest = hashlib.sha256(low_bytes).hexdigest()
    cache.files_dir.mkdir(parents=True, exist_ok=True)
    low_file = cache.files_dir / f"{low_digest}.png"
    low_file.write_bytes(low_bytes)
    low_key = cache._entry_key(low_identity)
    cache._load()["entries"][low_key] = {
        "status": "resolved",
        "requested_display_name": "Glass Horizon Legacy",
        "normalized_key": low_identity.normalized_key,
        "identity_key": low_identity.cache_identity,
        "matched_artist_name": "Glass Horizon",
        "discogs_artist_id": "10500",
        "image_provider": "Discogs legacy thumbnail",
        "cache_file": f"files/{low_file.name}",
        "content_type": "image/png",
        "fetched_at": "2026-07-18T00:00:00Z",
        "width": 150,
        "height": 150,
        "portrait_kind": "discogs",
        "pinned": False,
    }
    cache._write_manifest()
    cache.repair_index(
        {canonical_identity: (mb_identity, discogs_identity, low_identity)},
        dry_run=False,
    )
    resolved = cache.lookup(canonical_identity, repair=False)
    if (
        resolved is None
        or not resolved.resolved
        or resolved.portrait_kind != "musicbrainz_wikimedia"
        or min(int(resolved.width or 0), int(resolved.height or 0)) < 320
    ):
        raise RuntimeError("Synthetic preferred portrait was not selected.")

    runtime.cover_snapshot = {
        int(row["id"]): row["cover_path"]
        for row in db.conn.execute("SELECT id,cover_path FROM tracks ORDER BY id")
    }
    db.close()
    runtime.cache_snapshot = _file_snapshot(root / "data" / "artist_images")


def _create_window(runtime: ReviewRuntime, app) -> Any:
    window = base._create_window(runtime, app)
    base._sync_browser(window, "artists")
    loader = getattr(window, "_load_visible_artist_images", None)
    if callable(loader):
        loader()
    base._process_events(app, 8)
    return window


def _prepare_scene(runtime: ReviewRuntime, scene: ReviewScene, app):
    from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog

    window = runtime.window
    if window is None:
        raise RuntimeError("Production MusicVaultWindow is unavailable.")
    if runtime.intelligence_dialog is not None:
        runtime.intelligence_dialog.close()
        runtime.intelligence_dialog.deleteLater()
        runtime.intelligence_dialog = None

    if scene.name in {"canonical_artist_grid", "preferred_cached_portrait"}:
        base._sync_browser(window, "artists")
        window.search_box.setText("Glass Horizon")
        loader = getattr(window, "_load_visible_artist_images", None)
        if callable(loader):
            loader()
        target = window
    elif scene.name in {
        "canonical_artist_tracks",
        "artist_featured_on",
        "artist_collaborations",
        "artist_group_appearances",
    }:
        summaries = base._sync_browser(window, "artists")
        window.open_artist(base._summary_key(summaries, "Glass Horizon"))
        section = {
            "canonical_artist_tracks": "tracks",
            "artist_featured_on": "featured_on",
            "artist_collaborations": "collaborations",
            "artist_group_appearances": "group_appearances",
        }[scene.name]
        base._select_artist_section(window, section)
        target = window
    elif scene.name == "metadata_zero_review":
        dialog = MetadataIntelligenceDialog(
            window.db,
            window.metadata_intelligence_service,
            window,
        )
        all_items = dialog.filter_combo.findData(None)
        dialog.filter_combo.setCurrentIndex(all_items)
        dialog.refresh()
        runtime.intelligence_dialog = dialog
        dialog.show()
        target = dialog
    elif scene.name == "singles_uncatalogued_150":
        base._sync_browser(window, "albums")
        window.search_box.setText("Singles & Uncatalogued")
        target = window
    else:
        raise RuntimeError(f"Unknown Batch 10.5 review scene: {scene.name}")

    target.resize(scene.width, scene.height)
    if scene.scale > 1.0:
        font = target.font()
        base_size = font.pointSizeF() if font.pointSizeF() > 0 else 9.0
        font.setPointSizeF(base_size * scene.scale)
        target.setFont(font)
    target.show()
    target.raise_()
    base._process_events(app, 8)
    return target


def _validate_scene(target, runtime: ReviewRuntime, scene: ReviewScene) -> dict[str, object]:
    from music_vault.core.library_browser import (
        query_album_summaries,
        query_artist_summaries,
        query_artist_track_sections,
    )

    window = runtime.window
    texts = base._widget_texts(target)
    if window is not None:
        texts.extend(base._browser_model_texts(window))
    lower = "\n".join(texts).casefold()
    blocked = [marker for marker in base._BLOCKED_TEXT if marker in lower]
    if blocked:
        raise RuntimeError(f"Private marker present in {scene.name}.")
    missing = [term for term in scene.required_terms if term.casefold() not in lower]
    if missing:
        raise RuntimeError(f"Required production UI term missing in {scene.name}: {missing[0]}")

    summaries = query_artist_summaries(window.db.conn)
    target_summaries = [
        item for item in summaries if item.key.normalized_name == "glass horizon"
    ]
    if len(target_summaries) != 1:
        raise RuntimeError("Canonical artist cluster did not render as one card.")
    summary = target_summaries[0]
    semantic_checks = 1

    if scene.name == "canonical_artist_grid":
        normalized = [item.key.normalized_name for item in summaries]
        if len(normalized) != len(set(normalized)):
            raise RuntimeError("Artist grid contains duplicate normalized cards.")
    elif scene.name == "preferred_cached_portrait":
        item = window.artist_browser_model.item_for_key(summary.browser_key)
        identity = window.artist_image_identity(summary)
        cached = window.artist_image_cache.lookup(identity, repair=False)
        if (
            item is None
            or not item.artwork_path
            or cached is None
            or cached.portrait_kind != "musicbrainz_wikimedia"
            or min(int(cached.width or 0), int(cached.height or 0)) < 320
        ):
            raise RuntimeError("Preferred cached portrait is not visible.")
    elif scene.name in {
        "canonical_artist_tracks",
        "artist_featured_on",
        "artist_collaborations",
        "artist_group_appearances",
    }:
        expected = {
            "canonical_artist_tracks": "tracks",
            "artist_featured_on": "featured_on",
            "artist_collaborations": "collaborations",
            "artist_group_appearances": "group_appearances",
        }[scene.name]
        sections = query_artist_track_sections(window.db.conn, summary.key)
        if (
            str(window.artist_section_selector.currentData()) != expected
            or not getattr(sections, expected)
            or window.library_table.rowCount() < 1
        ):
            raise RuntimeError("Canonical artist role section did not render.")
    elif scene.name == "metadata_zero_review":
        review_count = int(
            window.db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state IN ('review','ready')"
            ).fetchone()[0]
        )
        if review_count != 0 or "pending: 0" not in runtime.intelligence_dialog.summary.text().casefold():
            raise RuntimeError("Metadata dashboard still reports ordinary review items.")
    elif scene.name == "singles_uncatalogued_150":
        virtual = [
            album
            for album in query_album_summaries(window.db.conn)
            if album.key.virtual_kind == "singles_uncatalogued"
        ]
        persisted = int(
            window.db.conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE album='Singles & Uncatalogued'"
            ).fetchone()[0]
        )
        if len(virtual) != 1 or virtual[0].track_count < 2 or persisted:
            raise RuntimeError("Virtual uncatalogued album behavior is incorrect.")

    if target.width() != scene.width or target.height() != scene.height:
        raise RuntimeError(f"Production surface size drifted in {scene.name}.")
    return {
        "text_evidence_count": len([text for text in texts if text]),
        "blocked_marker_count": 0,
        "semantic_check_count": semantic_checks,
        "production_surface": type(target).__name__,
    }


def _capture(app, runtime: ReviewRuntime, scene: ReviewScene, output: Path) -> dict[str, object]:
    target = _prepare_scene(runtime, scene, app)
    validation = _validate_scene(target, runtime, scene)
    pixmap = target.grab()
    if pixmap.width() != scene.width or pixmap.height() != scene.height:
        raise RuntimeError(f"Review capture dimensions drifted in {scene.name}.")
    destination = output / f"{scene.name}.png"
    if not pixmap.save(str(destination), "PNG") or destination.stat().st_size < 4_000:
        raise RuntimeError(f"Could not capture review scene: {scene.name}")
    result = {
        "scene": scene.name,
        "purpose": scene.purpose,
        "width": scene.width,
        "height": scene.height,
        "scale_percent": int(scene.scale * 100),
        "capture_size": destination.stat().st_size,
        "capture_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        **validation,
    }
    if runtime.intelligence_dialog is target:
        target.close()
        target.deleteLater()
        runtime.intelligence_dialog = None
        base._process_events(app, 2)
    return result


def run_review(app, output: Path, runtime: ReviewRuntime) -> dict[str, object]:
    _seed_batch10_5(runtime)
    _create_window(runtime, app)
    try:
        captures = [_capture(app, runtime, scene, output) for scene in SCENES]
        db = runtime.window.db
        backwards = db.get_track(runtime.track_ids["backwards"])
        current_covers = {
            int(row["id"]): row["cover_path"]
            for row in db.conn.execute("SELECT id,cover_path FROM tracks ORDER BY id")
        }
        if runtime.provider_request_count:
            raise RuntimeError("Production UI review invoked an artist-image provider.")
        if runtime.seed_evidence.get("remaining_review_count") != 0:
            raise RuntimeError("Synthetic metadata repair left ordinary review items.")
        if tuple(backwards[name] for name in ("title", "artist", "album")) != (
            "Song Name",
            "Band Name",
            "Catalogue Record",
        ):
            raise RuntimeError("Stored backwards-title evidence was not repaired.")
        if current_covers != runtime.cover_snapshot:
            raise RuntimeError("Synthetic track cover paths changed during review.")
        if _file_snapshot(runtime.root / "data" / "artist_images") != runtime.cache_snapshot:
            raise RuntimeError("Cached portrait files or index changed during review.")
        return {
            "schema_version": 1,
            "review": "Music Vault Batch 10.5 metadata acceptance",
            "status": "complete",
            "synthetic_only": True,
            "temporary_synthetic_database": True,
            "production_window_used": True,
            "production_metadata_dialog_used": True,
            "capture_count": len(captures),
            "scenes": [scene.name for scene in SCENES],
            "resolutions": sorted({f"{scene.width}x{scene.height}" for scene in SCENES}),
            "scale_states_percent": sorted({int(scene.scale * 100) for scene in SCENES}),
            "captures": captures,
            "canonical_cluster_count": 1,
            "review_queue_count": 0,
            "backwards_orientation_repaired": True,
            "preferred_cached_portrait_preserved": True,
            "cover_paths_unchanged": True,
            "network_attempt_count": 0,
            "credential_read_count": 0,
            "runtime_database_read_count": 0,
            "media_file_write_count": 0,
            "provider_request_count": 0,
            "personal_data_used": False,
        }
    finally:
        base._close_runtime_ui(runtime, app)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"
    network_attempts = base._install_network_guard()
    output: Path | None = None
    output_token = ""
    runtime: ReviewRuntime | None = None
    complete = False
    try:
        output, output_token = _output_directory(args.output)
        runtime = _runtime_directory()
        with _review_environment(runtime.root):
            private_attempts = base._install_private_file_guard(runtime.root)
            from PySide6.QtWidgets import QApplication
            from music_vault.ui.theme import application_stylesheet

            app = QApplication.instance() or QApplication([])
            if args.offscreen:
                base._install_offscreen_review_font(app)
            app.setStyleSheet(application_stylesheet())
            payload = run_review(app, output, runtime)
            if network_attempts:
                raise RuntimeError("The Batch 10.5 review attempted network access.")
            if private_attempts:
                raise RuntimeError("The Batch 10.5 review attempted private file access.")
            payload["captures_retained"] = bool(args.keep_captures)
            print(json.dumps(payload, indent=2, sort_keys=True))
        complete = True
        return 0
    except Exception as exc:
        print(f"Batch 10.5 UI review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None and runtime.root.exists():
            shutil.rmtree(_owned_runtime(runtime))
        if output is not None and output_token and (complete or output.exists()):
            owned = _owned_output(output, output_token)
            if not args.keep_captures:
                shutil.rmtree(owned)


if __name__ == "__main__":
    raise SystemExit(main())
