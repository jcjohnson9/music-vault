from __future__ import annotations

import ast
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "music_vault"
REQUIRED_FILES = (
    "run.py",
    "music_vault/version.py",
    "music_vault/app.py",
    "music_vault/core/db.py",
    "music_vault/core/importer.py",
    "music_vault/core/youtube_sync.py",
    "music_vault/core/app_status.py",
    "music_vault/core/watchtower_status.py",
    "music_vault/core/paths.py",
    "music_vault/core/ffmpeg.py",
    "music_vault/core/desktop_shortcut.py",
    "music_vault/core/library_browser.py",
    "music_vault/core/audio_analysis.py",
    "music_vault/metadata/schema.py",
    "music_vault/metadata/service.py",
    "music_vault/metadata/artwork.py",
    "music_vault/metadata/musicbrainz_enricher.py",
    "music_vault/metadata/remediation_schema.py",
    "music_vault/metadata/matching.py",
    "music_vault/metadata/remediation.py",
    "music_vault/metadata/tag_writer.py",
    "music_vault/metadata/artist_images.py",
    "music_vault/metadata/artist_credits.py",
    "music_vault/metadata/intelligence_schema.py",
    "music_vault/metadata/intelligence_settings.py",
    "music_vault/metadata/intelligence.py",
    "music_vault/metadata/title_parser.py",
    "music_vault/metadata/uploader_classifier.py",
    "music_vault/metadata/ensemble.py",
    "music_vault/metadata/providers/__init__.py",
    "music_vault/metadata/providers/discogs.py",
    "music_vault/metadata/discogs_artwork.py",
    "music_vault/ui/browser_loader.py",
    "music_vault/ui/media_grid.py",
    "music_vault/ui/metadata_editor.py",
    "music_vault/ui/metadata_tasks.py",
    "music_vault/ui/metadata_remediation.py",
    "music_vault/ui/metadata_intelligence.py",
    "music_vault/ui/artist_credit_editor.py",
    "music_vault/ui/thumbnail_cache.py",
    "music_vault/ui/theme.py",
    "music_vault/ui/icons.py",
    "music_vault/ui/components.py",
    "music_vault/ui/review.py",
    "music_vault/ui/onboarding.py",
    "music_vault/ui/party_mode.py",
    "music_vault/ui/party_palette.py",
    "music_vault/ui/party_visuals.py",
    "assets/icons/music_vault.ico",
    "assets/icons/ui/README.md",
    "assets/icons/ui/artist-unknown.svg",
    "assets/icons/ui/party-mode.svg",
    "assets/icons/ui/exit-fullscreen.svg",
    "assets/icons/ui/visual-preset.svg",
    "assets/icons/ui/overlay-help.svg",
    "tools/dev/run_party_mode_review.py",
    "tools/dev/run_party_mode_review.ps1",
    "tools/dev/remediate_library_metadata.py",
    "tools/dev/remediate_library_metadata.ps1",
    "requirements-release.txt",
    "tools/release/build_portable_release.py",
    "tools/release/fetch_compliance_sources.py",
    "tools/release/generate_qt_attributions.py",
    "tools/release/verify_portable_release.py",
    "tools/release/third_party_licenses.json",
    "THIRD_PARTY_NOTICES.md",
    "licenses/MPL-2.0.txt",
    "licenses/OPENSSL-APACHE-2.0.txt",
    "licenses/qt-attrib/INDEX.md",
    "licenses/qt-attrib/SOURCE_ARCHIVES.json",
    "docs/BINARY_DISTRIBUTION_LICENSE.md",
    "docs/PARTY_MODE.md",
    ".github/workflows/release.yml",
)


def main() -> int:
    missing_files = [
        relative_path
        for relative_path in REQUIRED_FILES
        if not (PROJECT_ROOT / relative_path).is_file()
    ]

    if missing_files:
        print("Missing required files:")
        for relative_path in missing_files:
            print(f"- {relative_path}")
        return 1

    python_files = sorted(PACKAGE_ROOT.rglob("*.py"))
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    project_root_text = str(PROJECT_ROOT)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)

    from music_vault.core import paths
    from music_vault.core.desktop_shortcut import create_or_update_desktop_shortcut
    from music_vault.core.ffmpeg import discover_ffmpeg
    from music_vault.core.library_browser import load_album_summaries, load_artist_summaries
    from music_vault.core.audio_analysis import AudioAnalyzer
    from music_vault.core.app_status import write_app_status
    from music_vault.core.watchtower_status import write_watchtower_status
    from music_vault.ui.components import EmptyState, IconButton, SearchField
    from music_vault.ui.icons import REQUIRED_ICONS, icon_path
    from music_vault.ui.media_grid import MediaGridModel, MediaGridView
    from music_vault.ui.thumbnail_cache import ThumbnailCache
    from music_vault.metadata.artist_images import ArtistImageCache, ArtistImageService
    from music_vault.metadata.artist_credits import ArtistCreditService
    from music_vault.metadata.discogs_artwork import DiscogsArtworkCache
    from music_vault.metadata.ensemble import build_metadata_ensemble
    from music_vault.metadata.intelligence import MetadataIntelligenceService
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    from music_vault.metadata.providers.discogs import DiscogsProvider
    from music_vault.metadata.title_parser import parse_youtube_title
    from music_vault.metadata.artwork import CoverArtArchiveProvider, prepare_local_artwork
    from music_vault.metadata.musicbrainz_enricher import MusicBrainzProvider
    from music_vault.metadata.matching import classify_candidates
    from music_vault.metadata.remediation import RemediationService
    from music_vault.metadata.tag_writer import SafeTagWriter
    from music_vault.metadata.service import MetadataService
    from music_vault.ui.metadata_editor import MetadataEditorDialog
    from music_vault.ui.metadata_tasks import MetadataTaskRunner
    from music_vault.ui.metadata_remediation import MetadataRemediationDialog
    from music_vault.ui.review import schedule_ui_review
    from music_vault.ui.theme import application_stylesheet
    from music_vault.ui.onboarding import FirstRunWizard, inspect_runtime_evidence
    from music_vault.ui.party_mode import PartyModeWindow, normalize_party_mode_settings
    from music_vault.ui.party_palette import PaletteExtractor
    from music_vault.ui.party_visuals import PartyCanvas
    from music_vault.version import APP_VERSION, RELEASE_CHANNEL
    import music_vault.app as app

    if not callable(write_app_status) or write_watchtower_status is not write_app_status:
        print("App status exporter or compatibility alias is invalid.")
        return 1

    if APP_VERSION != "1.1.0" or RELEASE_CHANNEL != "development":
        print("Central development version metadata is invalid.")
        return 1

    if not all(
        callable(value)
        for value in (
            discover_ffmpeg,
            create_or_update_desktop_shortcut,
            FirstRunWizard,
            inspect_runtime_evidence,
        )
    ):
        print("Music Vault portable-release components are unavailable.")
        return 1

    if not application_stylesheet().strip():
        print("Music Vault UI stylesheet is empty.")
        return 1

    missing_icons = [name for name in REQUIRED_ICONS if not icon_path(name).is_file()]
    if missing_icons:
        print(f"Missing required UI icons: {', '.join(missing_icons)}")
        return 1

    if not all(callable(value) for value in (EmptyState, IconButton, SearchField, schedule_ui_review)):
        print("Music Vault UI components or review hook are unavailable.")
        return 1

    if not all(
        callable(value)
        for value in (
            AudioAnalyzer,
            PartyModeWindow,
            normalize_party_mode_settings,
            PaletteExtractor,
            PartyCanvas,
        )
    ):
        print("Music Vault Party Mode components are unavailable.")
        return 1

    if not all(
        callable(value)
        for value in (
            load_album_summaries,
            load_artist_summaries,
            MediaGridModel,
            MediaGridView,
            ThumbnailCache,
            ArtistImageCache,
            ArtistImageService,
            ArtistCreditService,
            DiscogsArtworkCache,
            DiscogsProvider,
            MetadataIntelligenceJobStore,
            MetadataIntelligenceService,
            build_metadata_ensemble,
            parse_youtube_title,
            MetadataService,
            MusicBrainzProvider,
            CoverArtArchiveProvider,
            prepare_local_artwork,
            MetadataEditorDialog,
            MetadataTaskRunner,
            MetadataRemediationDialog,
            RemediationService,
            SafeTagWriter,
            classify_candidates,
        )
    ):
        print("Music Vault media-browser components are unavailable.")
        return 1

    expected_data_dir = PROJECT_ROOT / "data"
    resolved_paths = {
        "project root": paths.project_root(),
        "data directory": paths.data_dir(),
        "database": paths.database_path(),
        "config": paths.config_path(),
        "status": paths.app_status_path(),
        "artist images": paths.artist_images_dir(),
        "metadata reports": paths.metadata_reports_dir(),
        "metadata job backups": paths.metadata_job_backups_dir(),
        "Discogs token": paths.discogs_token_path(),
        "Discogs covers": paths.discogs_covers_dir(),
        "Discogs provider cache": paths.discogs_provider_cache_dir(),
    }

    if resolved_paths["project root"] != PROJECT_ROOT:
        print(f"Unexpected project root: {resolved_paths['project root']}")
        return 1

    if resolved_paths["data directory"] != expected_data_dir:
        print(f"Unexpected data directory: {resolved_paths['data directory']}")
        return 1

    for name in ("database", "config", "status", "artist images"):
        if resolved_paths[name].parent != expected_data_dir:
            print(f"{name.title()} path is outside the data directory: {resolved_paths[name]}")
            return 1

    for name in (
        "metadata reports",
        "metadata job backups",
        "Discogs token",
        "Discogs covers",
        "Discogs provider cache",
    ):
        if not resolved_paths[name].is_relative_to(expected_data_dir):
            print(f"{name.title()} path is outside the data directory: {resolved_paths[name]}")
            return 1

    print(f"Imported app: {Path(app.__file__).resolve()}")
    print(f"Resolved project root: {resolved_paths['project root']}")
    print(f"Resolved data directory: {resolved_paths['data directory']}")
    print(f"Parsed active Python files: {len(python_files)}")
    print("Music Vault verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
