# Changelog

Notable changes to Music Vault are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

No changes yet.

## 1.0.0 - 2026-07-13

### Added

- Established a safe public source-control baseline, Windows verification,
  publication scanning, and a reproducible Python 3.11/PyInstaller workflow.
- Added truthful public/unlisted YouTube playlist synchronization with complete
  pagination, stable source identity, structured failure history, retry, safe
  output paths, and sanitized results.
- Added accurate now-playing state, visible playback errors, persisted volume,
  and active-row tracking while preserving the FIFO queue and base-context
  resume behavior.
- Introduced the premium scalable desktop UI, accessible original icon system,
  responsive layouts, and synthetic-only visual review tooling.
- Replaced eager album/artist card trees with fast SQL-backed model/view grids,
  bounded thumbnail caching, exact album identity, and optional privacy-aware
  artist photos.
- Added schema-v3 metadata provenance, protected manual/confirmed fields,
  history/undo, trusted manual correction, and explicit MusicBrainz/Cover Art
  Archive candidate review.
- Added schema-v4 resumable existing-library remediation with non-destructive
  analysis, strict high-confidence apply, private reports, verified MP3 tag
  backups/writeback, unchanged-audio proof, verification, and rollback.
- Added blank-runtime first-run onboarding, optional local-only setup, portable
  data selection, centralized FFmpeg/ffprobe discovery, and non-admin desktop
  shortcut support.

### Distribution

- Centralized product version `1.0.0` and added matching Windows executable
  version metadata.
- Added the explicit `music-vault.portable.json` root contract so an extracted
  package works without the source repository or a special working directory.
- Added exact release dependencies, deterministic portable/source-compliance
  builders, fail-closed package verification, checksums, release manifests, and
  a tag-driven GitHub Release workflow.
- Published an empty-by-default Windows x64 portable layout containing no user
  database, credentials, configuration, media, artwork, reports, or backups.
- Recorded complete third-party notices and source/relinking availability. The
  repository's own source remains MIT; the combined portable distribution is
  GPL-3.0-or-later with separately licensed components retaining their terms.

### Security and privacy

- Kept API keys in the existing local secret file and out of configuration,
  App Status, logs, manifests, and release artifacts.
- Standardized anonymous extraction without silent browser-cookie access and
  bounded provider networking with safe URL, response, and error handling.
- Kept artist images, remediation state, provider caches, reports, screenshots,
  original-media backups, and all personal runtime data private and untracked.
