# Developer tools

Run these helpers from PowerShell at the repository root. Each wrapper resolves
the root and uses `.venv\Scripts\python.exe`; generated output and private
runtime data are not source artifacts.

| Command | Purpose |
| --- | --- |
| `.\tools\dev\verify.ps1` | Parse/import active source and validate required project resources. |
| `.\tools\dev\run_source.ps1` | Run the checked-out development source. |
| `.\tools\dev\build_exe.ps1` | Rebuild the official one-folder development EXE. |
| `.\tools\dev\capture_ui_review.ps1` | Run the explicitly gated synthetic application review matrix. |
| `.\tools\dev\run_batch10_1_review.ps1 --offscreen` | Capture the bounded ten-state Discogs metadata-intelligence review with in-memory synthetic data and networking blocked. |
| `.\tools\dev\profile_media_browsers.ps1` | Profile browser structure with temporary synthetic databases. |
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
