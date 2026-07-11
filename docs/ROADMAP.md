# Music Vault Roadmap

Music Vault is a standalone, local-first personal music system. It is a
personal-only product with no monetization plan. It has no Watchtower runtime
dependency and no planned Watchtower integration. Android is a separate future
application, Prime interoperability is optional, and personal radio is a major
future branch rather than a V1 requirement.

The roadmap uses implementation batches. A batch is complete only when its own
acceptance checks pass; a label does not imply that later batches have begun.

**Current public product status:** Music Vault v1.0.0 Release Candidate.

## Batch 1 — GitHub and Source-Control Safety Baseline

**Objective:** Establish a safe, professional, source-only Git baseline for the
v1.0.0 Release Candidate.

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

**Status:** Next.

## Batch 4 — Premium UI System Overhaul

**Objective:** Give Music Vault a cohesive, polished desktop interface without
changing its established product behavior.

**Scope:** Refine visual hierarchy, responsive layouts, first-run guidance,
accessibility, progress/error presentation, reusable UI components, and
sanitized demo presentation.

**Non-goals:** No sync, metadata, schema, playlist, or playback-semantic changes.
A richer editable queue interface is later work and must preserve the working
FIFO queue and base-context resume behavior.

**Status:** Planned.

## Batch 5 — Album/Artist Performance and Artist Identity

**Objective:** Keep album and artist browsing accurate and responsive as the
library grows.

**Scope:** Define stable artist/album grouping rules, remove title-only grouping
ambiguities, measure current rendering/query costs, and optimize the proven
bottlenecks.

**Non-goals:** No automatic bulk metadata rewrite and no speculative database
replacement.

**Status:** Planned.

## Batch 6 — Metadata Foundation and Manual Correction

**Objective:** Make metadata correctable, attributable, and durable.

**Scope:** Add versioned metadata provenance, confidence, canonical-versus-source
values, non-destructive candidate review, manual field and artwork correction,
and protection for user overrides.

**Non-goals:** No automatic rewrite of the existing personal library and no
AcoustID requirement unless separately approved.

**Status:** Planned.

## Batch 7 — Existing Library Metadata Remediation

**Objective:** Safely repair existing library metadata using the Batch 6
foundation.

**Scope:** Back up first, produce a private remediation report, review proposed
changes, apply approved corrections incrementally, and verify that later imports
do not undo them.

**Non-goals:** No blind bulk replacement, no publication of personal reports,
and no modification of downloaded audio without explicit approval.

**Status:** Planned.

## Batch 8 — Clean Blank Distribution and Public V1 Release

**Objective:** Publish a reproducible, empty-by-default V1 package after the V1
trust and presentation gates pass.

**Scope:** Build from a reviewed source commit, bootstrap empty runtime data,
verify a clean-machine workflow, finalize version metadata and release notes,
and publish approved source/release artifacts.

**Non-goals:** No personal database, key, media, artwork cache, reports, status
file, or playlist data in the distribution.

**Status:** Planned; V1 Stable is gated here.

## Batch 9 — Full-Screen Party Mode

**Objective:** Add an optional, readable full-screen playback experience.

**Scope:** Large now-playing artwork and metadata, simple transport visibility,
keyboard escape behavior, and display-aware presentation.

**Non-goals:** No radio scheduling, mobile mirroring, or change to queue
semantics.

**Status:** Planned post-V1.

## Batch 10 — Multiple Source Playlists

**Objective:** Support several authorized source playlists without losing
identity or membership information.

**Scope:** Persist source definitions, source-item membership, per-source sync
state and errors, cross-source video identity, and deterministic local mapping.

**Non-goals:** No private-playlist OAuth requirement and no conversion of Music
Vault into a hosted service.

**Status:** Planned post-V1.

## Batch 11 — Highest-Practical-Quality / Best Original

**Objective:** Prefer the best useful source representation without misleading
quality claims or wasteful transcoding.

**Scope:** Define an honest quality policy, evaluate original Opus/M4A retention,
retain a compatibility option, expose clear choices, and verify playback and
metadata behavior for supported formats.

**Non-goals:** No promise to create fidelity absent from the source and no
unbounded file-size growth.

**Status:** Planned post-V1.

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
