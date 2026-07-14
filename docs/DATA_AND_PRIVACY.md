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
- synchronization archive history and structured failed-item records;
- downloaded audio and other media;
- extracted or downloaded cover and artist artwork;
- field-level metadata provenance, source observations, confidence, locks, and
  change history;
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
selected preset, and whether decoded-buffer reactivity is currently available.
App Status never contains PCM, audio samples, spectra, or other decoded-audio
content, and it does not contain the YouTube API key. Music Vault has no
Watchtower relationship or integration.

Synchronization supports public and unlisted playlists and performs anonymous
media extraction. It does not silently read Firefox, Chrome, Edge, or other
browser cookie profiles.

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

The v1.0.0 portable package bootstraps empty schema-v4 runtime data on first use.
Moving, copying, or sharing an initialized portable folder can also move its
private `data` directory, so inspect and remove runtime data before sharing an
application folder. The clean release ZIP should be obtained from the published
release rather than recreated from an initialized personal copy.
