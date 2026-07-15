<p align="center">
  <img src="assets/icons/music_vault_icon.png" alt="Music Vault icon" width="128">
</p>

# Music Vault

Music Vault is a standalone, local-first Windows music library and player. It
imports music already on the computer and can optionally synchronize authorized
public or unlisted YouTube playlists into a persistent local library.

**Latest public release: v1.0.0 Stable.** See the
[v1.0.0 release notes](docs/releases/v1.0.0.md) for installation, first-run,
licensing, and known-limit details.

**Current main development line: v1.1.0 Development.** Batch 9 adds
full-screen audio-reactive Party Mode; Batch 9.1 refines its musical motion and
adds optional local-first lyrics. No v1.1.0 tag or public v1.1.0 release has
been created. Music Vault has no Watchtower relationship or runtime dependency.

## Install the portable release

1. Download `MusicVault-v1.0.0-Windows-x64-Portable.zip` from the
   [GitHub Releases page](https://github.com/jcjohnson9/music-vault/releases).
2. Verify the published SHA-256 checksum, then extract the complete folder to a
   writable location. Do not run the application from inside the ZIP.
3. Run `MusicVault.exe` and complete the first-run guide.

The portable package starts blank. It contains no personal library, database,
playlist, configuration, API key, media, artwork, report, backup, or status
file. Windows may show a SmartScreen warning because v1.0.0 is not code-signed.

Local import and playback need neither a YouTube API key nor FFmpeg. YouTube
synchronization is optional and requires a locally stored YouTube Data API key,
an authorized public or unlisted playlist, and separately installed
`ffmpeg.exe` plus `ffprobe.exe`. FFmpeg command-line tools are not bundled or
downloaded automatically.

## Core V1 capabilities

- Local SQLite library, custom playlists, album and artist browsers, search,
  cover art, and Qt Multimedia playback
- Seek, persisted volume, default Windows audio output, autoplay, shuffle,
  repeat, and a FIFO manual queue that resumes its original context
- Optional authorized public/unlisted YouTube playlist synchronization with
  full pagination, incremental video-ID reconciliation, structured failures,
  retry, and truthful completion states
- Local settings for downloads, conversion quality, API readiness, FFmpeg
  readiness, data location, and a non-admin desktop shortcut
- Trusted Metadata editing with provenance, protected manual/confirmed values,
  grouped history, undo, explicit MusicBrainz candidate review, and validated
  artwork
- Resumable existing-library remediation with analysis before apply, strict
  high-confidence automation, private reports, verified MP3 backups/writeback,
  unchanged-audio checks, and conflict-aware rollback
- Fast SQL-backed album/artist grids, optional privacy-aware artist photos, and
  the premium scalable Windows desktop UI
- Neutral, versioned local App Status JSON for optional local consumers

Music Vault does not silently inspect browser cookies, start synchronization or
metadata remediation on launch, auto-apply uncertain metadata matches, or scan
the entire computer.

## v1.1.0 development preview

Current `main` includes Party Mode, an optional full-screen now-playing
experience with Static, Starfield, Aurora, Orb Cluster, Fireworks, and Pulse in
that order. Static is the default. The album remains fixed in every preset
except the restrained four-beat Pulse, while a smooth beat clock turns transient
analysis into phrase-scale motion. Party Mode reuses the existing player,
output, queue, playback context, volume, and transport behavior.

Lyrics are Off by default and appear in a separate overlay directly above the
playback bar. Local/manual, adjacent, embedded, and cached sources are checked
before an optional consent-gated LRCLIB lookup. Synchronized lyrics follow the
player position; plain lyrics are labeled and never presented as synchronized.
Provider results are cached privately under `data/lyrics/`, never written into
audio files, App Status, or public logs, and never bundled into releases. The
visual pipeline records no audio or PCM and performs no networking; only the
separately enabled lyrics lookup may send the current title, artist, optional
album, and duration to LRCLIB. See [Party Mode](docs/PARTY_MODE.md) and
[Lyrics](docs/LYRICS.md).

## First launch and local data

The first-run guide appears only for a genuinely blank runtime. It validates a
writable data location, offers an optional local-folder import, and lets the
user continue without YouTube or FFmpeg. YouTube setup requires acknowledgement
of the [Authorized Use](docs/AUTHORIZED_USE.md) notice; local-only use does not.

By default, a portable copy stores private runtime data in `data` beside
`MusicVault.exe`. A different writable location can be selected during first-run
setup; Settings reports and opens the active location. That location can contain
the database, API-key file, configuration, status, downloaded media, artwork,
archives, remediation state, private lyric cache, and backups.
Back it up as private personal data and never add it to source control or a
public release. See [Data and Privacy](docs/DATA_AND_PRIVACY.md).

## Authorized synchronization and metadata

Use synchronization only for music that you own or are authorized to download.
Public and unlisted playlists are supported; private-playlist OAuth is not.
Failed items remain visible and are retried on a later manual sync.

YouTube upload dates remain source provenance rather than canonical release
dates. Manual metadata work is local. Music Vault contacts MusicBrainz only for
an explicit candidate search or library analysis; uncertain items remain
unchanged. See the [Metadata Model](docs/METADATA_MODEL.md) and
[Metadata Remediation](docs/METADATA_REMEDIATION.md).

## Source development

Music Vault source targets Python 3.11 on Windows:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Common engineering commands:

```powershell
.\tools\dev\verify.ps1
.\tools\dev\pre_public_commit_check.ps1
.\tools\dev\pre_public_history_check.ps1
.\tools\dev\run_source.ps1
.\tools\dev\build_exe.ps1
.\tools\dev\run_exe.ps1
.\tools\dev\capture_ui_review.ps1
.\tools\dev\run_party_mode_review.ps1
.\tools\dev\profile_media_browsers.ps1
.\tools\dev\remediate_library_metadata.ps1 status
.\.venv\Scripts\python.exe -B -m pytest -q
```

Release builds use the exact versions in `requirements-release.txt` and the
checked-in `MusicVault.spec`:

```powershell
.\tools\dev\build_exe.ps1
.\tools\release\build_portable_release.ps1
.\tools\release\verify_portable_release.ps1 `
  .\release_artifacts\MusicVault-v1.0.0-Windows-x64-Portable.zip `
  --release-version 1.0.0
```

The ordinary commands above build the checked-out tree. Corrective publication
for the already-pushed immutable `v1.0.0` tag instead uses the separate tagged
application/current-tooling rehearsal after the corrective tooling is committed
and the working tree is clean:

```powershell
.\tools\dev\pre_public_history_check.ps1
.\tools\release\rehearse_tagged_release.ps1 -ReleaseTag v1.0.0
```

That path does not move or recreate the tag. Its release manifests identify the
tagged application commit and later release-tooling commit separately.

Generated builds, screenshots, benchmarks, release staging, and all runtime
data remain untracked. See [Developer tools](tools/dev/README.md),
[Architecture](docs/ARCHITECTURE.md), and [Contributing](CONTRIBUTING.md) before
proposing a change.

## Product boundaries and roadmap

Music Vault is a standalone application. Neutral Prime interoperability is only
a possible external future option. Android, multiple source playlists, Best
Original quality, an installer/updater, an editable queue panel, and personal
radio are not V1 requirements. Batch 9 Party Mode and Batch 9.1 motion/lyrics
refinement are complete on the v1.1.0 development line; Batch 10 Multiple
Source Playlists is next. See the [roadmap](docs/ROADMAP.md).

## Licensing

Music Vault source written for this repository remains under the
[MIT License](LICENSE). Third-party components retain their own licenses. The
combined v1.0.0 portable Windows distribution is provided under
GPL-3.0-or-later because it embeds GPL-covered Mutagen, while separately
licensed components keep their terms; it is not an MIT-only binary.

Each binary release includes third-party notices and is accompanied by a source-
compliance archive with the exact tagged source, build inputs, license texts,
and source/relinking information. See
[Third-Party Notices](THIRD_PARTY_NOTICES.md) and
[Binary Distribution License](docs/BINARY_DISTRIBUTION_LICENSE.md). The project
licenses do not grant rights to third-party music, artwork, metadata, APIs,
websites, or services.
