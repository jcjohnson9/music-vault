<p align="center">
  <img src="assets/icons/music_vault_icon.png" alt="Music Vault icon" width="128">
</p>

# Music Vault

Music Vault is a standalone, local-first personal music system that transforms
authorized public or unlisted YouTube playlists into a persistent local library
with metadata, artwork, playlists, and playback.

## Current status

**v1.0.0 Release Candidate — not yet V1 Stable**

The core Windows application and premium desktop UI are established, while
remaining release-candidate gates are tracked in the
[project roadmap](docs/ROADMAP.md). Music Vault is designed for personal,
local-first use; its source code is available under the [MIT License](LICENSE).

## Core capabilities

- Public and unlisted YouTube playlist synchronization through the YouTube Data API
- Anonymous yt-dlp extraction with no silent browser-cookie access
- Full API pagination for large playlists
- Incremental acquisition reconciled from stable video IDs and real local files
- Truthful complete, complete-with-issues, and failed synchronization outcomes
- Structured failed-item history with retry on the next manual sync
- Authorized media acquisition with yt-dlp and FFmpeg
- Persistent local library backed by SQLite
- Embedded artwork extraction and artwork display
- Album and artist browsing
- Custom local playlists
- Local playback with seek and persisted volume controls
- Independent now-playing identity with active-row tracking across automatic,
  queued, next, and previous playback
- Autoplay, shuffle, and repeat modes
- Temporary FIFO queue that resumes its original playback context
- Windows default audio-output following
- Local settings for the API key, download folder, and audio quality
- PyInstaller-based Windows EXE workflow with a custom icon
- Centralized premium dark UI system with original scalable icons, responsive
  desktop layouts, accessible focus states, and native-title-bar integration
- Source verification, build, launch, and publication-safety tooling
- Versioned, neutral App Status JSON for optional local consumers

Music Vault does not currently provide private-playlist OAuth, multiple source
playlists, Android support, Prime control, radio stations, AcoustID matching, or
a complete metadata editor.

## Product boundaries

Music Vault is a standalone application. It is not a Watchtower module, and
Watchtower has no planned role in the product. The existing versioned status
document is generic local infrastructure and does not create a Watchtower
runtime dependency.

Neutral interoperability with Prime is only a possible future option. A
separate Android application and personal radio system are also future
ambitions, not current features or requirements for this release candidate.

## Requirements

- Windows
- Python 3.11 for source development
- FFmpeg for synchronized-media conversion
- A YouTube Data API key for playlist synchronization

Install FFmpeg before using Sync Center and make it discoverable by FFmpeg-aware
tools, normally through `PATH`.

## Source setup

From the project root, create a project-local Python 3.11 virtual environment
and install the runtime dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For development and EXE build tooling, install the development requirements:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Launch the source application with:

```powershell
.\tools\dev\run_source.ps1
```

## Synchronizing an authorized playlist

1. Open **Sync Center**.
2. Enter a public or unlisted YouTube playlist URL.
3. Choose a local download folder and audio-quality setting as needed.
4. Confirm that you are authorized to download the playlist content.
5. Select **Start Sync**.

Music Vault enumerates the playlist through the YouTube Data API, compares
stable video IDs with local state, acquires missing authorized items, and imports
only the resulting new or newly discovered local files into the library. Public
and unlisted playlists are supported; Music Vault does not silently inspect
browser cookie stores. Failed items are reported and retried on the next manual
sync rather than permanently suppressed.

YouTube upload dates are stored as source provenance. They are not presented as
canonical musical release years. Local imports may continue to use legitimate
embedded release-year metadata, and explicit MusicBrainz enrichment can supply
canonical metadata after user confirmation.

See [Authorized Use](docs/AUTHORIZED_USE.md) before using synchronization.

## Local configuration and privacy

The YouTube Data API key, database, configuration, status, artwork, archives,
downloaded media, and pre-migration database backups are stored locally under
the runtime data area. Database backups are written to `data/backups/` before a
non-empty older schema is upgraded. These files
are intentionally excluded from Git and are not part of a source checkout.

Download-folder and audio-quality choices are local settings. Never paste API
keys, private playlist details, database files, status files, or unsanitized
local paths into an issue. See [Data and Privacy](docs/DATA_AND_PRIVACY.md) and
[Security](SECURITY.md).

## Developer workflow

The PowerShell helpers resolve the project root and use the project-local
virtual environment:

```powershell
.\tools\dev\verify.ps1
.\tools\dev\pre_public_commit_check.ps1
.\tools\dev\run_source.ps1
.\tools\dev\build_exe.ps1
.\tools\dev\run_exe.ps1
.\tools\dev\run_exe_from_temp.ps1
.\tools\dev\check_status.ps1
.\tools\dev\rebuild_and_run.ps1
.\tools\dev\v1_sanity_check.ps1
.\tools\dev\capture_ui_review.ps1
```

Run the synthetic regression suite with:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q
```

The UI review helper creates an isolated synthetic runtime and sanitized
screenshots outside the repository by default. It never uses the personal
database, API key, media, artwork cache, or network services. Screenshot output
is for local review only and is not committed automatically.

Build the one-folder Windows application with:

```powershell
.\tools\dev\build_exe.ps1
```

Source changes require rebuilding the EXE before a desktop shortcut reflects
them. To create or update the non-admin desktop shortcut after a build, run:

```powershell
.\tools\dev\install_desktop_shortcut.ps1
```

For a direct PyInstaller invocation, use the project-local environment and the
checked-in specification:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean .\MusicVault.spec
```

See [Architecture](docs/ARCHITECTURE.md) for the current code and data flow, and
[Contributing](CONTRIBUTING.md) before proposing a change.

## Remaining release-candidate gates

- Manual metadata correction is not yet complete.
- A clean, blank V1 distribution has not yet been published.

Correction work and release ordering are tracked in
[the roadmap](docs/ROADMAP.md).

## Screenshots

The developer review harness can generate sanitized synthetic screenshots for
Library, Albums, Artists, Sync Center, Settings, and empty states at common
desktop sizes. Personal-library and temporary review screenshots are
intentionally not included in source control.

## License

Music Vault source code is licensed under the [MIT License](LICENSE). The source
license does not grant rights to third-party music, artwork, metadata, APIs,
websites, or services used with the application.
