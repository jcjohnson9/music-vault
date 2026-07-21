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
Batch 10.1 follows as a corrective metadata patch on the same line.

## Batch 10.1 — Discogs-First Automatic Metadata Intelligence

**Objective:** Correct incomplete automatic metadata without weakening manual
locks, source identity, version preservation, or safe file-writeback rules.

**Scope:** Schema-v6 artist entities and ordered credits; release/label and
original-versus-version context; personal-token, consent-gated Discogs primary
authority; MusicBrainz secondary corroboration/fallback; YouTube-title parsing
and uploader classification; field-level confidence/review; post-import and
resumable existing-library jobs; gap-only private Discogs artwork; and verified
high-confidence text-tag writeback.

**Non-goals:** No blind bulk rewrite, label/uploader-as-artist promotion,
track/version merge, source-membership change, automatic artwork replacement or
Discogs image embedding, YouTube sync change, public v1.1.0 release, or Batch 11
quality implementation.

**Status:** Complete on the v1.1.0 development line after schema, provider,
privacy, automated regression, packaged synthetic, controlled migration, and
delivery gates. Stable remains v1.0.0.

## Batch 10.2 — Schema-6 Preservation Correction

**Objective:** Preserve source-identity timestamps and prove the schema-5 to
schema-6 transition without restoring or rewriting the live library.

**Scope:** Make identical source mappings true no-ops, retain timestamp changes
for genuine canonical remaps, correct field/artist preservation verification,
and provide disposable migration proof plus a narrow timestamp-only live
repair with verified rollback backups.

**Non-goals:** No provider lookup, metadata remediation, media/tag change,
source-membership change, sync change, or public release.

**Status:** Complete on the v1.1.0 development line. Stable remains v1.0.0.

## Batch 10.3 — Canonical Media Browser and Review Tuning

**Objective:** Present one safe canonical album/artist identity, preserve
edition and credit roles, reduce manual review to critical uncertainty, and
make Spacebar Play/Pause work safely across ordinary application pages.

**Scope:** Schema-v7 canonical albums/memberships, artist aliases and verified
relationships, transactional safe consolidation, version-as-artist repair,
canonical Albums/Artists queries, private portrait fallback, **Tracks**,
**Featured On**, **Collaborations**, and **Group Appearances**, field-level
review outcomes, soundtrack policy, stored-evidence reclassification, and
guarded Spacebar control.

**Non-goals:** No track/media merge or deletion, artwork standardization,
speculative artist/group relationship, provider request during migration,
YouTube sync change, quality policy, tag rewrite, tag/release, or Batch 11 work.

**Status:** Complete on the v1.1.0 development line after schema-v7 migration,
consolidation, preservation, packaged UI, visual/performance, regression,
publication-safety, and Batch 10.4 quiescence acceptance gates. Stable remains
v1.0.0; Batch 10.4 followed on the same development line.

## Batch 10.4 — Migration-Startup Quiescence and Post-Hoc Acceptance Recovery

**Objective:** Keep database migration and optional external provider work in
separate startup phases, then accept the retained schema-v7 library and private
artist-photo cache through aggregate-only preservation checks.

**Scope:** Process-local migration state, centralized no-secret/no-network and
migration-startup policy, lazy provider construction, deferred background
provider work, read-only cache validation, packaged two-launch proof, and one
controlled quiescent live startup with a verified schema-v7 rollback backup.

**Non-goals:** No repeat migration or restore, provider lookup during controlled
acceptance, cache deletion, sync, remediation, playback change, media/tag write,
new tag, or public release.

**Status:** Complete on the v1.1.0 development line. Stable remains v1.0.0.

## Batch 10.5 — Metadata Acceptance and Artist Identity Correction

**Objective:** Finish the retained canonical-media batch by accepting the best
honest stored metadata automatically and repairing artist/portrait identity
without touching personal media.

**Scope:** Active-interpreter CI portability; cache-preserving
MusicBrainz/Wikimedia-first portrait selection; full-size Discogs fallback;
canonical cross-provider artist clusters and role-aware detail unions;
provider-adjudicated dash-title orientation; best-available field outcomes;
soundtrack/album application; one virtual **Singles & Uncatalogued**
collection; and one idempotent, offline schema-v7 live repair.

**Non-goals:** No AI provider, schema 8, provider request during repair, media
or tag rewrite, source/playlist change, sync change, public tag, or Release.

**Status:** Complete on the v1.1.0 development line after offline repair,
preservation, packaged/UI/performance, regression, build, and delivery gates.
Stable remains v1.0.0; Batch 10.6 followed on the same development line.

## Batch 10.6 — Dual-Orientation Title Resolution and Final Metadata Acceptance

**Objective:** Resolve ambiguous dash-separated source titles deterministically
and close the final metadata-acceptance blocker without broad library work.

**Scope:** Immutable left/right title-orientation hypotheses; safe top-level
dash parsing; adaptive maximum-two Discogs adjudication; one secondary
MusicBrainz lookup; normalized orientation evidence; strict offline canonical-
artist selection; accepted source-fallback eligibility; and one explicit,
backup-protected, exact-target schema-v7 repair with no media/tag/artwork write.

**Non-goals:** No AI provider, schema change, full-library scan, YouTube sync,
media/tag write, artwork/portrait refresh, playlist/source/playback change,
public tag, or Release.

**Status:** Complete on the v1.1.0 development line after targeted/private,
packaged, regression, build, CI, and delivery gates. Stable remains v1.0.0;
Batch 11 followed on the same development line.

## Batch 11 — Highest-Practical-Quality / Best Original

**Objective:** Prefer the best useful source representation without misleading
quality claims or wasteful transcoding.

**Scope:** Make Best Original the default for future missing-track downloads,
retain supported source codecs through direct storage or container-only remux,
offer an explicit MP3 320 compatibility transcode, persist schema-v8 quality
provenance, support per-source overrides, preserve one-file cross-source
identity, and verify native-format import/playback without changing existing
media.

**Non-goals:** No promise to create fidelity absent from the source, no
FLAC/WAV conversion theater, no Hi-Res claim, no automatic library upgrade,
and no broad native-container tag writeback.

**Status:** Complete on the v1.1.0 development line after the ordinary focused
and complete regression, official build, essential E2E, controlled live
migration, branch/main CI, merge, and cleanup gates. Batches 1 through 11 are
Complete; stable remains v1.0.0 and development remains v1.1.0. Remaining
metadata-polish items are deferred and are not Batch 11 blockers. Batch 12 is
Next.

## Batch 12 — Selective Library and Playlist Mobile Export

**Objective:** Transfer all or selected Music Vault content to a mobile device
through a portable, inspectable export.

**Scope:** Define stable portable IDs, a versioned manifest, relative media and
artwork references, playlist order, whole-library and selected-playlist export,
and incremental reconciliation.

**Non-goals:** No Android application in this batch and no cloud account system.

**Status:** Next on the v1.1.0 development roadmap.

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
