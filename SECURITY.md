# Security Policy

Music Vault is currently supported as **v1.0.0 Stable** on the `main` branch.

## Protect personal and secret data

Never include any of the following in an issue, pull request, discussion,
attachment, screenshot, log excerpt, or reproduction repository:

- API keys, access tokens, cookies, credentials, or private keys
- Music Vault database, configuration, archive, failure, or status files
- Downloaded media, cover art, or other copyrighted/private assets
- Cached, imported, adjacent, embedded, or provider-returned lyric content
- Saved/private playlist URLs, labels, titles, item IDs, membership snapshots,
  destination mappings, source-run history, or contents
- Personal filesystem paths or usernames
- Unsanitized logs that may contain any of the above

This includes `data/discogs_token.txt`, Discogs queries/results and downloaded
images, metadata-intelligence item records, and personal review screenshots.

Credentials and runtime data are stored locally and are excluded from Git by
the repository rules. Those rules do not make it safe to share an arbitrary
copy of a working data directory.

Synchronization sanitizes external error text before it reaches the activity
log, structured failure history, or App Status. Common Google API keys, query
tokens, bearer/authorization values, and private-key blocks are redacted. The
supported public/unlisted workflow does not silently access browser cookies.

Batch 10 source definitions and occurrence/run history remain in the private
SQLite runtime database. App Status exposes aggregate source/batch counts only;
it excludes source URLs, labels, titles, playlist/item IDs, destination names,
local source folders, memberships, and per-item errors. Source activity logs
are bounded and sanitized and must not contain API keys, authorization headers,
cookies, raw provider responses, or unrestricted private paths.

Source synchronization is explicit and sequential. Saving a source does not
make a request, no source runs at startup, and the application does not support
private-playlist OAuth or Google login. Only complete pagination may reconcile
remote removals. Failed or partial enumeration preserves the last known-good
snapshot. Remote removal, source archive/detachment, and destination changes
cannot delete media or rewrite metadata, artwork, lyrics, or histories.

## Migration-startup and acceptance quiescence

Database startup records, in process-local memory, whether that database
instance initialized a new library or actually upgraded an existing schema.
An old migration backup does not imply that a later launch migrated. When an
upgrade did occur, Music Vault defers optional metadata-intelligence,
artist-photo, online-lyrics, synchronization-provider, and other external work
for the remainder of that process. The next ordinary non-migration launch can
resume enabled provider behavior; no provider setting or queued job is changed
merely because work was deferred.

The centralized runtime policy also interprets the acceptance-only
`MUSIC_VAULT_ACCEPTANCE_NO_SECRETS=1` and
`MUSIC_VAULT_ACCEPTANCE_NO_NETWORK=1` controls. No-secret mode returns an
unavailable readiness state before opening the YouTube API-key or Discogs-token
file. No-network mode rejects optional provider construction before creating a
transport session. Provider activation is lazy, so ordinary application
construction alone does not read the Discogs token or create an external
client. Existing valid cached portraits may still render read-only; a blocked
cache miss does not create a negative-cache entry.

App Status exposes only `provider_work_deferred` and the stable aggregate
reason `migration_startup`, `acceptance_no_network`, or
`acceptance_no_secrets`. It does not expose a credential path or value,
provider query, artist/album identity, source URL, or item-level result.

## Portable release integrity

Obtain the portable ZIP from the project's GitHub Release and compare its
SHA-256 value with the published checksum before extraction. Version 1.0.0 is
not code-signed, so Windows SmartScreen may show an unsigned-publisher warning;
that warning is not a substitute for checksum verification. Music Vault has no
automatic updater and does not request administrator access.

The published portable package is blank by construction and is checked for
credentials, personal paths, databases, configuration, status, archives,
media, artwork, lyrics, provider fixtures, reports, backups, unsafe ZIP entries,
and manifest/hash mismatches. It includes the explicit
`music-vault.portable.json` root marker
and creates private runtime data only after launch. Do not redistribute an
initialized portable folder as though it were the clean release.

Before public source publication, maintainers run both the current-candidate
scanner and the complete reachable-history scanner. The history gate examines
branches, remote-tracking refs, commits, tags and annotated-tag messages,
bounded blobs, and historical paths through read-only Git plumbing; it reports
only sanitized object/path/rule identities. A finding blocks publication and
must never be worked around by printing a secret or automatically rewriting
history.

Music Vault's repository source remains MIT licensed. The combined portable
binary includes separately licensed components and is distributed under the
terms described in [Binary Distribution License](docs/BINARY_DISTRIBUTION_LICENSE.md)
and [Third-Party Notices](THIRD_PARTY_NOTICES.md); it is not an MIT-only binary.

## Optional lyrics lookup

Party Mode lyrics and online lookup are independent and Off by default. Local,
manual, adjacent, embedded, and cached sources are checked before any network
work. If no local result exists, Music Vault requests consent before enabling
online lookup. Keeping local-only mode makes no provider request.

When explicitly enabled, the read-only LRCLIB request contains only the current
title, artist, optional album, and duration. It never includes the YouTube API
key, cookies, media/audio bytes, playlists, local paths, or a bulk library
inventory, and Music Vault does not upload or contribute lyrics. Requests use
HTTPS only, allow only `lrclib.net`, disable environment proxy inheritance,
apply connection/read and response-size limits, revalidate redirects and DNS
destinations, reject local/private addresses and malformed content, and reduce
failures to sanitized user-facing states. Strict metadata, qualifier, duration,
and ambiguity checks prefer no result over a weak match.

Managed lyric bodies and metadata are content-addressed, bounded, and written
atomically under private `data/lyrics/` runtime storage. Fetched lyrics are not
written into audio tags. Lyric text, provider queries/result IDs, cache paths,
and raw errors do not enter App Status or public logs. Cache content and
provider responses may be copyrighted and may identify a personal library;
never commit or attach them. Publication/history and portable/source-compliance
gates reject `.lrc` files, `.lyrics` cache payloads, lyric-cache text, and
provider fixtures. See [Lyrics](docs/LYRICS.md).

## Metadata provider requests and artwork

Manual field editing, clear/unlock/reset, history, and undo are local database
operations. Music Vault contacts MusicBrainz only after the user explicitly
clicks **Search MusicBrainz**; that request contains the entered title and
artist. Separately, **Analyze Library** may send each track's current effective
title, artist, and duration to MusicBrainz for an explicitly started, resumable
remediation job. Neither path uses a provider API key, YouTube API key, browser
cookie, or automatic startup scan.

MusicBrainz searches run outside the GUI thread with rate limiting, explicit
timeouts, response-size and JSON validation, an HTTPS-only MusicBrainz host
policy, public-address validation, disabled environment proxy inheritance, and
sanitized error codes. In the manual editor, Cover Art Archive retrieval occurs
only after selected candidate artwork is confirmed. Remediation may retrieve
front artwork for a validated private preview after explicit candidate review,
or during an explicitly confirmed apply for one unambiguous release and an
unlocked artwork field. Cover URLs and redirects are restricted to
approved HTTPS hosts, public addresses, and standard ports; image bytes, MIME
type, encoded format, dimensions, pixels, and decodability are bounded and
validated before storage.

Chosen local artwork is also decoded and bounded before Music Vault copies it
to content-addressed runtime storage. Do not commit or publicly attach managed
covers, metadata observations/history, provider references, or pre-migration
database backups. Clear, reset, and undo intentionally do not delete artwork
files.

Remediation analysis does not change effective metadata or media. Applying a
job requires explicit confirmation and is limited to unique strict high-
confidence matches; ambiguous, needs-review, no-match, skipped, failed, stale,
and locked items remain unchanged. Supported MP3 writeback requires an
additional explicit file-write choice, a verified complete original-file
backup, temporary-copy mutation, tag readback, unchanged audio-payload, codec,
and duration verification, and conflict-aware rollback. Unsupported formats
never report a successful write. Private reports, cache rows, candidate
snapshots, generated artwork, and media/database backups are runtime data and
must not be shared or committed. See
[Metadata Remediation](docs/METADATA_REMEDIATION.md).

Batch 10.1's automatic metadata intelligence is separately disabled until the
user supplies a personal Discogs token, accepts the provider/privacy notice,
and enables the feature. The token is stored only in
`data/discogs_token.txt`; it is never copied into JSON config, SQLite, App
Status, logs, reports, manifests, screenshots, or packages. Requests use HTTPS
to the official Discogs API destination with bounded time/response/pagination,
public-address validation, disabled environment proxy inheritance, rate-limit
handling, cancellation, and sanitized errors. Authentication is never placed
in a query string or sent to an image host.

Discogs queries may contain normalized title, artist, and album hints for the
current track being analyzed. YouTube uploader/channel and upload date stay
provenance; they are not silently promoted to artist or canonical release date.
Raw API responses are not persisted. Only accepted normalized metadata,
provider IDs and public page references, field provenance/confidence, fetch
timestamps, and private job evidence required for review/resume are retained.
App Status is aggregate-only and contains no token, query, provider response,
item identifier, candidate, artwork URL, or raw error.

Discogs artwork can fill only a verified gap. Downloads are host-restricted,
bounded, decoded, and stored under private content-addressed runtime storage.
Valid embedded, YouTube, Cover Art Archive, manual, locked, or existing artwork
is not automatically replaced. Discogs images are never embedded into media
automatically and are rejected by Git/history and release publication checks.
Textual catalogue metadata is treated separately from restricted image
content. See [Discogs Metadata](docs/DISCOGS_METADATA.md).

Schema-v7 canonical album/artist rows, aliases, verified relationships,
edition memberships, review proposals/reasons, and consolidation diagnostics
are equally private. Publication and history gates reject databases, reports,
provider images, screenshots, and item-level evidence. App Status may expose
aggregate album/artist/conflict/outcome counts only; it must not expose names,
titles, provider IDs, paths, URLs, or proposals.

Migration and consolidation run without constructing provider clients. They
must preserve tracks, media paths, source/playlist membership, `cover_path`,
locks, observations, and history; they never rewrite media tags. Representative
album covers are browser-only. Conflicting same-name artists remain separate,
and punctuation, labels, uploaders, or `Various Artists` context cannot by
themselves create or merge performer identities.

## Optional artist-image requests

Artist-photo lookup is disabled by default and requires explicit user opt-in.
It uses no provider API key and never copies or transmits the YouTube API key.
When enabled, artist names may be sent to public MusicBrainz services and a
high-confidence match may lead to Wikidata, English Wikipedia, Wikimedia
Commons, or Wikimedia image requests.

For a missing canonical portrait, an explicitly enabled Discogs provider may
first supply one high-confidence artist image. An invalid or unavailable image
falls through to the existing strict Wikimedia chain. Provider images remain
private third-party runtime content; album artwork is never substituted as a
portrait, and no request occurs while portrait fetching is disabled.

Artist-image networking runs outside the GUI thread with bounded concurrency,
timeouts, MusicBrainz rate limiting, and request coalescing. Only HTTPS URLs on
an explicit provider whitelist are accepted. Redirect targets are revalidated,
DNS answers resolving to loopback, private, local, or other non-global
addresses are rejected, and environment proxy inheritance is disabled. JSON
and image responses have byte limits; image MIME type, encoded format,
dimensions, and decodability are validated before storage. Provider errors are
reduced to sanitized codes instead of exposing raw URLs or local paths.

Downloaded photos and provenance are private runtime data under
`data/artist_images/`. The manifest is written atomically, cached filenames use
content hashes, and cache-clear operations remain inside that directory.
Source-page URLs open only after an explicit user action and only when they
match the safe public-source policy. Do not commit or publicly attach the
cache, its manifest, or downloaded third-party photographs.

## Reporting a vulnerability

If GitHub private vulnerability reporting is enabled for the repository, use
the repository's **Security** tab to submit a private report.

If private reporting is unavailable, open a minimal public issue that describes
only the affected component and general impact. Do not include exploit details,
credentials, personal data, or sensitive logs. A maintainer can arrange a safer
channel if additional details are required.

Before sharing logs or diagnostics, remove secrets, playlist information, song
or library details, usernames, and absolute local paths. Do not merely obscure
part of a credential; remove it entirely.

## Scope and response expectations

Security reports concerning the current stable source and portable release are
welcome. This is a personal open-source project, and no response-time or
remediation SLA is promised.
