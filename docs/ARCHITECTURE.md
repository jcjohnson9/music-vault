# Architecture

Music Vault is a standalone, local-first Windows desktop application. PySide6
provides the user interface, Qt Multimedia provides playback through
`QMediaPlayer`, and SQLite stores the local library and playlists. Version
`1.0.0` remains the stable portable Windows release; current `main` is the
`1.1.0` development line.

## Source layout

| Path | Responsibility |
| --- | --- |
| `run.py` | Source entry point that creates and starts the application. |
| `music_vault/version.py` | Authoritative application name, current-tree version, release channel, public user agent, and Windows resource values. |
| `music_vault/app.py` | Main PySide6 window, view orchestration, playback, queue, settings, Sync Center integration, and status updates. |
| `music_vault/core/db.py` | Versioned additive SQLite migrations, failure history, and compatibility library/playlist APIs. |
| `music_vault/core/sync_schema.py` | Schema-v5 saved-source, source-item/run, global source identity/conflict, playlist-origin, index, and additive backfill definitions. |
| `music_vault/core/sync_sources.py` | YouTube playlist identity normalization, stable storage keys, persistent source CRUD/order/destination/archive operations, and source-card projections. |
| `music_vault/core/playlist_membership.py` | Central origin-aware materialization of source-managed and manual playlist membership into the compatibility `playlist_tracks` table. |
| `music_vault/core/multi_source_sync.py` | Sequential selected/enabled-source execution, per-source import/reconciliation, bounded batch aggregation, and Stop After Current coordination. |
| `music_vault/core/importer.py` | Source-aware Mutagen metadata and embedded-artwork import. |
| `music_vault/core/youtube_sync.py` | Public/unlisted API enumeration, authoritative video-ID reconciliation, and anonymous yt-dlp/FFmpeg acquisition. |
| `music_vault/core/sync_result.py` | Typed synchronization outcome shared by engine, UI, status, logging, and tests. |
| `music_vault/core/safety.py` | Secret redaction, video-ID extraction, source-date normalization, and safe output paths. |
| `music_vault/core/playback_state.py` | Pure volume normalization/config filtering and track-ID-to-table-row helpers. |
| `music_vault/core/audio_analysis.py` | Bounded PCM conversion, spectral features, smoothing, beat detection, and latest-buffer analysis for Party Mode. |
| `music_vault/core/musical_motion.py` | Bounded tempo estimation, smooth beat/bar/phrase phase, missed-beat continuation, and deterministic sparse accent scheduling. |
| `music_vault/core/library_browser.py` | SQL-aggregated album/artist summaries, stable browser identities, exact track lookup, revision fingerprints, and summary-cache invalidation. |
| `music_vault/core/paths.py` | Central source, portable-marker, selected runtime-data, asset, and frozen-application path resolution. |
| `music_vault/core/ffmpeg.py` | Bounded discovery and validation of a complete external `ffmpeg.exe`/`ffprobe.exe` pair. |
| `music_vault/core/desktop_shortcut.py` | Non-admin portable desktop-shortcut inspection, conflict handling, and creation. |
| `music_vault/core/app_status.py` | Versioned, read-only-for-consumers neutral App Status JSON export. |
| `music_vault/core/watchtower_status.py` | Temporary compatibility re-export for the former module name. |
| `music_vault/ui/theme.py` | Central colors, spacing, radii, typography, shared QSS, and guarded native dark-title-bar integration. |
| `music_vault/ui/icons.py` | Safe source/frozen lookup plus cached, tinted, high-DPI rendering for original SVG UI assets. |
| `music_vault/ui/components.py` | Focused reusable controls for icon buttons, elided text, search, overflow actions, headers, and empty states. |
| `music_vault/ui/browser_loader.py` | Token-guarded background summary jobs using short-lived read-only database connections. |
| `music_vault/ui/media_grid.py` | Reusable album/artist models, filter proxy, delegate-painted responsive grid, state presentation, and visible-range discovery. |
| `music_vault/ui/thumbnail_cache.py` | Bounded memory LRU, coalesced background QImage decoding, high-DPI thumbnail keys, and stale-generation protection. |
| `music_vault/ui/metadata_editor.py` | Trusted Metadata dialog for field actions, source inspection, candidate review, history, and undo. |
| `music_vault/ui/metadata_tasks.py` | Cancellable, stale-result-safe background provider work for the metadata editor. |
| `music_vault/ui/onboarding.py` | Blank-runtime detection helpers and the local-first, optional-sync first-run guide. |
| `music_vault/ui/review.py` | Explicitly environment-gated synthetic screenshot controller; inert during normal application use. |
| `music_vault/ui/party_mode.py` | Full-screen Party Mode lifecycle, overlay, command routing, screen selection, and existing-player bridge. |
| `music_vault/ui/party_visuals.py` | Shared PartyCanvas renderer, six bounded visual presets, centralized album transform, frame timing, and adaptive quality. |
| `music_vault/ui/party_palette.py` | Deterministic artwork palette extraction, contrast handling, caching, and color interpolation. |
| `music_vault/ui/party_lyrics.py` | Independent synchronized/plain lyrics overlay, loading/empty states, position lookup, scrolling, and provider attribution. |
| `music_vault/ui/sync_center.py` | Persistent source-list/detail manager, aggregate synchronization dashboard, source dialogs, ordering, destination, and safe-removal controls. |
| `music_vault/lyrics/` | Bounded lyric models/parser/cache/service, read-only local/embedded/sidecar discovery, and the provider-neutral lookup boundary. |
| `music_vault/lyrics/providers/lrclib.py` | Consent-gated, read-only LRCLIB client with HTTPS destination controls, bounded responses, and strict result matching. |
| `music_vault/metadata/artist_images.py` | Provider-neutral artist-photo resolution, confidence checks, safe public networking, runtime cache/provenance, and background request service. |
| `music_vault/metadata/schema.py` | Schema-v3 field/observation/history tables, release-date validation, conservative migration seeding, and required indexes. |
| `music_vault/metadata/remediation_schema.py` | Additive schema-v4 remediation job, item, cache, constraint, and index definitions. |
| `music_vault/metadata/service.py` | Transactional metadata authority for precedence, materialization, locks, manual/confirmed/high-confidence changes, history, undo, and rollback snapshots. |
| `music_vault/metadata/matching.py` | Provider-query normalization, recording/release scoring, risk detection, ambiguity policy, and typed field-level decisions. |
| `music_vault/metadata/remediation.py` | Resumable analyze/apply/verify/rollback coordinator and private aggregate/item reporting. |
| `music_vault/metadata/tag_writer.py` | Verified MP3 full-file backup, temporary-copy tag writeback, audio-payload checks, atomic commit, and restore. |
| `music_vault/metadata/musicbrainz_enricher.py` | Typed, explicit MusicBrainz candidate search with bounded public networking. |
| `music_vault/metadata/artwork.py` | Validated content-addressed local and Cover Art Archive artwork storage. |
| `music_vault/metadata/cover_art.py` | Compatibility helper for existing Cover Art Archive behavior. |
| `MusicVault.spec` | PyInstaller configuration for the packaged Windows application. |
| `tools/release/` | Deterministic portable/source-compliance builders, release manifest and checksum generation, license inventory, and fail-closed verification. |
| `tools/dev/profile_media_browsers.py` | Synthetic-only 300/1,000/5,000-track query, model, render, thumbnail, and revisit profiler. |
| `tools/dev/run_party_mode_review.py` | Temporary synthetic-audio/artwork PartyCanvas/PartyModeWindow review matrix and bounded offscreen frame benchmark. |
| `tools/dev/remediate_library_metadata.py` | Aggregate-only status, analyze, resume, explicitly confirmed apply/writeback, verify, report, and rollback interface. |

Music Vault has no Watchtower runtime dependency or integration. Active code
uses `app_status.py`; `watchtower_status.py` only preserves import compatibility.
The `data/music_vault_status.json` filename and schema version remain compatible.

## Primary data flow

```text
saved source selection in Sync Center
  -> sequential source orchestrator in persisted order
  -> complete YouTube playlist-item snapshot
  -> global video-to-track identity and valid-file reconciliation
  -> authorized yt-dlp and FFmpeg processing when genuinely needed
  -> per-source import, occurrence reconciliation, and truthful run/failure state
  -> origin-aware managed-playlist materialization
  -> targeted Mutagen/source observations
  -> metadata precedence and effective field materialization
  -> schema-v5 SQLite library, source, provenance, history, and remediation state
  -> PySide6 browsing and the existing QMediaPlayer playback pipeline
```

The YouTube Data API supplies complete public/unlisted playlist-item snapshots.
yt-dlp operates anonymously and does not inspect browser cookie profiles.
Enabled sources run sequentially; every source is imported and reconciled
before the next starts, allowing overlapping video identities to reuse one
canonical track and valid media file. Only complete enumeration can remove a
source occurrence. yt-dlp and FFmpeg perform authorized acquisition and audio
processing. Mutagen reads media metadata and
embedded artwork. MusicBrainz and Cover Art Archive are optional enrichment
services. When separately enabled by the user, artist-photo lookup uses
MusicBrainz identity followed by public Wikidata, Wikipedia, or Wikimedia
image metadata. None of these external services owns the local Music Vault
library.

## Data and artifact boundaries

### Source code

Application modules, assets, documentation, development tools, dependency
manifests, and the PyInstaller specification belong in source control. Source
code must not contain credentials or private library content. Music Vault's own
source remains MIT licensed; that does not make the combined portable binary an
MIT-only distribution.

### Runtime data

In a default portable installation, the `data/` directory beside
`MusicVault.exe` contains user-specific state such as the SQLite database,
configuration, API-key file, synchronization state, media, artwork,
artist-image files and provenance, App Status, remediation state/reports, and
database/media backups. The first-run guide can select another writable data
directory; a small per-executable locator under local application data retains
that selection. Every runtime location is private and excluded from source
control and public packages.

### Build and release output

PyInstaller generates `build/` intermediates and the official one-folder
application under `dist/`. The release builder copies only the allowed runtime
payload and public notices into a fresh staging root, adds the portable marker,
manifest, and checksums, then creates the portable and source-compliance ZIPs.
Build, staging, and release artifacts are generated and excluded from source
control. The blank portable package contains no populated `data` directory.

The repository's source license is MIT. The combined v1.0.0 portable
distribution is conveyed under GPL-3.0-or-later because it embeds GPL-covered
Mutagen; PySide6/Qt, the Qt Multimedia FFmpeg shared libraries, and other
components retain their separate terms. Required texts and source/relinking
information ship in the release artifact set. The command-line
`ffmpeg.exe`/`ffprobe.exe` tools are not bundled.

### External services and tools

YouTube and the YouTube Data API provide source-playlist information. yt-dlp
and separately configured FFmpeg tools handle authorized media processing.
MusicBrainz and Cover Art
Archive can provide metadata and artwork. The application should continue to
separate these integrations from local library ownership and persistence.

## Portable release and first-run boundary

`music-vault.portable.json` identifies an extracted portable root beside
`MusicVault.exe`. Frozen path resolution first honors a valid explicit
`MUSIC_VAULT_PROJECT_ROOT`, then the portable marker, source/development roots,
and existing development-dist compatibility before using a logged fallback. It
does not derive runtime data from an arbitrary shell working directory and does
not create runtime data inside `_internal`.

The marker's default data directory is `<portable-root>/data`. A user can choose
a different writable folder before database construction; the portable package
itself remains relocatable and needs neither the source repository nor a folder
named `dist`. An unwritable location produces a visible choice to select another
folder or exit, with no automatic elevation.

First-run onboarding is shown only when there is no established config, secret,
library, playlist, or prior runtime evidence. Established installations infer
completion and continue without resetting settings or paths. A blank user can
import a chosen local folder or start empty, and can skip YouTube and FFmpeg
setup entirely. The API key remains in its secret file, never JSON config;
authorized-use acknowledgement gates optional synchronization setup but not
local import/playback. Desktop shortcuts are explicit, per-user, and retain the
portable root as their working directory.

## Metadata authority boundary

Schema version 3 introduced three metadata-authority concepts. Provider/file values
are retained in `track_metadata_observations`; one effective state per editable
field lives in `track_metadata_fields`; and compatible effective columns remain
materialized in `tracks` for established queries and playback. Effective
changes are recorded by group in `track_metadata_history`. `release_date` is
canonical music information with `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` precision,
while `year` is derived from it and `source_upload_date` remains source context.

`MetadataService` is the single transactional authority for automatic
observations, manual set/clear, unlock, reset, confirmed candidates, history,
undo, and materialized-column consistency. Manual and user-confirmed locks
outrank embedded and YouTube fallbacks; lower-priority or empty automatic
observations cannot erase them. No-op and observation-only work does not create
history or advance the effective metadata timestamp.

The Trusted Metadata UI reads typed snapshots, exposes provenance/lock state,
and applies a multi-field change under one change-group ID. MusicBrainz search
is explicit and background-only; candidates are never auto-applied. Selected
Cover Art Archive artwork is fetched only after confirmation and stored with
validated manual/candidate artwork under content-addressed runtime cover
directories. This track-artwork system is independent from the optional artist
portrait cache.

Schema migration is additive and uses SQLite's backup API before changing a
non-empty older database. It seeds conservative state and observations without
provider access, media-tag reads, fabricated canonical dates, or history.
Schema version 4 adds only persisted remediation job/item/cache structures.

The remediation coordinator keeps analysis separate from apply. Analysis
snapshots state, uses cached/rate-limited provider candidates, and writes only
private job/cache/report records. Apply rechecks the aggregate library revision,
per-item metadata/locks, candidate age, file state, and disk estimate. It routes
eligible database changes through `MetadataService`; manual and confirmed locks
remain authoritative. Distinct recording identity is evaluated separately from
album/release/artwork certainty, so ambiguous release fields stay review-only.

Supported MP3 writeback creates and verifies a complete original backup, edits
a temporary copy, validates tag readback and unchanged audio-payload hash/codec/
duration, then atomically replaces the source. Rollback restores the verified
file and exact pre-apply field/provenance/ID snapshot unless later changes create
a conflict. Unsupported formats never claim file success. See
[`METADATA_MODEL.md`](METADATA_MODEL.md) and
[`METADATA_REMEDIATION.md`](METADATA_REMEDIATION.md) for the complete contracts.

## Playback state boundary

`current_track_id` is the authoritative now-playing identity and remains
independent from ordinary table selection. Each table rebuild creates a
database-track-ID-to-row map, allowing the active-row treatment to follow
automatic, shuffled, queued, next, previous, and error-continuation playback
without changing pages or queue/base context. Volume is normalized in memory,
applied consistently to the slider and audio output, and persisted through a
short debounce with a final close-time flush.

## Media-browser performance boundary

Albums and Artists use lightweight immutable summaries rather than
materializing every track and constructing a QWidget tree per card. Album
identity combines trimmed/case-folded album title, album artist with a
conservative track-artist fallback, and canonical year. Artist identity trims
and case-folds the complete credit without splitting collaborations or
guessing canonical people. The same keys drive exact track lookup, preventing
same-title releases from collapsing merely because their titles match.

Summary work runs through a short-lived query-only SQLite connection and is
applied only while its request token and library revision remain current.
Revision-aware caches are invalidated after imports, successful sync imports,
missing-track removal, enrichment, and artwork changes, but not after playback,
queue, volume, search, or App Status changes. Album artwork is decoded as QImage
work in a bounded worker pool; QPixmap creation and model updates stay on the
GUI thread. Only visible and near-visible keys request thumbnails, and duplicate
requests coalesce.

The committed profiler uses temporary current-schema synthetic databases and no
network or personal files. A representative post-change run measured:

| Synthetic scale | Albums query | Artists query | Album/artist summaries |
| --- | ---: | ---: | ---: |
| 300 tracks | 1.863 ms | 1.413 ms | 100 / 200 |
| 1,000 tracks | 5.428 ms | 3.795 ms | 300 / 600 |
| 5,000 tracks | 23.833 ms | 14.274 ms | 1,000 / 2,000 |

Cached revisits were at most 0.007 ms, the grid created zero per-card QWidgets,
and only the 25 visible/near-visible entries were considered for artwork.
These measurements are development-machine evidence, not hard timing promises;
structural profiler failures are gates while timing variance is informational.

## Artist-image boundary

Album cards use album covers. Artist cards never substitute an album cover for
an artist portrait: they use a dedicated original unknown-artist asset until a
credible cached artist photo exists. External fetching is controlled by the
`artist_image_fetch_enabled` setting and defaults to false. Valid cached photos
can still render while fetching is disabled.

The initial provider is accessed through a provider-neutral interface. It
requires one unique high-confidence exact normalized-name MusicBrainz match,
then follows validated MusicBrainz relations to Wikidata or English Wikipedia
and obtains image metadata from Wikimedia services. Ambiguous or low-confidence
results remain unknown. Requests are background, capped, rate-limited, HTTPS
only, host-whitelisted, DNS-checked against private/local destinations, bounded
by time and response size, and content-validated before caching.

`data/artist_images/index.json` is a versioned atomic manifest containing
normalized request identity, match/provenance fields, safe source URLs, status,
and retry timestamps. Content-addressed files live below
`data/artist_images/files/`. Negative matches are cached longer than temporary
network failures. Clearing this cache does not modify tracks, the database, or
neighboring runtime data.

## UI system and review boundary

The premium visual system is centralized under `music_vault/ui/`; `app.py`
retains page orchestration and established behavior while consuming shared
tokens, static QSS, cached original SVG icons, narrowly reusable controls, and
the delegate-painted media browser. Resizing reflows the item view without
constructing card widgets or repeating database grouping.

`tools/dev/capture_ui_review.py` creates a temporary marker-valid project root,
synthetic current-schema data, generated artwork, and non-media sentinel files. The
packaged/source capture hook activates only when its explicit review environment
variable and validated plan are present. Visible paths are neutralized before
capture, the runtime is deleted afterward, and screenshot output remains an
ignored local review artifact rather than product or personal data.

## Party Mode boundary

Party Mode is a presentation client of the existing `QMediaPlayer` and
`QAudioOutput`. Its top-level window never owns a second player, never changes a
source on entry or exit, and routes transport, queue, Auto, Shuffle, Repeat,
seek, and volume commands through the established main-window behavior. The
Party window can therefore appear or disappear without restarting a track or
changing playback context.

When supported by the active Qt backend, a `QAudioBufferOutput` attached to the
same media player supplies decoded buffers to the bounded analysis pipeline.
PCM is copied only into small transient analysis windows; stale work is
replaced, not queued. Feature snapshots contain normalized aggregate energy,
spectral-band, beat, and availability values—not samples. Decoded audio is
never recorded, written to disk, placed in App Status, or sent over a network.
Backends without buffer output use an explicitly non-audio-reactive ambient
fallback derived from timing and playback state.

The musical-motion layer converts transient beat detections into continuous
beat, four-beat bar, and 32-beat phrase phases. It estimates a bounded tempo,
rejects outliers, continues calmly across missed beats, and corrects phase
without restarting animations. Raw beat flags do not directly move the album,
cluster, aurora, or particles.

`PartyCanvas` owns data-only particles and paints them in one widget. Static,
Starfield, Aurora, Orb Cluster, Fireworks, and Pulse share artwork-derived
palettes and bounded brightness/motion rules. A centralized transform keeps the
album exactly fixed outside Pulse; Pulse uses a restrained four-beat curve.
Static stops high-frequency visual simulation. Reduced motion lowers movement
and particle budgets. Auto quality uses measured frame history to reduce work
with hysteresis rather than oscillating. Palette extraction is cached per
artwork identity and performs no file mutation or provider request.

Lyrics remain a separate overlay above the playback bar and use the existing
player position, not the render clock. The lyrics service resolves manual,
adjacent, embedded, and cached local sources before optional network work. A
generation guard prevents a stale provider result from appearing after a track
change. Only one consent-gated LRCLIB request may be active, and strict matching
rejects ambiguous metadata. Cached bodies are content-addressed under the
selected runtime `data/lyrics/` directory; writes are atomic, filenames contain
no track titles, and fetched text is never written to media, App Status, or
public logs. See [Lyrics](LYRICS.md).

`tools/dev/run_party_mode_review.py` is a separate development harness. It sets
an isolated temporary project root, creates only synthetic WAV/artwork/metadata,
synthetic lyric inputs and fake provider responses, blocks public network
access, drives the canvas offscreen, records aggregate frame metrics, and
removes its runtime. The deterministic Batch 9/9.1 matrix exercises the real
`PartyModeWindow` against a synthetic host/player/DB contract, including all
presets, lyric states, transitions, overlay/help/queue stacking, and
single-player ownership. Optional retained captures are allowed only in an
ignored review directory or outside the repository and must be deleted after
review.

## Known architectural debt

The current architecture is functional and does not require a wholesale
rewrite. Known areas for incremental improvement are:

- `music_vault/app.py` has broad responsibilities and is large;
- broader audio-format writeback beyond the currently audited MP3 path remains
  future work;
- private-playlist OAuth, automatic startup synchronization, and parallel
  source downloads are intentionally unsupported;
- there is no portable manifest for selective mobile transfer; and
- there is no station, program-timeline, or mixed audio-segment model for the
  future personal-radio branch.

Future extraction should preserve working playback, queue, synchronization,
path-resolution, and persistence behavior while introducing narrower module
boundaries as needed.
