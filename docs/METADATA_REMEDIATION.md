# Metadata Remediation

Music Vault's existing-library remediation workflow is conservative by design.
It can analyze a library, retain private candidate evidence, apply only strict
high-confidence changes, write verified tags to supported files, and roll an
applied job back. A trustworthy unresolved result is preferable to a false
canonical match.

Remediation is separate from synchronization. It never starts a YouTube sync,
does not use or read the YouTube API key, and does not change source-video
identity, playlists, queue state, or media organization.

Batch 10.1 adds a separate automatic Metadata Intelligence queue. Its
Discogs-first, MusicBrainz-secondary field ensemble can run after new imports
or in a resumable existing-library job only after explicit provider/privacy
consent. The older remediation guarantees remain authoritative: analysis and
review evidence are private, automatic application is field-level and strict,
manual/confirmed locks win, and no track, version, source membership, playlist
origin, or media file is merged or deleted. See
[Discogs Metadata](DISCOGS_METADATA.md).

## Analysis and apply are separate

**Analyze Library** creates or resumes a schema-v4 remediation job. Analysis:

- snapshots the effective metadata, provenance, confidence, locks, provider
  IDs, duration, and file state for each track;
- sends the current effective title, artist, and duration to MusicBrainz;
- uses cached valid responses where possible and retains sanitized candidate
  evidence in private runtime state;
- classifies every item without changing effective metadata, history, artwork
  references, embedded tags, audio, playlists, or source identity; and
- may be paused, cancelled, retried, and resumed after an application restart.

Analysis never runs automatically at application startup. MusicBrainz analysis
requires an explicit user action and network access. It uses no provider key,
YouTube key, browser cookie, arbitrary web search, or general web scraper.

**Apply High Confidence** is a second, explicit action. It first checks the job
and candidate age, library revision, per-item snapshot, locks, file state, disk
estimate, and backup location. Only items still classified as strict high
confidence are eligible. Needs-review, ambiguous, no-match, skipped, failed,
and stale items remain unchanged.

Database application and media-file writeback are separately explicit. The
headless maintenance tool requires a live-apply confirmation flag, and media
writeback additionally requires the file-write flag. Its default action is
aggregate status and is non-destructive.

## Confidence classes

- **High confidence:** a unique recording identity satisfies every automatic
  approval gate. Only safe fields supported by field-level evidence are
  proposed.
- **Needs review:** a candidate is plausible, but at least one automatic gate
  or release-level decision is incomplete.
- **Ambiguous:** multiple recording identities remain comparably plausible or
  identity evidence conflicts.
- **No match:** no credible provider candidate exists. Current metadata and
  artwork remain authoritative.
- **Skipped:** policy excludes the item, for example because authoritative
  locked metadata is already complete.
- **Failed:** provider, storage, or processing work failed safely. The item can
  be retried without reprocessing completed items.

Strict automatic approval requires, at minimum:

- provider score at least 95;
- exact approved-normalization title and artist comparison;
- duration evidence within five seconds;
- one distinct leading recording with a meaningful margin;
- no unresolved recording-identity or meaningful-version conflict;
- no meaningful version-risk qualifier; and
- no manual or user-confirmed lock.

Live, remix, edit, cover, acoustic, instrumental, demo, karaoke, remaster,
extended, slowed, sped-up, nightcore, mashup, game-version, and similar identity
signals are preserved and routed to review rather than silently discarded.
Ambiguous matches are never applied automatically.
Release or populated-field conflicts do not promote release metadata: the
recording may remain high confidence while conflicting album, album artist,
date, release ID, and artwork stay unchanged for review.

### Schema-v7 review outcomes

The automatic-intelligence review queue uses a narrower field-level policy:

- **Needs Review** means critical title/song, primary artist, structured
  credit, version, severe-duration, or competing-provider identity evidence is
  unresolved.
- **Applied with Gaps** means the critical identity is safe while secondary
  album, year, exact edition, artwork, label, catalogue number, or country is
  missing or ambiguous.
- **Accepted Source Fallback** means strong saved source-title evidence supplies
  title, artist, and version without a critical conflict when no credible
  catalogue match exists.

Missing secondary detail is not a failed remediation and does not inflate the
manual-review count. Soundtrack title/performer identity may apply with an
unresolved exact edition, while soundtrack, score, cast, and sequel identities
remain structurally distinct.

Existing `review`/`ready` rows can be re-evaluated from their persisted
normalized hints, field proposals/confidence, provider agreement, reason, and
current authoritative field state. Reclassification is bounded and resumable,
constructs no provider client, never weakens locks, applies only safe
high-confidence gaps, and leaves critical conflicts in review. It does not
touch media tags.

## Query normalization is not metadata rewriting

Provider queries use a separate comparison representation. Unicode, case,
whitespace, accents, and safely equivalent punctuation are normalized for
comparison. Presentation-only suffixes such as **Official Video**, **Official
Audio**, **Lyrics**, **Visualizer**, **HD**, or **4K** may be removed from the
query copy.

The stored title and artist are not changed merely because normalized query
text was used. Meaningful version qualifiers remain part of identity. A
provider's apparently cleaner title does not become safe unless the complete
matching and field-level policy approves it.

## Recording matches and field decisions

A high-confidence recording does not imply high confidence for every release
field:

- title and artist require exact or strongly confirmed identity;
- recording ID follows the high-confidence recording identity;
- album, album artist, release ID, and release date require one clearly
  preferred credible release;
- multiple plausible releases leave release-level fields for review;
- release date accepts only canonical `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`
  evidence and may safely add precision when compatible;
- artwork requires a confidently selected release and valid front artwork; and
- missing provider values never clear populated library fields.

`source_upload_date` is source-publication context. It is never used as
canonical `release_date` or `year` during remediation.

Automatic changes use `musicbrainz_high_confidence` (and, for accepted cover
art, `cover_art_archive_high_confidence`) provenance. This authority is above
embedded and YouTube fallback but below manual and user-confirmed locked
metadata. It is not marked manual, remains editable, and is recorded through
the same grouped history used by trusted manual correction.

Embedded artwork preserves aspect ratio, is never enlarged unnecessarily, is
bounded to about 1200 pixels on its longest edge, and targets about 1.5 MB or
less. Opaque images use efficient JPEG; transparency remains PNG when needed.

## Provider cache and private reports

Schema version 4 adds persisted remediation jobs and items plus a provider
query cache. Cache keys are normalized and hashed; cached records contain only
the candidate fields needed for matching, a response status, and fetch/expiry
times. Raw HTTP response bodies and credentials are not stored. Temporary
failures expire sooner than successful or no-match results.

Private reports are written atomically below:

```text
data/metadata_reports/<job-id>/
```

They include aggregate progress, item snapshots, proposed changes, sanitized
match reasons and errors, candidate evidence, metrics, and apply/rollback
manifests. These reports can contain titles, artists, albums, provider IDs,
paths, and prior metadata. They are private runtime data: Git ignores them,
the publication scanner rejects them, and public reports must use aggregate
counts only. App Status does not include item-level remediation data.

## Backups and supported media writeback

Before applying a job, Music Vault creates and verifies a SQLite backup through
SQLite's backup API. Before changing each supported media file, it creates and
hash-verifies a complete original-file backup under ignored runtime backup
storage. Backups are retained after success and are not deleted automatically.

MP3 is the currently supported audited tag-write format. Writeback operates on
a temporary full-file copy, updates approved text/identifier/artwork tags,
verifies tag readback, and atomically replaces the original only after all
checks pass. It verifies that the audio-payload hash, codec, and duration did
not change. It does not transcode, normalize, rename, move, or delete audio.

Unsupported formats are reported truthfully. Approved database metadata may be
applied while file writeback remains `unsupported`; Music Vault never claims a
tag write succeeded when no supported writer ran.

Cover Art Archive is used only for the confidently selected release. Downloads
are limited to approved public HTTPS hosts and pass redirect, response-size,
MIME, encoded-format, dimension, pixel, and decode checks. Artwork is normalized
for safe embedding, bounded to avoid unnecessary file growth, and never
replaces manual or confirmed locked artwork. Missing or ambiguous art preserves
the current cover.

Discogs artwork follows a narrower automatic policy: it may fill only a true
gap and is stored in private content-addressed runtime cover storage with
attribution. It never automatically replaces valid embedded, YouTube, Cover Art
Archive, manual, locked, or existing cover artwork, and it is never embedded in
media automatically. Accepted high-confidence text fields—including supported
original-date, version, and provider identifiers—may use the same verified MP3
writeback transaction; source upload date is never written as release date.

## Failure isolation, verification, and rollback

Each item applies independently. If one item fails, its prepared change is
restored when possible, a sanitized failure is retained, and other eligible
items continue. The completed job reports issues rather than claiming total
success.

Job verification reconciles aggregate counts and checks SQLite integrity,
foreign keys, grouped metadata history, lock preservation, provider-derived
release dates, truthful file status, exact backups, and unchanged audio
payloads. Verification output is aggregate-only.

Rollback requires the applied job ID and explicit confirmation. It restores
the full original media file from its verified backup and restores the exact
pre-apply metadata values, provenance, confidence, locks, and provider IDs while
adding an auditable rollback history group. If metadata or media changed after
the remediation apply, rollback marks a conflict and does not overwrite the
newer state.

Items left unresolved can be reviewed manually in the remediation dashboard or
with the Trusted Metadata editor. Manual and confirmed changes remain locked
and authoritative.

## Headless maintenance interface

The developer wrapper exposes aggregate-safe status, analysis, resume, apply,
private-report export, rollback, and verification actions:

```powershell
.\tools\dev\remediate_library_metadata.ps1 status
```

Analyze and provider access are explicit. Apply, file writeback, and rollback
require their dedicated confirmation flags. Item titles, artists, paths, and
candidate details are never printed by aggregate actions.

The separate schema-v7 stored-evidence reclassification helper defaults to a
dry run and prints aggregate outcomes only:

```powershell
.\tools\dev\reclassify_metadata_review.ps1
```

Its explicitly confirmed apply mode still uses only local saved evidence; it
does not contact Discogs, MusicBrainz, YouTube, LRCLIB, or another provider.
