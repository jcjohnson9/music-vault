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
full-screen audio-reactive Party Mode, Batch 9.1 refines its musical motion and
adds optional local-first lyrics, Batch 10 adds persistent Multiple Source
Playlists, and Batch 10.1 adds consent-gated Discogs-first metadata
intelligence. No v1.1.0 tag or public v1.1.0 release has been created. Music
Vault has no Watchtower relationship or runtime dependency.

Batch 10.2 preserves source-identity timestamps during no-op schema work.
Batch 10.3 adds schema-v7 canonical albums and artists, role-aware artist
pages, field-level review outcomes, soundtrack-aware acceptance, safer artist
portrait fallback, and a guarded application-wide Spacebar Play/Pause shortcut.
Batch 10.4 adds process-local migration-startup quiescence, centralized
acceptance no-secret/no-network controls, and lazy optional-provider
construction. A launch that actually upgrades the database defers optional
external work for the rest of that process; the next ordinary non-migration
launch can resume the user's enabled provider behavior without rewriting
settings.

Batch 10.5 completes the retained development batch with canonical
cross-provider artist clusters, cache-preserving portrait priority, provider-
adjudicated title orientation, best-available automatic metadata outcomes, and
one virtual **Singles & Uncatalogued** album collection. User-confirmed locks
remain authoritative, medium-confidence values stay database-only, and
automatic media-tag writeback remains limited to high-confidence fields.

Batch 10.6 makes dash-title adjudication deterministic: Music Vault evaluates
both `Artist - Title` orientations when necessary, stops after a conclusive
first Discogs result, permits at most two Discogs searches and one secondary
MusicBrainz search, and stores the selected orientation and safe decision
reasons without provider responses or credentials. Unresolved offline items
remain honest source fallbacks and eligible for a later bounded lookup.

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
  persistent saved sources, sequential Sync Selected/Sync All execution,
  cross-source video deduplication, origin-aware managed local playlists,
  complete-snapshot reconciliation, structured failures, retry, and truthful
  per-source/aggregate completion states
- Local settings for downloads, conversion quality, API readiness, FFmpeg
  readiness, data location, and a non-admin desktop shortcut
- Trusted Metadata editing with provenance, protected manual/confirmed values,
  grouped history, undo, structured artist credits, explicit MusicBrainz
  candidate review, and validated artwork
- Resumable existing-library remediation with analysis before apply, strict
  high-confidence automation, private reports, verified MP3 backups/writeback,
  unchanged-audio checks, and conflict-aware rollback
- Optional Discogs-first automatic intelligence for title, artist credits,
  release/version context, and true artwork gaps; MusicBrainz remains a
  secondary authority, best-available outcomes retain field confidence and
  history, and uploader/channel names remain source provenance rather than
  assumed musical artists
- Fast SQL-backed canonical album/artist grids, edition-aware album cards,
  role-aware **Tracks**, **Featured On**, **Collaborations**, and **Group
  Appearances** sections, optional privacy-aware artist photos, and the premium
  scalable Windows desktop UI
- Neutral, versioned local App Status JSON for optional local consumers

Music Vault does not silently inspect browser cookies, start synchronization or
metadata remediation on launch, write medium/low-confidence metadata into media
tags, or scan the entire computer.

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

Multiple Source Playlists replaces the one-URL synchronization form with a
persistent Sync Center source manager. Each authorized public or unlisted
playlist retains independent identity, order, destination, membership, history,
and failure state. A source can target Library Only or one managed local
playlist; remote tracks appear first while manual additions remain after them.
Sync All runs enabled sources sequentially so overlapping videos reuse one
canonical library track and one valid media file. Failed/partial enumeration
never infers removals, and source removal safely preserves the local playlist,
library, media, metadata, artwork, lyrics, and history. See
[Multiple Source Playlists](docs/MULTIPLE_SOURCE_PLAYLISTS.md).

Discogs-first Metadata Intelligence is disabled until the user stores a
personal token, accepts the provider/privacy notice, and enables it in
Settings. It can enrich new imports in the background and offers a resumable
existing-library scan. Only strong, unambiguous, unlocked fields may apply
to media-file tags automatically. Best-available database values are selected
with confidence and history instead of waiting in an ordinary review queue;
rare mistakes remain manually correctable. Structured credits distinguish primary,
featured, collaborator, remixer, and performer roles. Studio, live, remix,
edit, acoustic, cover, slowed, sped-up, YouTube-exclusive, and other versions
remain separate tracks. See [Discogs Metadata](docs/DISCOGS_METADATA.md).

Canonical album cards group ordinary deluxe, expanded, anniversary, remaster,
reissue, format, country, and alternate-cover editions without changing any
track album string or `cover_path`. Live albums, soundtracks, scores, cast
recordings, compilations, EPs, singles, and remix albums remain distinct.
Canonical artist aliases and verified relationships unify safe display
variants while preserving conflicting same-name identities. Secondary gaps can
finish as **Applied with Gaps** or **Accepted Source Fallback**; manual review is
available as an optional audit/correction surface rather than an automatic-work
queue. See [Canonical Media Browser](docs/CANONICAL_MEDIA_BROWSER.md).

Space toggles the existing player's Play/Pause state from ordinary application
pages. Text fields, metadata/lyrics editors, dialogs, buttons, checkboxes,
sliders, and other controls retain their normal Space behavior; Party Mode
keeps its existing shortcut handler.

Database migration and optional external work are separate startup phases.
Music Vault records whether the current database instance performed an upgrade
and, for that process only, defers metadata intelligence, portrait resolution,
online lyrics, and other optional provider work. Acceptance no-secret mode
prevents YouTube API-key and Discogs-token content reads; acceptance no-network
mode blocks provider transport before construction. Existing valid cached
portraits remain available read-only, and App Status reports only the aggregate
deferred flag and safe reason.

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

YouTube upload dates and uploader/channel names remain source provenance rather
than canonical release dates or default musical artists. Manual metadata work
is local. Music Vault can contact Discogs only after explicit setup and consent;
MusicBrainz remains an explicit candidate source and secondary corroboration or
fallback. Unsupported fields remain blank or are recorded as explicit gaps;
accepted values remain reversible through metadata history. See the
[Metadata Model](docs/METADATA_MODEL.md), [Metadata Remediation](docs/METADATA_REMEDIATION.md),
and [Discogs Metadata](docs/DISCOGS_METADATA.md).

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
.\tools\dev\run_batch10_1_review.ps1 --offscreen
.\tools\dev\run_batch10_3_review.ps1
.\tools\dev\profile_media_browsers.ps1
.\tools\dev\run_batch10_4_packaged_quiescence_smoke.ps1
.\tools\dev\audit_batch10_4_artist_cache.ps1
.\tools\dev\remediate_library_metadata.ps1 status
.\.venv\Scripts\python.exe -B -m pytest -q
```

The Batch 10.4 live-startup wrapper is deliberately acknowledgment-gated. It
captures aggregate preservation evidence, creates a verified schema-v7 backup,
starts the official EXE once with no-secret/no-network controls, requests a
graceful close, and verifies quiescence. Use it only for the specifically
authorized acceptance procedure documented in [Developer tools](tools/dev/README.md).

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
a possible external future option. Android, Best Original quality, an
installer/updater, an editable queue panel, and personal radio are not V1
requirements. Batch 9 Party Mode, Batch 9.1 motion/lyrics refinement, Batch 10
Multiple Source Playlists, Batch 10.1 Discogs-first metadata intelligence,
Batch 10.2 timestamp preservation, Batch 10.3 canonical media browsing, Batch
10.4 migration-startup quiescence, and Batch 10.5 metadata acceptance and
artist-identity correction, plus Batch 10.6 dual-orientation metadata
acceptance, are complete on the v1.1.0 development line; Batch 11
Highest-Practical-Quality / Best Original is next. See the
[roadmap](docs/ROADMAP.md).

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
