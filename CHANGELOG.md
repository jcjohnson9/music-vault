# Changelog

Notable changes to Music Vault will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- Added additive SQLite schema version 2 with an automatic database backup
  before migration.
- Separated YouTube source upload dates from canonical release years and safely
  cleared unverified YouTube-derived canonical years.
- Added one typed sync result shared by the engine, UI, log, and App Status,
  with truthful `complete`, `complete_with_issues`, and `failed` outcomes.
- Added structured failed-item tracking, retry, resolution, and legacy-file
  compatibility import.
- Made valid database/file identity authoritative over stale archive history.
- Standardized anonymous public/unlisted extraction and removed silent Firefox,
  Chrome, and Edge cookie probing.
- Added centralized secret redaction and Windows-safe playlist output paths.
- Changed Downloaded identity from folder-name matching to `source_kind`.
- Added visible non-blocking playback errors while preserving queue ordering.
- Standardized user-facing status terminology as neutral App Status while
  retaining the compatibility import shim and existing status filename/schema.
- Added explicit confirmation and confidence display before MusicBrainz changes.
- Added synthetic pytest coverage for migrations, sync truth, safety, and identity.
- Persisted normalized 0–100 volume settings with immediate audio updates,
  debounced config writes, and close-time flushing.
- Separated authoritative now-playing track identity from ordinary table
  selection and restored the active-row treatment through track-ID-based view
  rebuilds.
- Kept the active row synchronized across Auto, Shuffle, manual Queue, queue
  return, Next, Previous, Repeat All, and playback-error continuation while
  preserving FIFO queue and base-context behavior.
- Added centralized design tokens, reusable UI components, and a static premium
  dark theme with transparent label backgrounds and consistent focus states.
- Added 34 original, cached, high-DPI SVG interface icons plus a guarded native
  Windows dark-title-bar treatment.
- Refined the sidebar, summary cards, search field, library table, album and
  artist cards, Sync Center, Settings, menus, tooltips, and custom scrollbars.
- Reworked the Library header into primary actions plus an accessible overflow
  menu without removing any existing action.
- Rebalanced the player bar around precisely centered icon transport controls,
  premium timeline/volume controls, and compact mode and queue status.
- Added responsive layouts, keyboard/accessibility polish, empty states, and a
  reusable isolated synthetic UI review harness for three desktop sizes.
- Replaced eager Album and Artist card widget trees with a delegate-painted,
  responsive model/view grid that preserves keyboard activation and filtering.
- Added SQL-aggregated album and artist summaries, stable identity keys,
  exact key-based track lookup, revision-aware summary caching, centralized
  invalidation, and read-only background summary loading.
- Added a bounded, coalescing thumbnail cache that decodes local images away
  from the GUI thread and requests only visible or near-visible artwork.
- Separated artist identity from album artwork. Artist cards now use a
  dedicated original unknown-artist placeholder unless a credible cached
  artist photo is available.
- Added optional artist-photo lookup behind an explicit, disabled-by-default
  setting, using a no-key MusicBrainz and Wikimedia provider path with strict
  match confidence, HTTPS destination validation, local provenance, negative
  caching, and safe cache-clear controls.
- Added a synthetic media-browser profiler for 300, 1,000, and 5,000 tracks;
  representative post-change queries remained below 30 ms, revisits below
  0.01 ms, and eager per-card QWidget creation remained zero.

## 1.0.0-rc.1 - Unreleased RC baseline

### Established

- Local SQLite music library and Windows playback
- Authorized public or unlisted source-playlist synchronization
- Full YouTube Data API pagination for large playlists
- Incremental acquisition based on stable video IDs
- yt-dlp and FFmpeg media acquisition workflow
- Embedded and downloaded artwork support
- Albums, artists, and custom local playlists
- Temporary FIFO queue with original base-context resume
- Local settings and configuration
- Hardened source and packaged-application data paths
- PyInstaller one-folder Windows EXE workflow
- Developer verification, build, launch, and shortcut tooling

### Known release-candidate limitations at baseline

- A YouTube source upload date may appear as a track's release year.
- Partial synchronization failures may be reported inaccurately.
- Manual metadata correction is not yet complete.
- A clean, blank public distribution has not yet been published.
- The interface still requires its planned premium overhaul.
