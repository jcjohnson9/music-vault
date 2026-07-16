# Music Vault Roadmap

Music Vault is a standalone, local-first personal music system. It is a
personal-only product with no monetization plan. It has no Watchtower runtime
dependency and no planned Watchtower integration. Android is a separate future
application, Prime interoperability is optional, and personal radio is a major
future branch rather than a V1 requirement.

The roadmap uses implementation batches. A batch is complete only when its own
acceptance checks pass; a label does not imply that later batches have begun.

**Latest public product status:** Music Vault v1.0.0 Stable.

**Current main development target:** Music Vault v1.1.0 Development. No public
v1.1.0 release has been created.

## Batch 1 — GitHub and Source-Control Safety Baseline

**Objective:** Establish a safe, professional, source-only Git baseline for
Music Vault v1.0.0.

**Scope:** Harden ignore and text-handling rules, quarantine local-only hazards,
add public project documentation, add a read-only publication scanner, add
Windows CI verification, initialize `main`, and create one reviewed baseline
commit.

**Non-goals:** No app behavior, schema, runtime-path, packaging, metadata,
playback, sync, or UI changes; no EXE upload; no release tag.

**Status:** Complete and Published.

## Batch 2 — V1 Trust, Sync Correctness, and Safety

**Objective:** Make the existing playlist-to-vault workflow truthful and safe
enough for a stable V1 claim.

**Scope:** Correct upload-date versus release-date handling, report partial sync
failures accurately, make failed-item recovery actionable, harden archive and
output-folder reconciliation, sanitize external errors and output directory
names, and retain proof of a clean incremental-sync acceptance test.

**Non-goals:** No private-playlist OAuth, multiple-source system, broad metadata
editor, mobile application, Prime interface, radio system, or app-wide rewrite.

**Status:** Complete.

## Batch 3 — Playback State and Now-Playing Accuracy

**Objective:** Make playback state, transport feedback, and now-playing status
accurate across normal and error paths.

**Scope:** Handle media errors visibly, verify transport state transitions,
keep exported status synchronized, and decide the narrow persistence policy for
safe playback preferences and resume state.

**Non-goals:** No replacement playback engine and no expansion of the manual
queue into a new queue product.

**Status:** Complete.

## Batch 4 — Premium UI System Overhaul

**Objective:** Give Music Vault a cohesive, polished desktop interface without
changing its established product behavior.

**Scope:** Refine visual hierarchy, responsive layouts, first-run guidance,
accessibility, progress/error presentation, reusable UI components, and
sanitized demo presentation.

**Non-goals:** No sync, metadata, schema, playlist, or playback-semantic changes.
A richer editable queue interface is later work and must preserve the working
FIFO queue and base-context resume behavior.

**Status:** Complete.

## Batch 5 — Album/Artist Performance and Artist Identity

**Objective:** Keep album and artist browsing accurate and responsive as the
library grows.

**Scope:** Aggregate album and artist summaries in SQLite, use stable
album/artist browser identities, replace eager card widgets with a delegate
model/view grid, cache unchanged summaries and visible thumbnails, load work in
the background, and separate optional artist photos from album artwork through
an explicit privacy-aware opt-in and runtime-only cache.

**Non-goals:** No automatic bulk metadata rewrite and no speculative database
replacement.

**Status:** Complete.

## Batch 6 — Metadata Foundation and Manual Correction

**Objective:** Make metadata correctable, attributable, and durable.

**Scope:** Add schema-v3 source observations, effective field provenance and
confidence, canonical release dates, manual/confirmed locks, precedence-aware
imports, grouped metadata history and undo, manual field/artwork correction,
and explicit MusicBrainz/Cover Art Archive candidate review. Corrections remain
inside the Music Vault library; audio-file tag writeback is deferred.

**Non-goals:** No automatic rewrite of the existing personal library, bulk
provider search, audio-file tag mutation, or AcoustID requirement.

**Status:** Complete.

## Batch 7 — Existing Library Metadata Remediation

**Objective:** Safely repair existing library metadata using the Batch 6
foundation.

**Scope:** Back up first, produce a private remediation report, review proposed
changes, apply only strict high-confidence corrections incrementally, write
supported MP3 tags through verified full-file backups without changing audio,
and provide resumable analysis, manual review, verification, and rollback.

**Non-goals:** No blind bulk replacement, no publication of personal reports,
and no unaudited modification of downloaded audio.

**Status:** Complete. Automated, packaged-sandbox, controlled-live dry-run and
strict high-confidence apply, backup, verification, publication-safety, and
delivery gates passed. Uncertain and unresolved tracks remain unchanged for
later review.

## Batch 8 — Clean Blank Distribution and Public V1 Release

**Objective:** Publish a reproducible, empty-by-default V1 package after the V1
trust and presentation gates pass.

**Scope:** Centralize stable version metadata, define an explicit portable-root
marker, bootstrap empty private runtime data, add local-only first-run setup and
bounded FFmpeg discovery, produce reproducible release/compliance artifacts,
verify clean extracted startup, and publish the approved source and portable
release.

**Non-goals:** No personal database, key, media, artwork cache, reports, status
file, or playlist data in the distribution.

**Status:** Complete. Music Vault v1.0.0 is the stable, blank-by-default Windows
portable release. The repository's own source remains MIT; the combined
portable distribution carries GPL-3.0-or-later and the preserved terms of its
separately licensed third-party components. Batch 8.1 adds corrective
publication without retagging: application artifacts remain tied to the
immutable v1.0.0 tag, while the manifest separately identifies the later
release-tooling commit that performs hardened corresponding-source validation
and complete-history publication scanning.

## Batch 9 — Full-Screen Party Mode

**Objective:** Add an optional, premium full-screen audio-reactive playback
experience without creating a second playback pipeline.

**Scope:** Artwork-led Pulse, Starfield, and Aurora visuals; transient bounded
decoded-audio analysis with an ambient fallback; readable now-playing and
transport controls; keyboard operation; multi-monitor presentation; reduced
motion; adaptive quality; and synthetic visual/performance review tooling.

**Non-goals:** No radio scheduling, mobile mirroring, or change to queue
semantics.

**Status:** Complete on the v1.1.0 development line. The public stable release
remains v1.0.0.

## Batch 9.1 — Party Mode Motion Refinement and Premium Lyrics

**Objective:** Refine Party Mode into slower phrase-driven motion and add an
optional premium, local-first lyrics overlay without changing playback or the
approved album/metadata/control layout.

**Scope:** Static as the migrated default; smooth beat, four-beat bar, and
32-beat phrase timing; fixed artwork outside Pulse; refined Starfield/Aurora;
bounded 3D Orb Cluster and Fireworks; synchronized and honestly unsynchronized
lyrics directly above the playback bar; manual/sidecar/embedded/cache discovery;
consent-gated read-only LRCLIB lookup; strict matching; private hashed cache;
and publication/release safety gates for lyric content.

**Non-goals:** No lyrics editor, bulk lyric download, provider contribution,
word-level karaoke, audio-tag writeback, additional provider, queue/playback
change, database-schema change, or public v1.1.0 release.

**Status:** Complete on the v1.1.0 development line. Stable remains v1.0.0;
Batch 10 follows on the same development line.

## Batch 10 — Multiple Source Playlists

**Objective:** Support several authorized source playlists without losing
identity or membership information.

**Scope:** Persist source definitions, source-item occurrences, per-source run
and failure state, cross-source video identity, stable source folders,
origin-aware managed local playlists, sequential Sync Selected/Sync All,
Stop After Current, complete-snapshot removal reconciliation, and a premium
multi-source Sync Center.

**Non-goals:** No private-playlist OAuth requirement and no conversion of Music
Vault into a hosted service.

**Status:** Complete on the v1.1.0 development line. Stable remains v1.0.0;
Batch 11 remains next.

## Batch 11 — Highest-Practical-Quality / Best Original

**Objective:** Prefer the best useful source representation without misleading
quality claims or wasteful transcoding.

**Scope:** Define an honest quality policy, evaluate original Opus/M4A retention,
retain a compatibility option, expose clear choices, and verify playback and
metadata behavior for supported formats.

**Non-goals:** No promise to create fidelity absent from the source and no
unbounded file-size growth.

**Status:** Next on the v1.1.0 development roadmap.

## Batch 12 — Selective Library and Playlist Mobile Export

**Objective:** Transfer all or selected Music Vault content to a mobile device
through a portable, inspectable export.

**Scope:** Define stable portable IDs, a versioned manifest, relative media and
artwork references, playlist order, whole-library and selected-playlist export,
and incremental reconciliation.

**Non-goals:** No Android application in this batch and no cloud account system.

**Status:** Planned post-V1.

## Batch 13 — Independent Android Music Vault Foundation

**Objective:** Establish a separate Android Music Vault application that can use
the portable contracts from Batch 12.

**Scope:** Android-local storage, library browsing, playlist playback, artwork,
and an independently maintainable synchronization boundary.

**Non-goals:** No embedding of PySide6 code and no requirement that the desktop
application be running for ordinary mobile playback.

**Status:** Planned post-V1.

## Batch 14 — Personal Radio Data Model and Program Timeline

**Objective:** Build a deterministic foundation for personal stations and mixed
audio programs.

**Scope:** Versioned stations, ordered music and non-music segments, persisted
durations and start times, deterministic programming, cached assets, and clear
separation from the ordinary music library.

**Non-goals:** No generated host voices, scripts, commercials, or external TTS
provider integration yet.

**Status:** Planned major future branch.

## Batch 15 — Fictional Hosts, Scripts, TTS, Station IDs, Mock Commercials, and Talk Stations

**Objective:** Add original fictional radio presentation on top of the Batch 14
timeline.

**Scope:** Provider-neutral script and TTS boundaries, fictional personas,
station IDs, jingles, mock commercials, talk-only programs, asset caching,
provenance, cost limits, failure behavior, and offline replay.

**Non-goals:** No imitation of real people and no dependency on a single LLM or
voice provider.

**Status:** Planned major future branch.

## Batch 16 — Optional Neutral Prime Interface

**Objective:** Allow an optional external assistant to discover and control
Music Vault through a small neutral local contract.

**Scope:** Versioned status, library search, playback commands, explicit local
permissions, authentication boundaries, and failure isolation while Music Vault
remains authoritative.

**Non-goals:** Prime is not required for Music Vault, Music Vault is not a Prime
module, and this batch creates no Watchtower role or dependency.

**Status:** Optional future work.
