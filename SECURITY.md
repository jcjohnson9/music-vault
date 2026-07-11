# Security Policy

Music Vault is currently supported as a **v1.0.0 Release Candidate** on the
`main` branch. Security handling may evolve before V1 Stable.

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

Security reports concerning the current release-candidate source are welcome.
This is a personal open-source project, and no response-time or remediation SLA
is promised.
