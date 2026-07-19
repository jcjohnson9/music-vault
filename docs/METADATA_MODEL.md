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
- version-specific canonical release date;
- original-song release date;
- normalized version type and descriptive version label; and
- artwork.

These values remain materialized in `tracks` (`cover_path` stores artwork) for
compatibility and browser performance. Schema version 3 adds `release_date` and
`metadata_updated_at`. The `year` column remains a compatible display/grouping
value derived from the first four digits of a valid canonical `release_date`.
Schema version 6 adds `original_release_date`, `version_type`, `version_label`,
Discogs release/master/position identifiers, and `recording_group_key`. A group
key is navigation/relationship evidence only; it never merges or deletes
separate track/media records.

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

## Structured artists, release context, and versions

Schema version 6 stores artists independently from the legacy display string.
`artists` distinguishes person, group, band, duo, orchestra, fictional,
collective, and unknown entities. `track_artist_credits` preserves ordered
primary, featured, collaborator, remixer, and performer roles plus display join
phrases. A band/group remains one entity; punctuation alone does not imply a
split, and labels or uploader channels do not become artists merely because
they released or uploaded media.

The materialized `tracks.artist` remains the compatibility/display value.
Featured artists appear in the primary artist's ordinary tracks and in the
featured artist's **Featured On** section, but not as primary releases for that
artist. `track_release_context` keeps Discogs release/master identity, title,
country, format, catalogue number, label, version release date, original-song
date, provider reference, confidence, and timestamp. A label is company/release
metadata, never an artist credit.

`release_date` describes the specific effective recording/release version;
`original_release_date` describes the underlying song/recording history when
credible. For an unofficial live recording with no official release,
`release_date` and Year remain blank while the original-song date may still be
shown separately. `version_type` normalizes studio, live, remix, edit, acoustic,
cover, instrumental, demo, radio/extended, sped-up/slowed, nightcore, mashup,
re-recording, soundtrack, YouTube-exclusive, and unknown identities;
`version_label` preserves useful detail such as venue, mix, or remaster text.

## Canonical albums and artist identity

Schema version 7 adds a browser-identity layer without changing materialized
track metadata. `canonical_albums` represents a durable album/master family;
`track_album_memberships` retains each track's release ID, edition label/date,
position, provenance, provider reference, and confidence. Identity priority is
Discogs master, MusicBrainz release group, accepted provider release family,
then conservative normalized base title plus canonical album artist and album
kind. Ordinary year, country, format, and cover differences do not create a
new top-level card.

Album kind protects genuinely distinct works: studio, live, soundtrack, score,
cast, compilation, greatest-hits, remix, EP, single, and demo identities do not
collapse merely because their titles resemble each other. The membership layer
never rewrites `tracks.album`, `tracks.album_artist`, release dates, or
`cover_path`.

`artist_aliases` retains safe display/provider/legacy variants while
`artist_relationships` stores verified facts such as `member_of`. Provider IDs
or strong saved evidence may justify consolidation; conflicting IDs,
person/group ambiguity, and unrelated same-name artists remain separate.
Credit role, order, join phrase, provenance, confidence, manual/lock authority,
portrait provenance, and history survive any safe reassignment. See
[Canonical Media Browser](CANONICAL_MEDIA_BROWSER.md).

Artist pages partition one set-based result into **Tracks**, **Featured On**,
**Collaborations**, and verified **Group Appearances**. A group track is not
presented as a member's solo track. Punctuation does not prove a band split,
and `Various Artists`, labels, distributors, or uploaders never become
performer cards from release/source context alone.

## Field-level automatic outcomes

Automatic work finishes as `Applied`, `Applied with Gaps`, `Accepted Source
Fallback`, `Failed`, or `Skipped`; it does not wait in an ordinary Review
queue. Accepted identity with missing album, year, exact edition, artwork,
label, catalogue number, or country becomes `Applied with Gaps`. Strong
source-title identity with no credible catalogue match becomes `Accepted
Source Fallback`; unsupported release fields remain blank.

Stored normalized evidence can be reclassified in bounded batches without a
provider request. High- and medium-confidence database values retain confidence
and history; medium-confidence values are not eligible for automatic media-tag
writeback. Operational corruption or apply failure is `Failed`, while honest
metadata absence is a gap or source fallback. Soundtrack title/performer
identity may apply while an exact edition remains a gap; soundtracks, scores,
casts, and sequel entries retain distinct album kinds.

## Authority and precedence

Automatic observations use a centralized precedence policy:

1. locked manual values;
2. locked user-confirmed MusicBrainz values;
3. other locked user-confirmed provider values;
4. strict high-confidence remediation values;
5. credible embedded metadata;
6. YouTube/source fallback;
7. filename fallback or unknown.

When automatic intelligence is enabled, a version-consistent Discogs match is
preferred; MusicBrainz is secondary corroboration/fallback. Embedded values,
provider-adjudicated source-title hints, and source fallback follow. Field
confidence and reasons are retained, and hard version/duration mismatches do
not erase the source qualifier. YouTube title parsing supplies two orientation
hypotheses rather than overruling provider identity; uploader/channel and
upload date remain source provenance rather than default artist or release
date.

For a safe top-level dash split, the parser retains immutable
`left_is_artist` and `right_is_artist` hypotheses with the same raw title,
year, version, featured-credit, and presentation clues. Discogs evaluates the
conventional orientation first and the reverse only when the first result is
not conclusive. MusicBrainz is a single secondary corroboration/fallback.
Normalized intelligence evidence records the evaluated count, selected
orientation, confidence, bounded request counts, and reason codes; it never
stores a query, token, or raw provider response. Strict unique canonical-artist
evidence may resolve an item offline. Otherwise current values are preserved
as an accepted source fallback that remains eligible for provider
adjudication.

Locked values are not replaced by automatic imports. A lower-priority or empty
automatic observation cannot erase a stronger populated value, but the
observation is still retained for inspection. An effective mutation updates the
field-state row and materialized `tracks` value in one transaction. Observation-
only updates and no-op saves do not create metadata history or advance
`metadata_updated_at`.

## Manual correction operations

The Trusted Metadata editor supports title, artist, album, album artist,
version release date, original-song release date, normalized version type,
version label, and artwork, and deliberately
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

A canonical album card chooses one representative existing valid track cover
for display only. Manual/locked and existing local or embedded art outrank
provider/fallback art. That selection never copies a path to another track,
replaces valid artwork, deletes alternate covers, or writes an audio tag.

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

Schema version 5 adds multiple-source synchronization identity and origin
tables. Schema version 6 additively introduces structured artists/credits,
release context, new version/date materialization, and resumable automatic-
intelligence jobs/items. Its conservative backfill preserves the exact legacy
artist display string as one unsplit primary unknown entity, respects existing
field authority, and performs no provider request or media write. The verified
SQLite backup/integrity/idempotence rules remain in force.

Schema version 7 additively introduces canonical albums/memberships, artist
aliases/relationships, and expanded intelligence outcomes. Backfill groups only
safe edition variants, creates conservative credits/aliases, and reclassifies
stored evidence without provider access. It preserves every track, media path,
`cover_path`, source/playlist membership, lock, observation, history row, job,
provider record, and separate version. A verified backup precedes non-empty
schema-6 migration; integrity, foreign-key, index, idempotence, and aggregate
preservation checks remain mandatory.

Each database instance exposes process-local startup facts: whether it
initialized a new database, whether it actually migrated, the source and target
schema versions, and the migration backup path. An old backup cannot make a
later current-schema open report a migration. If an upgrade occurred, the
central runtime policy defers optional metadata-intelligence, portrait, lyric,
and other provider work for the rest of that process. This adds no metadata
field, history event, observation, job failure, or persisted configuration
change; the next ordinary non-migration process may resume queued work.

Acceptance no-secret/no-network states use the same process-local policy and
are checked before credential reads and transport construction. App Status
preserves its versioned consumer contract and exports only an aggregate
deferred boolean and safe reason, never a credential, query, identity, URL, or
item-level result.

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
