# Data and Privacy

Music Vault is a local-first application. Its runtime state is stored locally
and never in the public source repository. A default portable installation uses
`data/` beside `MusicVault.exe`; first-run setup can select another writable
private data directory. Source development continues to use the project-local
`data/` directory.

Depending on which features are used, local runtime data can include:

- a YouTube Data API key;
- the SQLite library database and its sidecar files;
- local configuration and status files;
- saved source URLs/labels/titles, source-item membership snapshots,
  source-run history, synchronization archive history, and structured
  failed-item records;
- downloaded audio and other media;
- extracted or downloaded cover and artist artwork;
- manually imported or provider-cached lyrics and negative-cache records;
- field-level metadata provenance, source observations, confidence, locks, and
  change history;
- a locally stored Discogs token, normalized accepted catalogue results,
  structured artist/release provenance, metadata-intelligence jobs, and private
  gap-only Discogs artwork;
- metadata-remediation reports; and
- local backups.

When a portable user selects a different data directory, Music Vault stores a
small per-executable location pointer under
`%LOCALAPPDATA%\Music Vault\runtime-locations\`. The pointer contains no API
key or library contents, but its local path can still be personal information.
Runtime files are never resolved relative to an arbitrary shell working
directory or written inside the packaged `_internal` directory.

Before an existing non-empty database is upgraded to a newer schema, Music
Vault uses SQLite's backup API to create a timestamped copy under
`data/backups/`. Backups are private runtime data and remain ignored by Git.

The generic `data/music_vault_status.json` App Status file contains operational
counts, paths, playback state, and the latest sanitized synchronization result.
Its additive Party Mode fields report only whether Party Mode is active, the
selected preset, whether decoded-buffer reactivity is currently available, and
optional boolean lyric availability/synchronization state.
App Status never contains PCM, audio samples, spectra, or other decoded-audio
content. It also never contains lyric lines, provider queries/results, cached
lyric paths, raw lyric errors, or the YouTube API key. Music Vault has no
Watchtower relationship or integration.

Batch 10 adds only aggregate source and batch counts to App Status. It never
exports a saved source URL, label, remote title, playlist ID, playlist-item or
video ID, destination playlist, source folder, membership snapshot, or per-item
error. App Status is updated at meaningful source/batch transitions rather than
for every progress line.

Batch 10.1 adds only aggregate metadata-intelligence readiness/job counts to App
Status. It never exports the Discogs token, provider query/result, track or
release/artist ID, title, artist, uploader, candidate, review reason, image URL,
local cover path, or raw provider error.

Batch 10.4 adds only `provider_work_deferred` and one stable aggregate reason:
`migration_startup`, `acceptance_no_network`, or `acceptance_no_secrets`.
Those fields contain no credential path or value, provider query, artist or
album name, source URL, or item-level provider result.

## Migration-startup and acceptance privacy

Music Vault distinguishes a database initialized during the current process
from an existing database actually upgraded during that process. The state is
memory-only; it is not user data, is not inferred from older backups, and is
not persisted to config. When an upgrade occurred, optional provider work is
deferred until the next ordinary launch. Persisted jobs and provider settings
remain unchanged, while local library browsing, playback, and valid cached
portraits remain available.

Acceptance no-secret mode prevents content reads of the YouTube API-key and
Discogs-token files. Acceptance no-network mode blocks optional provider
construction before transport creation. The controls are process-local and do
not permanently disable a feature. Existing portrait cache hits may be read,
but blocked misses are not queued and do not add negative-cache records or
rewrite the cache index.

Batch 10.4 acceptance evidence is aggregate-only. It may contain schema/table
counts, logical digests, file counts/bytes, credential file size/timestamp
metadata, safe status booleans, and a zero-attempt network report. It must not
contain credential contents, track/artist/album names, provider queries or
URLs, source identities, media paths, or personal screenshots. Fresh migration
acceptance backups remain private runtime data under `data/backups/` and are
never committed or bundled.

Synchronization supports public and unlisted playlists and performs anonymous
media extraction. It does not silently read Firefox, Chrome, Edge, or other
browser cookie profiles.

## Multiple source playlists

Schema version 5 stores saved source definitions, every remote playlist-item
occurrence, recent source runs, source-specific failure links, global
video-to-track identities, non-destructive identity-conflict diagnostics, and
manual/source playlist origins inside the private SQLite database. None of
these records is telemetry or hosted state.

Sources synchronize only after an explicit Sync Selected or Sync All Enabled
action and run sequentially in persisted order. Saving or editing a source does
not contact YouTube. The supported boundary remains authorized public/unlisted
playlists; there is no private-playlist OAuth, Google login, browser-cookie
access, automatic startup sync, or background schedule.

New source downloads use an identity-derived Windows-safe directory under the
configured download root. Existing media is never moved or renamed, and valid
database/file identity anywhere in the configured Music Vault tree is reused
across sources. A duplicate pre-existing identity is retained and recorded as
a private conflict rather than silently merged.

Only a complete multi-page source snapshot may mark an old occurrence removed.
Failed or partial enumeration preserves last-known membership and playlist
order. Remote removal, source detachment, destination changes, and source
archive never delete global tracks, media, metadata, artwork, lyrics, or
history. Managed playlist contents are preserved as manual origins when a
source is detached. See [Multiple Source Playlists](MULTIPLE_SOURCE_PLAYLISTS.md).

## Optional Discogs-first metadata intelligence

Automatic intelligence is disabled until the user stores a personal Discogs
token, accepts the provider/privacy notice, and enables the feature. The token
is kept only in `data/discogs_token.txt`, not config JSON or SQLite, and it is
never printed, logged, copied to App Status, sent to an image host, committed,
or bundled. Music Vault performs no purchase, marketplace transaction, or
automatic paid-service enrollment. Provider availability and terms can change;
imports and playback continue when Discogs is missing or unavailable.

For an analyzed track, Music Vault may send normalized title, artist, album,
duration, and version hints to Discogs. MusicBrainz can be used as secondary
corroboration/fallback. YouTube video titles help form queries; uploader/channel
names and upload dates remain source provenance. Provider networking is
bounded, cancellable, rate-aware, HTTPS/host restricted, and isolated from
browser credentials and environment proxy inheritance. Raw Discogs responses
are held in memory only for a short-lived duplicate-suppression cache and are
never retained as long-term library data.

Accepted metadata stores only normalized effective values, structured credits,
release/label context, provider IDs/page reference, field provenance,
confidence, and fetch time. Resumable job items can include private current
snapshots, parsed hints, proposals, agreement summaries, and review reasons;
these records can identify a personal library and stay inside the private
database. No item-level data enters public verification output.

Discogs catalogue text and images have different handling. Text catalogue data
is provided as CC0; Discogs image use remains restricted. A validated front
image may be cached under `data/covers/discogs/` only to fill a true gap, with
provider-page attribution. It never automatically replaces valid artwork and
is never embedded into an audio file automatically. The image, provider cache,
and attribution metadata are private runtime data, ignored by Git, and rejected
from release packages. See [Discogs Metadata](DISCOGS_METADATA.md).

## Canonical albums, artists, and review evidence

Schema version 7 stores canonical album cards and per-track edition membership,
artist aliases, verified relationships, and field-level intelligence outcomes
inside the same private SQLite library. These records can reveal album/artist
identity, personal corrections, provider references, and review history. They
are runtime data and never enter commits, screenshots, public reports, release
packages, or item-level App Status.

Canonical grouping changes browser identity only. It does not rewrite a
track's album text, date, media path, or `cover_path`; copy, delete, move, tag,
and artwork-replacement operations are not part of migration. Artist
consolidation retains aliases, relationships, credit roles/order/provenance,
portrait provenance, locks, and history. Conflicting provider identities stay
separate.

Stored-evidence review reclassification reads normalized private job fields
locally and does not need a provider request. Aggregate counts may enter App
Status, but titles, artist/album names, proposals, provider IDs, source/image
URLs, and review reasons do not. **Applied with Gaps** and **Accepted Source
Fallback** are metadata outcomes, not permission to invent missing release
data.

Canonical portrait fallback remains opt-in. A missing portrait may try a
high-confidence Discogs artist image and then the existing strict Wikimedia
chain; resulting images and attribution stay under private
`data/artist_images/`. Album artwork is never used as an artist portrait, and
valid cached portraits are not replaced merely because identities consolidate.

These categories can contain credentials, private library information,
personal playlist information, local paths, and copyrighted media. They are
private runtime data and are ignored by Git. They must not be added to commits,
issues, pull requests, release archives, or public logs.

A source checkout does not include a user's music library, credentials, media,
artwork, synchronization state, or private reports. The v1.0.0 portable release
also starts blank: it includes no populated data directory, database, config,
status, API key, archive, failed-item record, media, artwork, report, cache, or
backup. Its manifest and verifier record and enforce those boundaries.

The first-run guide supports entirely local use. A YouTube API key and FFmpeg
are optional for startup, local import, and local playback. If synchronization
is configured, the API key is written only through the local secret-file
mechanism; it is not added to JSON configuration, App Status, release manifests,
or logs. The release neither bundles nor automatically downloads the
`ffmpeg.exe` and `ffprobe.exe` command-line tools.

## Optional Party Mode lyrics

Lyrics display and online lookup are separate settings; both default to Off.
Display state persists when Party Mode or the application closes. Music Vault
first checks manually imported cache content, an adjacent same-stem `.lrc`,
read-only embedded synchronized lyrics, cached synchronized provider content,
an adjacent same-stem `.txt`, read-only embedded plain lyrics, and cached plain
provider content. It never modifies an adjacent file or embedded audio tag and
never writes fetched lyrics into personal media.

Managed content is stored in the selected private runtime directory under
`data/lyrics/`. The versioned index and content-addressed files use hashed
filenames, content hashes, atomic writes, track/fingerprint metadata, source
provenance, confidence, timestamps, and bounded negative-cache state. Cached
content may identify a personal library and may be subject to third-party lyric
rights; it is retained only for private local use. Cache files, manually
imported lyrics, adjacent personal sidecars, and provider responses must never
be committed, attached publicly, logged, or bundled. Git/history and portable/
source-compliance gates reject lyric payloads and provider-fixture paths.

When no local result exists, Music Vault asks before enabling online lookup.
Keeping local-only mode produces no request. When explicitly enabled, the
read-only LRCLIB lookup may send only the current track's title, artist,
optional album, and duration. It sends no API key, cookie, media/audio bytes,
playlist, filesystem path, or bulk-library inventory and performs no lyric
upload or contribution. Requests are HTTPS-only to `lrclib.net`, bounded by
timeouts and response limits, and protected by redirect/DNS public-address,
content-type, JSON, and strict-match validation. Weak, conflicting, or
ambiguous results are not automatically cached. Errors are sanitized and lyric
text never enters App Status or public logs. See [Lyrics](LYRICS.md).

## Manual metadata and candidate review

Manual metadata editing is local and requires no network. Schema version 3
stores source observations separately from effective values and records
field-level provenance, optional confidence, manual/lock state, and grouped
change history in the private SQLite library. This history can contain old and
new titles, artists, albums, dates, artwork references, and provider references;
it must be protected like the rest of the personal database.

The user can explicitly search MusicBrainz from the Trusted Metadata editor.
That action sends the current or user-entered title and artist to the public
MusicBrainz service. The separate remediation workflow can explicitly analyze
the library by sending each track's current effective title, artist, and
duration to MusicBrainz. Neither workflow runs automatically at startup; no
query is added to App Status, and no YouTube API key or browser cookie is sent.
Candidates in the Batch 6 one-track editor remain temporary until the user
selects a candidate, chooses fields, and confirms application. Batch 7 library
remediation instead persists private candidate evidence and expiring cache rows
so long-running jobs can resume safely, as described below.

Cover Art Archive image retrieval happens only after explicit candidate review
or when selected candidate artwork is explicitly applied. MusicBrainz and cover requests run with bounded time and
response sizes, HTTPS and provider-host restrictions, public-address checks,
sanitized errors, and disabled environment proxy inheritance. They require no
provider credential. The Batch 6 editor does not persist a separate search log;
Batch 7 retains only its private hashed cache identity and necessary candidate
evidence for resumability.

Validated local artwork is copied rather than permanently linked to its
original path. Content-addressed files are private runtime data under
`data/covers/manual/`; confirmed candidate covers use the provider-specific
runtime cover directory. Old artwork is not deleted automatically by clear,
reset, or undo. Track covers are independent from artist photos under
`data/artist_images/`.

Batch 6 manual corrections change only Music Vault's database and managed
artwork references. Batch 7 remediation keeps analysis non-destructive, then
allows a separately confirmed apply to update strict high-confidence database
fields and, with an additional explicit choice, verified tags in supported
MP3 files.

## Existing-library remediation data

Schema version 4 stores resumable jobs, private item snapshots, classifications,
candidate evidence, hashes, backup references, and an expiring provider cache
inside the local runtime database. Atomic private reports are written below
`data/metadata_reports/<job-id>/`; database backups remain under
`data/backups/`, and per-job original media backups remain under
`data/backups/metadata_jobs/<job-id>/`.

These records can contain titles, artists, albums, provider identifiers, local
paths, prior values, and candidate decisions. They are excluded from Git and
public packages and must not be pasted into issues or reports. Public and
headless verification output is aggregate-only. App Status contains no item-
level remediation data.

Successful/no-match provider cache entries expire, and temporary provider
failures use a shorter retry interval. The cache retains only normalized query
identity, necessary sanitized candidate fields, response state, and timestamps;
it does not store HTTP bodies, credentials, YouTube data, or browser cookies.

Applying a remediation job requires explicit confirmation. Needs-review,
ambiguous, no-match, skipped, failed, locked, and stale items remain unchanged.
Every supported media write uses a verified full-file backup and temporary
copy, then checks tag readback and that audio payload, codec, and duration did
not change. Music Vault does not transcode, normalize, rename, move, or delete
audio. Unsupported formats report no file write rather than pretending success.

Rollback uses the retained backup and pre-apply metadata/provenance snapshot.
If the user or another application changed a file or field after apply,
rollback records a conflict instead of overwriting the newer state. Backups and
reports are not removed automatically. See
[Metadata Remediation](METADATA_REMEDIATION.md).

## Optional artist photos

External artist-photo lookup is optional and defaults to disabled through the
local `artist_image_fetch_enabled` setting. While disabled, Music Vault makes
no artist-photo provider request. Existing valid cached photos may still be
displayed, and artists without one use Music Vault's local unknown-artist
placeholder rather than an album cover.

When the user explicitly enables artist photos, visible artist names may be
sent to public MusicBrainz services for identity matching. For a unique,
high-confidence exact normalized-name match, Music Vault may follow public
relations to Wikidata or English Wikipedia and request image metadata or image
bytes from Wikimedia services. Low-confidence, ambiguous, and absent matches
remain unknown. This workflow uses no provider credential, Music Vault account,
or YouTube API key.

Resolved photos and lookup provenance are cached locally under:

```text
data/artist_images/index.json
data/artist_images/files/
```

The versioned manifest can include the requested artist name, normalized key,
matched name, MusicBrainz artist ID, confidence score, provider, safe source
page and image URLs, local cache filename, timestamps, status, and retry time.
No-match and ambiguous outcomes are negatively cached so reopening or
repainting Artists does not repeatedly query public services. Temporary network
failures use a shorter retry period.

Disabling artist photos prevents future external requests but does not silently
delete the existing cache. The user can clear all cached artist photos or an
individual entry; clearing is contained to `data/artist_images/` and does not
modify music, track metadata, SQLite, covers, or other runtime data. Downloaded
artist photographs remain third-party material. Music Vault preserves
attribution links where available and does not claim ownership.

The entire artist-image cache is private runtime data, ignored by Git, and
excluded from public builds and source packages. Never attach its manifest or
files publicly without reviewing the artist identities, URLs, and local
information they contain.

Runtime files should not be deleted casually. Deleting the database or related
state may destroy library organization, playlists, metadata, or synchronization
history. Deleting downloaded media or artwork can also leave library records
incomplete. Back up the local `data/` directory before future metadata
remediation, database-schema work, or other maintenance that may alter runtime
state.

The v1.0.0 portable package bootstraps empty runtime data using its tagged
release schema; current v1.1.0 development builds create or migrate to schema
version 6 on first use.
Moving, copying, or sharing an initialized portable folder can also move its
private `data` directory, so inspect and remove runtime data before sharing an
application folder. The clean release ZIP should be obtained from the published
release rather than recreated from an initialized personal copy.
