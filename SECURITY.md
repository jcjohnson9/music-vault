# Security Policy

Music Vault is currently supported as **v1.0.0 Stable** on the `main` branch.

## Protect personal and secret data

Never include any of the following in an issue, pull request, discussion,
attachment, screenshot, log excerpt, or reproduction repository:

- API keys, access tokens, cookies, credentials, or private keys
- Music Vault database, configuration, archive, failure, or status files
- Downloaded media, cover art, or other copyrighted/private assets
- Private playlist URLs or contents
- Personal filesystem paths or usernames
- Unsanitized logs that may contain any of the above

Credentials and runtime data are stored locally and are excluded from Git by
the repository rules. Those rules do not make it safe to share an arbitrary
copy of a working data directory.

Synchronization sanitizes external error text before it reaches the activity
log, structured failure history, or App Status. Common Google API keys, query
tokens, bearer/authorization values, and private-key blocks are redacted. The
supported public/unlisted workflow does not silently access browser cookies.

## Portable release integrity

Obtain the portable ZIP from the project's GitHub Release and compare its
SHA-256 value with the published checksum before extraction. Version 1.0.0 is
not code-signed, so Windows SmartScreen may show an unsigned-publisher warning;
that warning is not a substitute for checksum verification. Music Vault has no
automatic updater and does not request administrator access.

The published portable package is blank by construction and is checked for
credentials, personal paths, databases, configuration, status, archives,
media, artwork, reports, backups, unsafe ZIP entries, and manifest/hash
mismatches. It includes the explicit `music-vault.portable.json` root marker
and creates private runtime data only after launch. Do not redistribute an
initialized portable folder as though it were the clean release.

Music Vault's repository source remains MIT licensed. The combined portable
binary includes separately licensed components and is distributed under the
terms described in [Binary Distribution License](docs/BINARY_DISTRIBUTION_LICENSE.md)
and [Third-Party Notices](THIRD_PARTY_NOTICES.md); it is not an MIT-only binary.

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

## Optional artist-image requests

Artist-photo lookup is disabled by default and requires explicit user opt-in.
It uses no provider API key and never copies or transmits the YouTube API key.
When enabled, artist names may be sent to public MusicBrainz services and a
high-confidence match may lead to Wikidata, English Wikipedia, Wikimedia
Commons, or Wikimedia image requests.

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
