# Developer tools

Run these helpers from PowerShell at the repository root. Each wrapper resolves
the root and uses `.venv\Scripts\python.exe`; generated output and private
runtime data are not source artifacts.

Batch 11 quality behavior and boundaries are documented in
[`docs/AUDIO_QUALITY.md`](../../docs/AUDIO_QUALITY.md). Its ordinary-batch
quality gates use disposable generated audio, fake downloader/provider facts,
no browser cookies, no secrets, no public network, and no personal media. A
controlled live schema-7-to-8 migration is a separate explicitly authorized
gate and must run with no-secret/no-network controls; this documentation does
not assert that gate has passed.

| Command | Purpose |
| --- | --- |
| `.\tools\dev\verify.ps1` | Parse/import active source and validate required project resources. |
| `.\tools\dev\run_source.ps1` | Run the checked-out development source. |
| `.\tools\dev\build_exe.ps1` | Rebuild the official one-folder development EXE. |
| `.\tools\dev\capture_ui_review.ps1` | Run the explicitly gated synthetic application review matrix. |
| `.\tools\dev\run_batch10_1_review.ps1 --offscreen` | Capture the bounded ten-state Discogs metadata-intelligence review with in-memory synthetic data and networking blocked. |
| `.\tools\dev\run_batch10_3_review.ps1` | Capture ten sanitized states from the production album/artist browser, detail pages, and metadata-review dialog over a disposable synthetic database; networking is blocked and captures are deleted by default. |
| `.\tools\dev\profile_media_browsers.ps1` | Profile schema-v7 canonical browsers and review reclassification at 300/1,000/5,000 synthetic tracks. |
| `.\tools\dev\run_batch10_4_packaged_quiescence_smoke.ps1` | Exercise packaged schema-6-to-7 migration quiescence and a second current-schema launch with synthetic providers in disposable TEMP roots. |
| `.\tools\dev\audit_batch10_4_artist_cache.ps1` | Validate the accepted private artist-image cache read-only and emit aggregate counts only. |
| `.\tools\dev\run_batch10_4_controlled_live_startup.ps1 -AcknowledgeLiveLibrary batch10.4-live-schema7-quiescence` | Run the explicitly authorized, one-launch live schema-v7 quiescence gate with no-secret/no-network controls and preservation verification. |
| `.\tools\dev\run_party_mode_review.ps1` | Review PartyCanvas and real PartyModeWindow states plus bounded frame performance with temporary synthetic audio/artwork. |
| `.\tools\dev\run_party_mode_9_1_review.ps1` | Review the 22-state Batch 9.1 motion/lyrics matrix and performance using temporary synthetic data with networking blocked. |
| `.\tools\dev\pre_public_commit_check.ps1` | Scan the current publication candidate for private/runtime content. |
| `.\tools\dev\pre_public_history_check.ps1` | Scan all reachable Git history and refs before publication. |

## Party Mode review

The Party Mode harness defaults to Qt's offscreen platform and uses no personal
library, API key, or network. It creates a temporary runtime outside the
repository, generates a short standard-library WAV and original synthetic
artwork, exercises a focused state matrix, benchmarks bounded 1080p and 4K
rendering, closes every window/canvas, and removes the runtime. The full profile
captures 14 states: 10 focused canvas states and four real `PartyModeWindow`
states for visible/hidden overlays, the queue count, and shortcut help. Those
window states assert content and stacking order and prove the Party window did
not create a second `QMediaPlayer`.

```powershell
.\tools\dev\run_party_mode_review.ps1 --capture-profile full --scale 1.0
.\tools\dev\run_party_mode_review.ps1 --capture-profile scale-smoke --scale 1.25
.\tools\dev\run_party_mode_review.ps1 --capture-profile scale-smoke --scale 1.5
```

`scale-smoke` captures exactly two representative real-window states. The
standard final evidence set is therefore 14 captures at 100% plus two at 125%
and two at 150%. Benchmarks and cleanup gates run in both profiles.

Captures are temporary by default. For a focused human review, retain copies
only in the ignored `.ui-review` directory or outside the repository:

```powershell
.\tools\dev\run_party_mode_review.ps1 `
  --output .\.ui-review\party-mode-125 `
  --capture-profile scale-smoke `
  --scale 1.25
```

Delete retained captures after recording findings. Never commit a generated
WAV, image capture, benchmark JSON, temporary database, or runtime directory.
The harness reports timing as informational; structural failures such as an
unbounded particle count, continuing hidden timer, or failed cleanup are gates.

## Batch 9.1 motion and lyrics review

The Batch 9.1 helper exercises the exact six-preset motion sequence and 22
sanitized motion/lyrics states at 720p, 1080p, and 1440p. It also checks the
album-motion invariant, compact lyric/control geometry, Static's stopped render
timer, bounded peak Fireworks particles, Aurora release smoothing, timeline and
private-cache lookup costs, and temporary-runtime cleanup. Python network calls
are blocked for the entire helper process.

```powershell
.\tools\dev\run_party_mode_9_1_review.ps1 -Scale 1.25
.\tools\dev\run_party_mode_9_1_review.ps1 -Scale 1.5
```

Pass `-Output .\.ui-review\batch9-1` only for a temporary human review. Delete
that ignored output after recording the findings.

## Batch 10.3 canonical-browser review and profile

The Batch 10.3 review renders the actual `MusicVaultWindow` album/artist grid
and detail pages plus the actual metadata-intelligence dialog over fictional
rows in a disposable current-schema database. Its ten states cover canonical
album grouping and retained editions, **Tracks**, **Featured On**,
**Collaborations**, **Group Appearances**, all three review outcomes, soundtrack
separation, version-as-artist repair, and missing-portrait fallback. It uses
1280×720 and 1920×1080 plus one 150% scale state, blocks Python network entry
points and live runtime/credential access, checks private markers and semantic
UI evidence, and deletes both its temporary captures and runtime by default:

```powershell
.\tools\dev\run_batch10_3_review.ps1
```

For a focused human review, retain the sanitized images only below the ignored
`.ui-review/` directory and delete them after recording findings:

```powershell
.\tools\dev\run_batch10_3_review.ps1 `
  --output .\.ui-review\batch10-3 `
  --keep-captures
```

The media-browser profiler seeds schema-v7 canonical albums/memberships,
canonical artists, aliases, featured/collaborator credits, verified group
relationships, several editions, and a reclassifiable intelligence job. It
reports aggregate query/model/render/cache/reclassification timings, constant
SQL statement counts, indexed membership lookup, zero eager card QWidgets, and
visible-only thumbnail work at 300, 1,000, and 5,000 tracks. All databases,
generated artwork, and media sentinels remain temporary; timing variance is
informational while structural checks are gates:

```powershell
.\tools\dev\profile_media_browsers.ps1
```

Pass `--json` only for a local sanitized aggregate report, and never commit the
result.

## Batch 10.4 migration-startup quiescence

The packaged smoke creates a disposable synthetic schema-6 runtime outside the
repository and launches the official EXE twice. The first launch enables
acceptance no-secret/no-network controls, migrates to schema 7, verifies safe
App Status and zero provider/network activity, and closes gracefully. The
second launch removes those acceptance controls, stays on schema 7, and uses
only the synthetic review provider to prove that migration deferral is
process-local and eligible work can resume without real networking. Successful
runs delete both temporary roots; failed evidence is retained under TEMP for
diagnosis.

```powershell
.\tools\dev\run_batch10_4_packaged_quiescence_smoke.ps1
```

The artist-cache audit parses the private index strictly, contains every path
under the cache root, validates content-addressed images, MIME/format,
dimensions, provider labels, safe provenance URLs, and absence of partial or
unexpected payloads. It prints aggregates only and never deletes or repairs a
cache entry:

```powershell
.\tools\dev\audit_batch10_4_artist_cache.ps1
```

The live-startup wrapper is intentionally separate and fail-closed. Use it only
after explicit authorization for the named live schema-7 acceptance gate. It
captures aggregate logical/file baselines, reads only credential file
size/timestamp metadata, validates the cache, creates one fresh verified SQLite
backup, launches the official EXE once under no-secret/no-network controls,
requests a graceful close, and verifies database, media, artwork, cache,
configuration, credential metadata, provider, and App Status preservation.
The pre-launch backup is a separate authorized preparation write; during the
controlled app launch, App Status is the only expected application runtime
write. Private aggregate evidence remains under TEMP and must not be committed.

```powershell
.\tools\dev\run_batch10_4_controlled_live_startup.ps1 `
  -AcknowledgeLiveLibrary batch10.4-live-schema7-quiescence
```

## Batch 10.1 metadata-intelligence review

The Batch 10.1 helper captures token setup, consent, resumable job summary,
provider agreement/disagreement, structured credits, unofficial-live dates,
YouTube-exclusive fallback, gap-only artwork, and Artist **Featured On** states.
It uses an in-memory database and fake local evidence only, blocks network
events, keeps the credential field blank and masked, rejects personal-path or
authorization text, and deletes all captures after validation by default:

```powershell
.\tools\dev\run_batch10_1_review.ps1 --offscreen
```

For temporary human review, retain captures only below ignored `.ui-review/`
or in a correctly prefixed TEMP directory, then delete them after review:

```powershell
.\tools\dev\run_batch10_1_review.ps1 --offscreen `
  --output .\.ui-review\batch10-1 `
  --keep-captures
```

## Batch 10.2 migration-preservation gates

The Batch 10.2 helpers prove schema-5-to-6 migration preservation and the
narrow source-identity timestamp correction without printing library values.
They require the verified schema-5 rollback database and its pinned SHA-256.
The source and packaged proofs copy that database to a system temporary root,
sanitize media paths only in the disposable copy, run without secrets or
network access, and delete the temporary runtime after collecting aggregate
evidence.

```powershell
.\tools\dev\run_batch10_2_source_migration_proof.ps1 `
  -Schema5Backup <verified-schema5-backup> `
  -ExpectedSha256 <sha256>

.\tools\dev\run_batch10_2_packaged_migration_smoke.ps1
```

The live repair wrapper is intentionally separate and fail-closed. Use
`compare`, then `clone-proof`, before the explicitly acknowledged `repair`
mode. Repair creates a fresh schema-6 SQLite backup and permits only
`source_track_identities.updated_at` to change.

```powershell
.\tools\dev\repair_batch10_2_identity_timestamps.ps1 `
  -Mode compare `
  -TargetDatabase <schema6-database> `
  -ReferenceBackup <verified-schema5-backup> `
  -ReferenceSha256 <sha256> `
  -ExpectedIdentityCount 304 `
  -ExpectedRepairCount 304
```

## Batch 10.6 dual-orientation acceptance

The packaged smoke uses the official EXE with a disposable schema-7 runtime,
fictional metadata, injected fake providers, no credentials, and the process-
local no-network guard. It proves the reverse-orientation path without touching
the personal library:

```powershell
.\tools\dev\run_batch10_6_packaged_smoke.ps1
```

The live wrapper is a separate exact-one-target gate. Its default mode performs
aggregate-only read-only discovery. The explicitly acknowledged apply mode
creates and verifies a fresh SQLite backup before the bounded provider lookup,
then permits only the normalized one-target database repair. It never runs a
full-library scan or media/tag/artwork work:

```powershell
.\tools\dev\run_batch10_6_live_repair.ps1

.\tools\dev\run_batch10_6_live_repair.ps1 -Mode Apply `
  -AcknowledgeTargetedLookup batch10.6-live-one-track-orientation-repair
```
