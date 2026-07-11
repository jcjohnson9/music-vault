# Metadata Model

Music Vault treats displayed metadata as library data with an explainable
authority chain. Provider and file values are retained as observations; they do
not automatically become the value shown by the application when a stronger or
locked value already exists.

## Three metadata layers

### Source observations

`track_metadata_observations` records values observed from a source without
blindly replacing the library value. An observation identifies the track,
provider, field, value, provider reference, optional confidence, and observation
time. A deterministic observation key prevents identical observations from
being duplicated while allowing distinct providers or values to coexist.

Examples include embedded tags, YouTube source title/uploader/upload date and
thumbnail information, and MusicBrainz or Cover Art Archive candidate data.
`source_upload_date` and `source_video_id` are observation/context fields, not
editable canonical music metadata.

### Effective library metadata

The effective values are what Music Vault displays, groups, searches, and shows
in Now Playing:

- title;
- artist;
- album;
- album artist;
- canonical release date; and
- artwork.

These values remain materialized in `tracks` (`cover_path` stores artwork) for
compatibility and browser performance. Schema version 3 adds `release_date` and
`metadata_updated_at`. The `year` column remains a compatible display/grouping
value derived from the first four digits of a valid canonical `release_date`.

`release_date` accepts `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` with real calendar
validation. A YouTube/source upload date describes publication of the source;
it never fills `release_date` or `year`.

### Field state

`track_metadata_fields` has one effective row per track and editable field. It
stores the effective value, provenance, provider reference, optional confidence
from 0 through 100, manual flag, lock flag, and update time. This normalized
shape allows future fields without adding a new lock column to `tracks`.

Common provenance values include `manual`, `musicbrainz_confirmed`,
`provider_confirmed`, `musicbrainz_high_confidence`,
`cover_art_archive_high_confidence`, `embedded`, `youtube`,
`youtube_thumbnail`, `cover_art_archive`, `filename`, and `unknown`.

## Authority and precedence

Automatic observations use a centralized precedence policy:

1. locked manual values;
2. locked user-confirmed MusicBrainz values;
3. other locked user-confirmed provider values;
4. strict high-confidence remediation values;
5. credible embedded metadata;
6. YouTube/source fallback;
7. filename fallback or unknown.

Locked values are not replaced by automatic imports. A lower-priority or empty
automatic observation cannot erase a stronger populated value, but the
observation is still retained for inspection. An effective mutation updates the
field-state row and materialized `tracks` value in one transaction. Observation-
only updates and no-op saves do not create metadata history or advance
`metadata_updated_at`.

## Manual correction operations

The Trusted Metadata editor supports six editable fields and deliberately
separates four operations:

- **Set:** stores the entered value as manual and locked by default.
- **Clear:** stores a null effective value as manual and locked, preventing an
  importer from silently repopulating it. Title cannot be cleared.
- **Unlock:** preserves the current value and provenance while permitting a
  stronger future automatic observation to replace it.
- **Reset to automatic:** removes manual/lock authority and selects the best
  already-recorded automatic observation. It does not contact a provider.

Multi-field changes commit atomically under one change-group ID. Cancel and a
save with no effective changes leave the database, history, timestamps, and
caches unchanged.

## History and undo

`track_metadata_history` records every effective change with the old and new
value, provenance, provider reference, confidence, manual flag, and lock flag,
plus actor, reason, timestamp, and change-group ID. History is retained for
manual edits, clear/unlock/reset operations, confirmed candidates, artwork
changes, and undo. Source-observation-only work, playback, refreshes, and no-op
imports are not history events.

**Undo Last Metadata Change** previews and restores the most recent complete
group for one track, then writes a new history group describing the undo. It
does not delete the original history or artwork files, alter playlists, restart
playback, or change the queue/base playback context.

## MusicBrainz confirmation

MusicBrainz is searched only when the user selects **Search MusicBrainz**. The
current or user-edited title and artist are sent to MusicBrainz; no API key or
browser cookie is used. Results are sorted by score and displayed as explicit
candidates with recording/release identifiers, title, artist credit, release,
release date, country/status where available, confidence, and artwork
availability.

No result is applied automatically. The user selects one candidate and chooses
which populated fields to apply. Low-confidence results show an additional
warning and require explicit confirmation. Applied fields use
`musicbrainz_confirmed` provenance and are locked. A candidate without usable
artwork never clears existing artwork.

Cover Art Archive retrieval occurs only when the user selects candidate artwork
and confirms application. Provider work runs outside the GUI thread, uses
bounded HTTPS requests to approved public hosts, validates redirects, response
size, MIME type, pixels, and image decoding, and reports sanitized errors.

## Artwork storage

Chosen local artwork must be a decodable PNG, JPEG, or supported WebP within the
configured byte and pixel limits. Music Vault copies validated content into
ignored runtime storage under `data/covers/manual/` using a content hash,
deduplication, and an atomic replacement. Confirmed Cover Art Archive images
use the corresponding runtime provider directory under `data/covers/`.

The library stores the managed artwork path and provenance. It does not keep an
arbitrary external file path as the permanent cover, and clear/reset/undo does
not delete older artwork files. Track artwork remains separate from Batch 5's
optional artist-photo cache under `data/artist_images/`.

## Schema migration

SQLite schema version 3 adds the two materialized track columns and the field,
observation, and history tables additively. Before a non-empty older database is
migrated, Music Vault creates and verifies a timestamped backup using SQLite's
backup API under `data/backups/`. Migration preserves existing tracks,
playlists, memberships, source identity, paths, and canonical values, seeds
conservative provenance without marking ordinary values manual, and does not
query providers, read media tags, fabricate release dates, or create history.

Schema version 4 additively introduces persisted remediation jobs/items and a
bounded provider cache. It does not reinterpret existing effective values,
locks, observations, or history during migration. A verified SQLite backup is
created before a non-empty schema-v3 database is upgraded.

## Existing-library remediation

Batch 6 corrections are authoritative inside the Music Vault database only.
They update visible library and current-player metadata without restarting the
media or changing playback position, queue, base context, or playlist
membership. Batch 6 never rewrites embedded audio-file tags.

The metadata service exposes effective and approved snapshots, observations,
review state, field actions, history, undo, strict high-confidence application,
and exact snapshot restoration for the audited remediation workflow.

Remediation analysis stores a private snapshot and candidate assessment without
changing effective fields or history. Query normalization is comparison-only;
presentation suffix cleanup never rewrites stored text by itself. Recording
identity and field confidence are separate: a unique high-confidence recording
can still leave album, release date, album artist, or artwork for review when
multiple releases remain plausible. Source upload dates never become canonical
release dates.

Eligible automatic changes use unlocked `musicbrainz_high_confidence` or
`cover_art_archive_high_confidence` provenance, retain provider references and
confidence, and write one grouped history event. They remain below manual and
confirmed locks and remain editable. User-confirmed candidates continue to use
locked `musicbrainz_confirmed` provenance.

An explicit file-writeback action may mirror approved fields into a supported
MP3 only after exact backup and unchanged-audio verification. Database and file
rollback restores the pre-apply field values, provenance, confidence, locks,
provider IDs, and original file while retaining auditable apply/rollback
history. Later metadata or media changes cause a conflict instead of being
overwritten. See [Metadata Remediation](METADATA_REMEDIATION.md).
