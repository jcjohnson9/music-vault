# Changelog

Notable changes to Music Vault are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Added persistent saved YouTube playlist sources with Library Only and Managed
  Local Playlist destinations, deterministic enable/order controls, sequential
  Sync Selected and Sync All execution, Stop After Current, per-source run and
  failure history, and a premium multi-source Sync Center.
- Added schema-v5 source definitions, durable playlist-item occurrences,
  cross-source video-to-track identity, non-destructive identity-conflict
  diagnostics, origin-aware playlist materialization, and safe source
  detachment/archive behavior.

- Added full-screen Party Mode with artwork-led Pulse, Starfield, and Aurora
  presets, a readable auto-hiding control overlay, keyboard controls, reduced
  motion, and adaptive quality.
- Added bounded transient PCM analysis for audio-reactive energy, spectral
  bands, and beats when supported by the active Qt backend, with a calm ambient
  fallback when decoded buffers are unavailable.
- Added original Party Mode icons and a synthetic, offscreen, network-disabled
  review and frame-benchmark tool.
- Added Static, Orb Cluster, and Fireworks Party presets, a phrase-aware beat
  clock, and centralized album-transform rules that keep the artwork fixed
  outside the restrained four-beat Pulse mode.
- Added optional premium synchronized/plain lyrics with local/manual,
  sidecar, embedded, and private-cache discovery; consent-gated read-only
  LRCLIB lookup; strict matching; negative caching; provider attribution; and
  an original Lyrics icon.

### Changed

- Advanced the current source tree to `1.1.0` on the `development` channel.
  The latest public stable release and immutable release tag remain `v1.0.0`.
- Made Static the one-time migrated Party default, routed long-lived animation
  through smooth beat/bar/phrase timing, and refined Starfield, Aurora, Orb
  Cluster, Fireworks, and Pulse for bounded, comfortable motion.
- Replaced the one-playlist-at-a-time synchronization screen with persistent
  multiple-source management. Complete source snapshots may reconcile remote
  removals; failed or partial enumeration preserves the last known-good
  membership and local playlist order.

### Security and privacy

- Kept saved source URLs, labels, remote titles, playlist/item identities,
  membership snapshots, local source folders, and per-item failures inside
  private runtime data. App Status receives aggregate source/batch values only.
- Preserved one canonical local track/media identity across overlapping
  sources, and made source removal, remote removal, and destination changes
  explicitly non-destructive to media, metadata, artwork, lyrics, and history.

- Kept lyrics Off and online lookup Off by default. Provider lookup requires
  consent and sends only the current title, artist, optional album, and
  duration; it sends no API key, audio, playlist, or bulk library inventory.
- Added fail-closed Git/history/publication and portable/source-compliance
  checks for private lyric caches, `.lrc`/`.lyrics`/lyric-text payloads, and
  provider fixtures. Lyric text is never written to audio files, App Status,
  or public logs.

### Fixed

- Added a corrective publication path for the existing immutable `v1.0.0`
  application tag without changing the application or retagging its source.
  Corrective release tooling now records the tagged application commit and the
  later tooling commit as separate provenance identities.
- Pinned zlib 1.3.1 corresponding source to the official versioned fossil
  archive and added fail-closed hash, response, archive-safety, layout,
  license, and internal-version validation. Verified offline-cache bytes pass
  the same checks as network downloads.
- Added an exact release-payload transfer index and a complete reachable-Git-
  history publication scanner for the public-release gate.

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
