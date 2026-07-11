# Data and Privacy

Music Vault is a local-first application. Its runtime state is stored locally
under the project's `data/` directory rather than in the public source
repository.

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

Before an existing non-empty database is upgraded to a newer schema, Music
Vault uses SQLite's backup API to create a timestamped copy under
`data/backups/`. Backups are private runtime data and remain ignored by Git.

The generic `data/music_vault_status.json` App Status file contains operational
counts, paths, playback state, and the latest sanitized synchronization result.
It does not contain the YouTube API key. Music Vault has no Watchtower
relationship or integration.

Synchronization supports public and unlisted playlists and performs anonymous
media extraction. It does not silently read Firefox, Chrome, Edge, or other
browser cookie profiles.

These categories can contain credentials, private library information,
personal playlist information, local paths, and copyrighted media. They are
private runtime data and are ignored by Git. They must not be added to commits,
issues, pull requests, release archives, or public logs.

A source checkout does not include a user's music library, credentials, media,
artwork, synchronization state, or private reports. The public repository is
source-only.

## Manual metadata and candidate review

Manual metadata editing is local and requires no network. Schema version 3
stores source observations separately from effective values and records
field-level provenance, optional confidence, manual/lock state, and grouped
change history in the private SQLite library. This history can contain old and
new titles, artists, albums, dates, artwork references, and provider references;
it must be protected like the rest of the personal database.

The user can explicitly search MusicBrainz from the Trusted Metadata editor.
That action sends the current or user-entered title and artist to the public
MusicBrainz service. No search runs automatically across the library, no query
is added to App Status, and no YouTube API key or browser cookie is sent.
MusicBrainz candidates remain temporary until the user selects a candidate,
chooses fields, and confirms application.

Cover Art Archive image retrieval happens only when selected candidate artwork
is explicitly applied. MusicBrainz and cover requests run with bounded time and
response sizes, HTTPS and provider-host restrictions, public-address checks,
sanitized errors, and disabled environment proxy inheritance. They require no
provider credential. Candidate searches are not persisted as a separate search
log.

Validated local artwork is copied rather than permanently linked to its
original path. Content-addressed files are private runtime data under
`data/covers/manual/`; confirmed candidate covers use the provider-specific
runtime cover directory. Old artwork is not deleted automatically by clear,
reset, or undo. Track covers are independent from artist photos under
`data/artist_images/`.

Batch 6 corrections change only Music Vault's database and managed artwork
references. Music Vault does not rewrite embedded tags or audio content in this
batch. Audited, resumable, rollback-capable file-tag remediation is a Batch 7
boundary.

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

A later blank release package will bootstrap empty runtime data on first use.
It will not contain a maintainer's or another user's library.
