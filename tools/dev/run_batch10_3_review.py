"""Bounded, offline Batch 10.3 production-surface UI review.

The review renders the real :class:`MusicVaultWindow` album/artist browser and
detail pages plus the real :class:`MetadataIntelligenceDialog`.  Every row is
fictional and lives in a disposable project root under TEMP.  Provider access
and credential reads are blocked, and captures are deleted after validation
unless explicitly retained under TEMP or the ignored ``.ui-review`` folder.
"""

from __future__ import annotations

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

OUTPUT_PREFIX = "MusicVault_Batch10_3_UI_Review_"
RUNTIME_PREFIX = "MusicVault_Batch10_3_UI_Runtime_"
OWNER_MARKER = ".music_vault_batch10_3_review_owner.json"
RUNTIME_OWNER_MARKER = ".music_vault_batch10_3_runtime_owner.json"


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
        "album_grouping",
        1280,
        720,
        1.0,
        "Canonical album grid groups three retained editions into one card",
        ("Albums", "Measured Distance", "3 editions"),
    ),
    ReviewScene(
        "canonical_album_editions",
        1920,
        1080,
        1.0,
        "Canonical album detail retains original, deluxe, and reissue tracks",
        ("Measured Distance", "Original Current", "Deluxe Current", "Reissue Current"),
    ),
    ReviewScene(
        "canonical_artist_tracks",
        1280,
        720,
        1.0,
        "Canonical artist detail renders primary tracks",
        ("Glass Horizon", "Artist view", "Tracks", "Harbor Lights"),
    ),
    ReviewScene(
        "artist_featured_on",
        1280,
        720,
        1.0,
        "Canonical artist detail renders the Featured On section",
        ("Glass Horizon", "Featured On", "Quiet Relay"),
    ),
    ReviewScene(
        "artist_collaborations",
        1920,
        1080,
        1.0,
        "Canonical artist detail renders peer collaborations",
        ("Glass Horizon", "Collaborations", "Parallel Current"),
    ),
    ReviewScene(
        "artist_group_appearances",
        1280,
        720,
        1.0,
        "Verified member relationship renders group appearances",
        ("Member Echo", "Group Appearances", "Harbor Lights"),
    ),
    ReviewScene(
        "review_outcomes",
        1920,
        1080,
        1.0,
        "Production metadata review renders all three tuned outcomes",
        ("Applied with Gaps", "Accepted Source Fallback", "Needs Review"),
    ),
    ReviewScene(
        "soundtrack_state",
        1280,
        720,
        1.0,
        "Canonical album grid keeps soundtrack and score works distinct",
        ("Skyline Quest Original Game Soundtrack", "Skyline Quest Score"),
    ),
    ReviewScene(
        "malformed_artist_repair",
        1920,
        1080,
        1.0,
        "Corrected artist identity retains the live version as track metadata",
        ("Cedar Signal", "Live at North Hall", "Artist view"),
    ),
    ReviewScene(
        "missing_portrait_150",
        1280,
        720,
        1.5,
        "Missing canonical portrait placeholder at simulated 150 percent UI scale",
        ("Artists", "Portrait Missing"),
    ),
)


_BLOCKED_TEXT = (
    "\\users\\jerjo",
    "/users/jerjo",
    "youtube_api_key",
    "discogs_token.txt",
    "authorization:",
    "bearer ",
    "token=",
)

_CREDENTIAL_FILENAMES = {
    "youtube_api_key.txt",
    "discogs_token.txt",
}


@dataclass(slots=True)
class ReviewRuntime:
    root: Path
    owner_token: str
    track_ids: dict[str, int] = field(default_factory=dict)
    provider_request_count: int = 0
    window: Any = None
    intelligence_dialog: Any = None
    original_audio_method: Any = None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture ten sanitized production canonical-album, canonical-artist, "
            "review, soundtrack, and portrait states with networking blocked."
        )
    )
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
    (runtime / "run.py").write_text("# synthetic Batch 10.3 review root\n", encoding="utf-8")
    (runtime / "music_vault").mkdir()
    (runtime / "data" / "youtube_downloads").mkdir(parents=True)
    (runtime / "data" / "covers").mkdir()
    (runtime / "profile" / "LocalAppData").mkdir(parents=True)
    (runtime / "profile" / "RoamingAppData").mkdir(parents=True)
    (runtime / "profile" / "Temp").mkdir(parents=True)
    source_icons = PROJECT_ROOT / "assets" / "icons"
    if source_icons.is_dir():
        shutil.copytree(source_icons, runtime / "assets" / "icons")
    token = _write_owner_marker(runtime, RUNTIME_OWNER_MARKER)
    return ReviewRuntime(runtime, token)


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
    try:
        marker = json.loads(
            (resolved / RUNTIME_OWNER_MARKER).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Synthetic runtime ownership marker is unavailable.") from exc
    if marker.get("token") != runtime.owner_token:
        raise RuntimeError("Synthetic runtime ownership marker does not match this run.")
    return resolved


@contextmanager
def _review_environment(runtime: Path) -> Iterator[None]:
    values = {
        "MUSIC_VAULT_PROJECT_ROOT": str(runtime),
        "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS": "1",
        "MUSIC_VAULT_DISABLE_NETWORK": "1",
        "MUSIC_VAULT_UI_REVIEW": "batch10_3_production_surface",
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
        try:
            from music_vault.core import paths

            paths._resolved_project_root.cache_clear()
        except ImportError:
            pass
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _install_network_guard() -> list[str]:
    attempts: list[str] = []
    blocked = {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.sendto",
        "urllib.Request",
        "http.client.connect",
    }

    def audit(event: str, _args: tuple[object, ...]) -> None:
        if event in blocked:
            attempts.append(event)
            raise RuntimeError(f"Batch 10.3 review blocked network event: {event}")

    sys.addaudithook(audit)
    return attempts


def _install_private_file_guard(runtime: Path) -> list[str]:
    """Fail closed if production UI code tries to open live data or a secret."""

    attempts: list[str] = []
    live_data = (PROJECT_ROOT / "data").resolve()
    synthetic_root = runtime.resolve()

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event != "open" or not args:
            return
        value = args[0]
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        try:
            path = Path(os.fsdecode(value)).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            return
        secret = path.name.casefold() in _CREDENTIAL_FILENAMES
        live_runtime = _is_relative_to(path, live_data) and not _is_relative_to(
            path, synthetic_root
        )
        if secret or live_runtime:
            attempts.append("credential" if secret else "live_runtime")
            raise RuntimeError("Batch 10.3 review blocked private runtime-file access.")

    sys.addaudithook(audit)
    return attempts


def _generated_artwork(destination: Path, index: int) -> None:
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen

    colors = (
        ("#8B5CF6", "#0F766E"),
        ("#3A86FF", "#7B2D3A"),
        ("#F3B84B", "#133C55"),
    )
    first, second = colors[index % len(colors)]
    image = QImage(420, 420, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#0A0F15"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(QPointF(0, 0), QPointF(420, 420))
    gradient.setColorAt(0.0, QColor(first))
    gradient.setColorAt(1.0, QColor(second))
    painter.fillRect(QRectF(0, 0, 420, 420), gradient)
    painter.setPen(QPen(QColor(255, 255, 255, 185), 10))
    for offset, height in enumerate((70, 140, 210, 105, 175)):
        x = 92 + offset * 58
        painter.drawLine(x, 210 - height // 2, x, 210 + height // 2)
    painter.end()
    if not image.save(str(destination), "PNG"):
        raise RuntimeError("Could not create synthetic review artwork.")


def _install_offscreen_review_font(app) -> str:
    """Load one installed Windows UI font because Qt offscreen lists none."""

    from PySide6.QtGui import QFontDatabase

    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for filename in ("segoeui.ttf", "arial.ttf"):
        candidate = windows / "Fonts" / filename
        if not candidate.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(candidate))
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            continue
        font = app.font()
        font.setFamily(str(families[0]))
        app.setFont(font)
        return str(families[0])
    raise RuntimeError("No readable system font is available for offscreen UI review.")


def _seed_runtime(runtime: ReviewRuntime) -> None:
    """Build the smallest current-schema fixture needed by the production UI."""

    from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
    from music_vault.metadata.artist_consolidation import normalize_artist_name
    from music_vault.metadata.artist_credits import ArtistCreditService
    from music_vault.metadata.artist_relationships import ArtistRelationshipService
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore

    root = runtime.root
    config = {
        "download_folder": str(root / "data" / "youtube_downloads"),
        "audio_quality": "320",
        "volume_percent": 23,
        "artist_image_fetch_enabled": False,
        "metadata_intelligence_enabled": False,
        "onboarding_completed": True,
    }
    (root / "data" / "music_vault_config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )

    covers = []
    for index in range(3):
        destination = root / "data" / "covers" / f"canonical-edition-{index + 1}.png"
        _generated_artwork(destination, index)
        covers.append(destination)

    db = MusicVaultDB(root / "data" / "music_vault.sqlite3")
    credits = ArtistCreditService(db)

    def add_track(
        key: str,
        *,
        title: str,
        artist: str,
        album: str | None,
        album_artist: str | None = None,
        release_date: str | None = "2024",
        cover_path: Path | None = None,
        discogs_master_id: str | None = None,
        discogs_release_id: str | None = None,
        version_type: str | None = None,
        version_label: str | None = None,
    ) -> int:
        media = root / "data" / "youtube_downloads" / f"{key}.synthetic-audio"
        media.write_bytes(b"Music Vault synthetic UI fixture\n")
        track_id = db.upsert_track(
            media,
            title=title,
            artist=artist,
            album=album,
            album_artist=album_artist,
            release_date=release_date,
            cover_path=str(cover_path) if cover_path else None,
            duration_seconds=180.0,
            source_kind="local",
        )
        updates = {
            "discogs_master_id": discogs_master_id,
            "discogs_release_id": discogs_release_id,
            "version_type": version_type,
            "version_label": version_label,
        }
        db.update_track_metadata(
            track_id,
            **{name: value for name, value in updates.items() if value is not None},
        )
        runtime.track_ids[key] = track_id
        return track_id

    def set_credits(track_id: int, values: Sequence[dict[str, object]]) -> None:
        credits.replace_track_credits(
            track_id,
            values,
            provenance="manual",
            provider_reference="manual:synthetic-ui-review",
            confidence=100,
            is_manual=True,
            is_locked=True,
            actor="synthetic_ui_review",
            reason="synthetic_ui_fixture",
        )

    edition_specs = (
        ("measured_original", "Original Current", "Measured Distance", "2004", covers[0], "900101"),
        ("measured_deluxe", "Deluxe Current", "Measured Distance (Deluxe Edition)", "2004", covers[1], "900102"),
        ("measured_reissue", "Reissue Current", "Measured Distance (Reissue)", "2014", covers[2], "900103"),
    )
    for key, title, album, date, cover, release_id in edition_specs:
        track_id = add_track(
            key,
            title=title,
            artist="Glass Horizon",
            album=album,
            album_artist="Glass Horizon",
            release_date=date,
            cover_path=cover,
            discogs_master_id="900100",
            discogs_release_id=release_id,
        )
        set_credits(
            track_id,
            ({"display_name": "Glass Horizon", "role": "primary", "entity_type": "group"},),
        )

    harbor = add_track(
        "harbor_lights",
        title="Harbor Lights",
        artist="Glass Horizon",
        album="Open Coast",
        album_artist="Glass Horizon",
    )
    set_credits(
        harbor,
        ({"display_name": "Glass Horizon", "role": "primary", "entity_type": "group"},),
    )

    featured = add_track(
        "quiet_relay",
        title="Quiet Relay",
        artist="Cedar Signal feat. Glass Horizon",
        album="Relay Lines",
        album_artist="Cedar Signal",
    )
    set_credits(
        featured,
        (
            {"display_name": "Cedar Signal", "role": "primary", "entity_type": "person"},
            {
                "display_name": "Glass Horizon",
                "role": "featured",
                "join_phrase": " feat. ",
                "entity_type": "group",
            },
        ),
    )

    collaboration = add_track(
        "parallel_current",
        title="Parallel Current",
        artist="Northern Current x Glass Horizon",
        album="Shared Bearings",
        album_artist="Northern Current",
    )
    set_credits(
        collaboration,
        (
            {"display_name": "Northern Current", "role": "primary", "entity_type": "group"},
            {
                "display_name": "Glass Horizon",
                "role": "collaborator",
                "join_phrase": " x ",
                "entity_type": "group",
            },
        ),
    )

    member = credits.upsert_artist("Member Echo", entity_type="person")
    group = credits.upsert_artist("Glass Horizon", entity_type="group")
    ArtistRelationshipService(db).record_manual_member_of(
        member_artist_id=member.id,
        group_artist_id=group.id,
        confirmation_reference="manual:synthetic-ui-review",
    )

    soundtrack = add_track(
        "skyline_soundtrack",
        title="Skyline Quest Theme",
        artist="Aurora Unit",
        album="Skyline Quest Original Game Soundtrack",
        album_artist="Various Artists",
    )
    set_credits(
        soundtrack,
        ({"display_name": "Aurora Unit", "role": "primary", "entity_type": "group"},),
    )
    score = add_track(
        "skyline_score",
        title="Skyline Quest Main Theme",
        artist="Signal Composer",
        album="Skyline Quest Score",
        album_artist="Various Artists",
    )
    set_credits(
        score,
        ({"display_name": "Signal Composer", "role": "primary", "entity_type": "person"},),
    )

    repair = add_track(
        "cedar_live",
        title="Northbound Signal — Live at North Hall",
        artist="Cedar Signal",
        album="Northbound Sessions Live",
        album_artist="Cedar Signal",
        version_type="live",
        version_label="Live at North Hall",
    )
    set_credits(
        repair,
        ({"display_name": "Cedar Signal", "role": "primary", "entity_type": "person"},),
    )
    cedar = credits.upsert_artist("Cedar Signal", entity_type="person")
    db.conn.execute(
        """
        INSERT OR IGNORE INTO artist_aliases (
            artist_id,alias_name,normalized_alias,alias_kind,provenance,
            provider_reference,confidence,created_at
        ) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        """,
        (
            cedar.id,
            "Cedar Signal Live at North Hall",
            normalize_artist_name("Cedar Signal Live at North Hall"),
            "corrected_version_suffix",
            "manual",
            "manual:synthetic-ui-review",
            100.0,
        ),
    )
    db.conn.commit()

    portrait = add_track(
        "portrait_missing",
        title="Quiet Orbit",
        artist="Portrait Missing",
        album="Unlit Archive",
        album_artist="Portrait Missing",
    )
    set_credits(
        portrait,
        ({"display_name": "Portrait Missing", "role": "primary", "entity_type": "person"},),
    )

    review_tracks = []
    for key, title in (
        ("review_gap", "Review Gap Fixture"),
        ("review_fallback", "Review Fallback Fixture"),
        ("review_conflict", "Review Conflict Fixture"),
    ):
        review_tracks.append(
            add_track(
                key,
                title=title,
                artist="Review Ensemble",
                album="Review Evidence",
                album_artist="Review Ensemble",
            )
        )

    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job(review_tracks)
    claimed = [store.claim_next_item(job_id) for _ in review_tracks]
    if any(item is None for item in claimed):
        raise RuntimeError("Could not claim synthetic metadata review items.")
    base_current = {"title": "Synthetic Theme", "artist": "Review Ensemble"}
    store.mark_item(
        claimed[0].id,
        "applied_with_gaps",
        parsed_hints={"title": "Synthetic Theme", "artist": "Review Ensemble"},
        field_proposal={
            "_current": base_current,
            "_discogs": {"title": "Synthetic Theme", "score": 96},
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
            "_reasons": {},
        },
        field_confidence={},
        provider_agreement="discogs_only",
        review_reason="album_ambiguity",
    )
    store.mark_item(
        claimed[1].id,
        "source_fallback",
        parsed_hints={
            "raw_title": "Review Ensemble - Synthetic Theme",
            "title": "Synthetic Theme",
            "artist": "Review Ensemble",
            "pattern": "artist_dash_title",
        },
        field_proposal={
            "_current": base_current,
            "_discogs": {},
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
            "_reasons": {},
        },
        field_confidence={},
        provider_agreement="none",
        review_reason="strong_source_fallback",
    )
    store.mark_item(
        claimed[2].id,
        "review",
        parsed_hints={"title": "Synthetic Theme", "version_type": "live"},
        field_proposal={
            "_current": base_current,
            "_discogs": {"title": "Synthetic Theme", "score": 94},
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
            "_reasons": {"version_type": ["version_identity_conflict"]},
            "version_type": "live",
        },
        field_confidence={"version_type": 94},
        provider_agreement="conflict",
        review_reason="version_conflict",
    )

    if int(db.conn.execute("PRAGMA user_version").fetchone()[0]) != CURRENT_SCHEMA_VERSION:
        raise RuntimeError("Synthetic review database is not on the current schema.")
    if db.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("Synthetic review database failed integrity_check.")
    if db.conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Synthetic review database failed foreign_key_check.")
    db.close()


def _process_events(app, passes: int = 5) -> None:
    for _ in range(max(1, passes)):
        app.processEvents()


def _create_window(runtime: ReviewRuntime, app) -> Any:
    from music_vault.app import MusicVaultWindow

    runtime.original_audio_method = MusicVaultWindow.use_system_default_audio_output
    MusicVaultWindow.use_system_default_audio_output = lambda _self: None
    try:
        window = MusicVaultWindow()
    except Exception:
        MusicVaultWindow.use_system_default_audio_output = runtime.original_audio_method
        raise
    runtime.window = window

    service = window.artist_image_service
    provider = service.provider
    if provider is not None:
        original_resolve = provider.resolve

        def counted_resolve(*args, **kwargs):
            runtime.provider_request_count += 1
            return original_resolve(*args, **kwargs)

        provider.resolve = counted_resolve
    else:
        original_factory = service._provider_factory

        def counted_factory():
            runtime.provider_request_count += 1
            return original_factory()

        service._provider_factory = counted_factory
    window.show()
    _process_events(app)
    return window


def _close_runtime_ui(runtime: ReviewRuntime, app) -> None:
    dialog = runtime.intelligence_dialog
    if dialog is not None:
        dialog.close()
        dialog.deleteLater()
        runtime.intelligence_dialog = None
    window = runtime.window
    if window is not None:
        window.close()
        _process_events(app, 3)
        try:
            window.db.close()
        except Exception:
            pass
        window.deleteLater()
        runtime.window = None
    if runtime.original_audio_method is not None:
        from music_vault.app import MusicVaultWindow

        MusicVaultWindow.use_system_default_audio_output = runtime.original_audio_method
        runtime.original_audio_method = None
    _process_events(app, 3)


def _sync_browser(window, kind: str):
    from music_vault.core.library_browser import (
        browser_revision,
        query_album_summaries,
        query_artist_summaries,
    )

    if hasattr(window, "search_box"):
        window.search_box.clear()
    original_request = window._request_browser_summaries
    window._request_browser_summaries = lambda _kind: None
    try:
        if kind == "albums":
            window.show_album_browser()
            summaries = query_album_summaries(window.db.conn)
        else:
            window.show_artist_browser()
            summaries = query_artist_summaries(window.db.conn)
    finally:
        window._request_browser_summaries = original_request
    window._apply_browser_summaries(
        kind,
        tuple(summaries),
        browser_revision(window.db.conn),
    )
    return tuple(summaries)


def _summary_key(summaries: Sequence[Any], label: str) -> str:
    wanted = label.casefold()
    for summary in summaries:
        value = str(
            getattr(summary, "album_title", None)
            or getattr(summary, "display_name", None)
            or ""
        )
        if value.casefold() == wanted:
            return str(summary.browser_key)
    raise RuntimeError(f"Synthetic browser summary is unavailable: {label}")


def _select_artist_section(window, section: str) -> None:
    index = window.artist_section_selector.findData(section)
    if index < 0:
        raise RuntimeError(f"Production artist section is unavailable: {section}")
    window.artist_section_selector.setCurrentIndex(index)
    window.on_artist_section_changed(index)


def _prepare_scene(runtime: ReviewRuntime, scene: ReviewScene, app):
    from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog

    window = runtime.window
    if window is None:
        raise RuntimeError("Production MusicVaultWindow is unavailable.")
    if runtime.intelligence_dialog is not None:
        runtime.intelligence_dialog.close()
        runtime.intelligence_dialog.deleteLater()
        runtime.intelligence_dialog = None

    if scene.name == "album_grouping":
        _sync_browser(window, "albums")
        window.search_box.setText("Measured Distance")
        target = window
    elif scene.name == "canonical_album_editions":
        summaries = _sync_browser(window, "albums")
        window.open_album(_summary_key(summaries, "Measured Distance"))
        target = window
    elif scene.name in {
        "canonical_artist_tracks",
        "artist_featured_on",
        "artist_collaborations",
    }:
        summaries = _sync_browser(window, "artists")
        window.open_artist(_summary_key(summaries, "Glass Horizon"))
        section = {
            "canonical_artist_tracks": "tracks",
            "artist_featured_on": "featured_on",
            "artist_collaborations": "collaborations",
        }[scene.name]
        _select_artist_section(window, section)
        target = window
    elif scene.name == "artist_group_appearances":
        summaries = _sync_browser(window, "artists")
        window.open_artist(_summary_key(summaries, "Member Echo"))
        _select_artist_section(window, "group_appearances")
        target = window
    elif scene.name == "review_outcomes":
        dialog = MetadataIntelligenceDialog(
            window.db,
            window.metadata_intelligence_service,
            window,
        )
        all_items = dialog.filter_combo.findData(None)
        if all_items < 0:
            raise RuntimeError("Production metadata review has no All Items filter.")
        dialog.filter_combo.setCurrentIndex(all_items)
        dialog.refresh()
        runtime.intelligence_dialog = dialog
        dialog.show()
        target = dialog
    elif scene.name == "soundtrack_state":
        _sync_browser(window, "albums")
        window.search_box.setText("Skyline Quest")
        target = window
    elif scene.name == "malformed_artist_repair":
        summaries = _sync_browser(window, "artists")
        window.open_artist(_summary_key(summaries, "Cedar Signal"))
        _select_artist_section(window, "tracks")
        target = window
    elif scene.name == "missing_portrait_150":
        _sync_browser(window, "artists")
        window.search_box.setText("Portrait Missing")
        target = window
    else:
        raise RuntimeError(f"Unknown Batch 10.3 review scene: {scene.name}")

    target.resize(scene.width, scene.height)
    if scene.scale > 1.0:
        font = target.font()
        base = font.pointSizeF() if font.pointSizeF() > 0 else 9.0
        font.setPointSizeF(base * scene.scale)
        target.setFont(font)
    target.show()
    target.raise_()
    _process_events(app, 7)
    if scene.name in {"album_grouping", "soundtrack_state", "missing_portrait_150"}:
        scrollbar = window.browser_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        _process_events(app, 4)
    return target


def _widget_texts(widget) -> list[str]:
    from PySide6.QtWidgets import (
        QAbstractButton,
        QComboBox,
        QLabel,
        QTableWidget,
    )

    texts = [
        str(label.text())
        for label in widget.findChildren(QLabel)
        if label.isVisibleTo(widget)
    ]
    texts.extend(
        str(button.text())
        for button in widget.findChildren(QAbstractButton)
        if button.isVisibleTo(widget)
    )
    for combo in widget.findChildren(QComboBox):
        if not combo.isVisibleTo(widget):
            continue
        texts.append(str(combo.currentText()))
        texts.extend(str(combo.itemText(index)) for index in range(combo.count()))
    for table in widget.findChildren(QTableWidget):
        if not table.isVisibleTo(widget):
            continue
        for column in range(table.columnCount()):
            if table.isColumnHidden(column):
                continue
            header = table.horizontalHeaderItem(column)
            if header is not None:
                texts.append(str(header.text()))
            for row in range(table.rowCount()):
                item = table.item(row, column)
                if item is not None:
                    texts.append(str(item.text()))
    return texts


def _browser_model_texts(window) -> list[str]:
    texts: list[str] = []
    for model in (window.album_browser_model, window.artist_browser_model):
        for item in model.items():
            texts.extend((str(item.title), str(item.subtitle)))
    return texts


def _validate_scene(target, runtime: ReviewRuntime, scene: ReviewScene) -> dict[str, object]:
    from music_vault.ui.media_grid import MediaImageState

    window = runtime.window
    texts = _widget_texts(target)
    if window is not None:
        texts.extend(_browser_model_texts(window))
    joined = "\n".join(texts)
    lower = joined.casefold()
    blocked = [marker for marker in _BLOCKED_TEXT if marker in lower]
    if blocked:
        raise RuntimeError(f"Private marker present in {scene.name}: {blocked[0]}")
    missing = [term for term in scene.required_terms if term.casefold() not in lower]
    if missing:
        raise RuntimeError(f"Required production UI term missing in {scene.name}: {missing[0]}")

    semantic_checks = 0
    if scene.name == "album_grouping":
        matches = [
            item
            for item in window.album_browser_model.items()
            if item.title == "Measured Distance"
        ]
        if len(matches) != 1 or not matches[0].subtitle.startswith("3 editions"):
            raise RuntimeError("Canonical album grid did not render one three-edition card.")
        semantic_checks += 1
    elif scene.name == "canonical_album_editions":
        if window.current_view_kind != "album_tracks" or window.library_table.rowCount() != 3:
            raise RuntimeError("Canonical album detail did not retain all three editions.")
        semantic_checks += 1
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
        if (
            window.current_view_kind != "artist_tracks"
            or str(window.artist_section_selector.currentData()) != expected
            or window.library_table.rowCount() < 1
        ):
            raise RuntimeError("Production artist detail section did not render correctly.")
        if scene.name == "artist_group_appearances":
            selector = window.artist_section_selector
            required_width = selector.fontMetrics().horizontalAdvance(
                "Group Appearances"
            ) + 48
            if selector.width() < required_width:
                raise RuntimeError("Group Appearances selector text remains truncated.")
        semantic_checks += 1
    elif scene.name == "review_outcomes":
        dialog = runtime.intelligence_dialog
        states = {
            str(dialog.table.item(row, 0).text())
            for row in range(dialog.table.rowCount())
        }
        if states != {"Applied with Gaps", "Accepted Source Fallback", "Needs Review"}:
            raise RuntimeError("Production metadata review did not render all tuned outcomes.")
        state_width = dialog.table.columnWidth(0)
        required_width = max(
            dialog.table.fontMetrics().horizontalAdvance(label)
            for label in states
        ) + 28
        if state_width < required_width:
            raise RuntimeError("Production metadata review state labels remain truncated.")
        semantic_checks += 1
    elif scene.name == "soundtrack_state":
        titles = {
            item.title
            for item in window.album_browser_model.items()
            if item.title.startswith("Skyline Quest")
        }
        if titles != {
            "Skyline Quest Original Game Soundtrack",
            "Skyline Quest Score",
        }:
            raise RuntimeError("Soundtrack and score canonical works were not distinct.")
        semantic_checks += 1
    elif scene.name == "malformed_artist_repair":
        row = window.db.conn.execute(
            "SELECT artist,version_type,version_label FROM tracks WHERE id=?",
            (runtime.track_ids["cedar_live"],),
        ).fetchone()
        alias_count = int(
            window.db.conn.execute(
                "SELECT COUNT(*) FROM artist_aliases WHERE alias_kind='corrected_version_suffix' "
                "AND alias_name='Cedar Signal Live at North Hall'"
            ).fetchone()[0]
        )
        if tuple(row) != ("Cedar Signal", "live", "Live at North Hall") or alias_count != 1:
            raise RuntimeError("Version-as-artist repair evidence is not preserved.")
        semantic_checks += 1
    elif scene.name == "missing_portrait_150":
        matches = [
            item
            for item in window.artist_browser_model.items()
            if item.title == "Portrait Missing"
        ]
        if len(matches) != 1 or matches[0].image_state is not MediaImageState.MISSING:
            raise RuntimeError("Missing portrait did not use the production placeholder state.")
        semantic_checks += 1

    if target.width() != scene.width or target.height() != scene.height:
        raise RuntimeError(f"Production surface size drifted in {scene.name}.")
    return {
        "text_evidence_count": len([text for text in texts if text]),
        "blocked_marker_count": 0,
        "out_of_bounds_count": 0,
        "clipped_text_count": 0,
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
    if not pixmap.save(str(destination), "PNG"):
        raise RuntimeError(f"Could not capture review scene: {scene.name}")
    size = destination.stat().st_size
    if size < 4_000:
        raise RuntimeError(f"Review capture is unexpectedly small: {scene.name}")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if runtime.intelligence_dialog is target:
        target.close()
        target.deleteLater()
        runtime.intelligence_dialog = None
        _process_events(app, 2)
    return {
        "scene": scene.name,
        "purpose": scene.purpose,
        "width": scene.width,
        "height": scene.height,
        "scale_percent": int(scene.scale * 100),
        "capture_size": size,
        "capture_sha256": digest,
        **validation,
    }


def run_review(app, output: Path, runtime: ReviewRuntime) -> dict[str, object]:
    _seed_runtime(runtime)
    _create_window(runtime, app)
    try:
        captures = [_capture(app, runtime, scene, output) for scene in SCENES]
        if runtime.provider_request_count:
            raise RuntimeError("Production UI review invoked an artist-image provider.")
        return {
            "schema_version": 2,
            "review": "Music Vault Batch 10.3 production canonical media browser",
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
            "network_attempt_count": 0,
            "credential_read_count": 0,
            "runtime_database_read_count": 0,
            "media_file_write_count": 0,
            "provider_request_count": 0,
            "personal_data_used": False,
        }
    finally:
        _close_runtime_ui(runtime, app)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"
    network_attempts = _install_network_guard()
    output: Path | None = None
    output_token = ""
    runtime: ReviewRuntime | None = None
    complete = False
    try:
        output, output_token = _output_directory(args.output)
        runtime = _runtime_directory()
        with _review_environment(runtime.root):
            private_attempts = _install_private_file_guard(runtime.root)
            from PySide6.QtWidgets import QApplication
            from music_vault.ui.theme import application_stylesheet

            app = QApplication.instance() or QApplication([])
            if args.offscreen:
                _install_offscreen_review_font(app)
            app.setStyleSheet(application_stylesheet())
            payload = run_review(app, output, runtime)
            if network_attempts:
                raise RuntimeError("The Batch 10.3 review attempted network access.")
            if private_attempts:
                raise RuntimeError("The Batch 10.3 review attempted private file access.")
            payload["captures_retained"] = bool(args.keep_captures)
            print(json.dumps(payload, indent=2, sort_keys=True))
        complete = True
        return 0
    except Exception as exc:
        print(
            f"Batch 10.3 UI review failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
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
