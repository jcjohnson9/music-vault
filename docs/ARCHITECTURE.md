# Architecture

Music Vault is a standalone, local-first Windows desktop application. PySide6
provides the user interface, Qt Multimedia provides playback through
`QMediaPlayer`, and SQLite stores the local library and playlists.

## Source layout

| Path | Responsibility |
| --- | --- |
| `run.py` | Source entry point that creates and starts the application. |
| `music_vault/app.py` | Main PySide6 window, view orchestration, playback, queue, settings, synchronization orchestration, and status updates. |
| `music_vault/core/db.py` | SQLite schema setup and library/playlist persistence operations. |
| `music_vault/core/importer.py` | Mutagen-based media metadata and embedded-artwork import. |
| `music_vault/core/youtube_sync.py` | YouTube Data API enumeration, video-ID reconciliation, and authorized yt-dlp/FFmpeg acquisition. |
| `music_vault/core/paths.py` | Central project, runtime-data, asset, and frozen-application path resolution. |
| `music_vault/core/watchtower_status.py` | Versioned, read-only-for-consumers external status JSON export. |
| `music_vault/metadata/musicbrainz_enricher.py` | Optional MusicBrainz metadata lookup. |
| `music_vault/metadata/cover_art.py` | Optional Cover Art Archive artwork retrieval. |
| `MusicVault.spec` | PyInstaller configuration for the packaged Windows application. |

The `watchtower_status.py` filename is a legacy internal name for a generic
external-status export. Music Vault has no Watchtower runtime dependency, and
no Watchtower integration is planned. The module name and status schema remain
unchanged in the current release-candidate batch to avoid an unrelated behavior
or compatibility change.

## Primary data flow

```text
source playlist
  -> YouTube Data API enumeration
  -> stable video-ID comparison
  -> authorized yt-dlp and FFmpeg processing
  -> local media files
  -> Mutagen metadata and artwork import
  -> SQLite library
  -> PySide6 browsing and QMediaPlayer playback
```

The YouTube Data API supplies playlist enumeration. yt-dlp and FFmpeg perform
authorized acquisition and audio processing. Mutagen reads media metadata and
embedded artwork. MusicBrainz and Cover Art Archive are optional enrichment
services. None of these external services owns the local Music Vault library.

## Data and artifact boundaries

### Source code

Application modules, assets, documentation, development tools, dependency
manifests, and the PyInstaller specification belong in source control. Source
code must not contain credentials or private library content.

### Runtime data

The local `data/` directory contains user-specific state such as the SQLite
database, configuration, API-key file, synchronization state, media, artwork,
status export, and reports. Runtime data is private and excluded from source
control and public packages.

### Build output

PyInstaller generates `build/` intermediates and the packaged application under
`dist/`. These are generated artifacts, not source, and are excluded from the
repository.

### External services and tools

YouTube and the YouTube Data API provide source-playlist information. yt-dlp
and FFmpeg handle authorized media processing. MusicBrainz and Cover Art
Archive can provide metadata and artwork. The application should continue to
separate these integrations from local library ownership and persistence.

## Known architectural debt

The current architecture is functional and does not require a wholesale
rewrite. Known areas for incremental improvement are:

- `music_vault/app.py` has broad responsibilities and is large;
- SQLite setup has no versioned schema-migration framework;
- canonical metadata, source metadata, provenance, confidence, and manual
  overrides are not fully modeled;
- synchronization assumes a single configured source workflow;
- there is no portable manifest for selective mobile transfer; and
- there is no station, program-timeline, or mixed audio-segment model for the
  future personal-radio branch.

Future extraction should preserve working playback, queue, synchronization,
path-resolution, and persistence behavior while introducing narrower module
boundaries as needed.
