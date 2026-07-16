# Discogs-First Metadata Intelligence

Music Vault 1.1.0 development builds can optionally enrich local metadata with
Discogs as the preferred automatic catalogue authority. MusicBrainz remains a
secondary corroboration/fallback source, and existing embedded metadata,
YouTube source context, title parsing, and manual decisions remain part of the
field-level evidence. The feature is local-first, opt-in, resumable, and
non-destructive.

This application uses Discogs’ API but is not affiliated with, sponsored or
endorsed by Discogs. “Discogs” is a trademark of Zink Media, LLC.

Accepted Discogs-backed details show a normal **Data provided by Discogs** link
to the applicable public release, master, or artist page.

## Personal token setup

Discogs access requires the user's own personal token. In Discogs, open
[Developer settings](https://www.discogs.com/settings/developers), create a
personal token under the current Discogs instructions, then in Music Vault:

1. Open **Settings**.
2. Find **Automatic Metadata Intelligence**.
3. Paste the token into the masked field and choose **Save Token**.
4. Choose **Test Connection** if desired.
5. Review the data/provider notice and enable Metadata Intelligence.
6. Choose whether to enable Discogs, MusicBrainz secondary lookup, verified
   text-tag writeback, gap-only artwork, and an initial existing-library scan.
7. Save Settings.

The token is stored only in the selected private runtime directory as
`data/discogs_token.txt`. It is not copied to configuration JSON, SQLite, App
Status, logs, reports, screenshots, release manifests, source control, or the
portable package. **Remove Token** deletes that local secret and disables
Discogs-dependent work. Never paste a real token into an issue or diagnostic.

Discogs availability and terms can change. Music Vault guarantees neither free
nor permanent access, performs no API purchase or marketplace transaction, and
does not enroll the user in a paid plan. A missing/rejected token, rate limit,
provider outage, policy change, or future access charge fails safely: local
import/playback continues and non-Discogs evidence remains usable.

## Provider authority and field confidence

Discogs is preferred automatically for:

- track and version title;
- primary, featured, and collaborative artist credits;
- album and album artist;
- original-song and version-specific release dates;
- version identity and release/master context;
- label/catalogue context; and
- front artwork for a true gap.

Provider preference is not blind authority. A strong, version-consistent
Discogs match wins only for fields with sufficient evidence. MusicBrainz can
corroborate recording/artist identity or supply a credible fallback. Meaningful
provider disagreement, competing releases, ambiguous dates, version conflict,
or uncertain artist identity enters review. Medium/low-confidence fields do not
apply automatically, and a missing provider value never clears a populated
library field.

Manual and user-confirmed locks remain strongest. Every accepted automatic
field retains provenance, provider reference, confidence, and timestamp and is
recorded through the existing metadata-history rules. An automatic change is
editable and cannot silently displace stronger locked authority.

## YouTube titles and uploader provenance

Music Vault parses a comparison-only copy of a YouTube video title to recover
useful artist/title, featured-credit, and version hints. Presentation suffixes
such as an official-audio or visualizer marker can be removed from the query
copy, while meaningful qualifiers such as live, remix, cover, acoustic,
extended, slowed, or sped-up remain identity evidence. Parsing by itself does
not rewrite stored metadata.

The YouTube uploader/channel is source provenance, not the default musical
artist. Conservative classification recognizes likely labels, distributors,
topic/auto-generated channels, promotional channels, and unknown uploaders.
Those names can help provider search but do not create artist entities merely
because they uploaded or released the media. A credible artist channel remains
evidence rather than unquestioned authority. The source upload date never
becomes `release_date`, `original_release_date`, or Year.

When no credible catalogue result exists for an actual online-only item, Music
Vault can preserve a `youtube_exclusive` version with its current safe fallback
metadata. It does not invent an official release or studio album.

## Structured artists and credits

Music Vault stores ordered artist credits separately from the compatible
display string. Roles are primary, featured, collaborator, remixer, and
performer; join phrases preserve intended display such as `feat.`, `&`, `with`,
or `x`. Artist entities can represent a person, group, band, duo, orchestra,
fictional artist, collective, or unknown entity.

A group or band remains one entity. Existing legacy artist strings migrate as
one unsplit primary credit—Music Vault does not split on ampersands, commas,
slashes, or the word “and.” Structured provider credits replace that
conservative placeholder only when evidence is credible. A label remains
release/company metadata and never appears as a primary/featured artist.

A featured recording appears in the primary artist's ordinary track list and
in **Featured On** for the featured artist. It is not counted as a primary
release for that featured artist. Collaborations remain independently
filterable without altering playback or playlist context.

## Original date, version date, and version identity

`release_date` describes the specific effective release/version and drives the
main Year display. `original_release_date` describes the original song or
recording when credible and is shown separately. Reissue/remaster evidence does
not blindly replace the original date.

For an unofficial live recording without an official release, `release_date`
and Year remain blank. Music Vault may show the original studio song's date
separately, marks the version as Live, does not assign the studio album
automatically, and retains uploader provenance. Studio, live, remix, edit,
acoustic, cover, instrumental, demo, radio edit, extended, sped-up, slowed,
nightcore, mashup, re-recording, soundtrack, YouTube-exclusive, and unknown
versions remain distinct tracks/media. `recording_group_key` can relate them
without deduplication, deletion, or source-membership changes.

## New imports and the existing library

After consent, a newly imported canonical track is queued once after its normal
import transaction. Provider work never blocks or invalidates import; provider
absence or failure leaves the imported track available. Successful enrichment
refreshes library/album/artist and current-player metadata without restarting
playback.

The **Analyze Existing Library** action creates or resumes one private job over
canonical tracks, regardless of how many Batch 10 sources reference each track.
Jobs can pause, resume, cancel, and retry failures. Completed items are not
repeated, no-match preserves current metadata, and aggregate counts reconcile.
The dashboard filters high-confidence apply, needs review, provider/version/
album/date/artist ambiguity, YouTube-exclusive, no-match, and failed outcomes.

Saving a token or synchronizing a source does not silently start a full-library
scan. After the user enables the feature and grants consent, Music Vault may
resume already-approved work or process canonical new-import items at launch.
The initial existing-library scan remains an explicit Settings choice. Source
playlist definitions, occurrences, origins, order, and media identity are
outside the metadata job and remain unchanged.

## Safe text-tag writeback

Automatic tag writeback is a separate setting and confirmation. Eligible
high-confidence textual corrections use Batch 7's supported-media path:

- verify a full-file original backup;
- mutate a temporary copy;
- read back approved fields;
- prove audio-payload, codec, and duration are unchanged;
- atomically replace only after validation; and
- restore safely or record a conflict on failure.

Ambiguous fields are not written. Source upload date is not written as release
date. Unsupported formats report no write rather than claiming success.
Discogs artwork is excluded from automatic media-file embedding.

## Gap-only artwork

A true artwork gap means no reference, a missing/corrupt referenced file, or an
explicit Music Vault placeholder. Music Vault never automatically replaces
valid embedded artwork, YouTube artwork already in use, Cover Art Archive
artwork, manual artwork, confirmed/locked artwork, or any valid existing
`cover_path`.

Only a validated front image from the accepted release is eligible. Image URLs
and redirects stay on approved HTTPS hosts; encoded bytes, MIME type, format,
dimensions, pixels, and decodeability are bounded. The image is content-
addressed under private `data/covers/discogs/` storage, retains source-page
attribution and fetch time, and is never committed or bundled.

Discogs catalogue text is provided under CC0. Discogs image content has
separate restricted handling and must not be treated as CC0, republished as a
project asset, or used as Music Vault branding.

## Networking, rate limits, cache, and privacy

Provider requests are sequential/bounded, cancellable, rate-aware, and use
explicit timeouts, response/pagination limits, HTTPS destination checks,
public-address validation, disabled environment proxy inheritance, response
structure validation, and sanitized errors. Music Vault honors provider rate
headers and backs off rather than issuing an unbounded request queue. It does
not inspect browser cookies.

Raw Discogs search/release/master/artist responses are held only in a private
in-memory duplicate-suppression cache for no more than six hours; they are not
persisted. Long-term state is limited to accepted normalized metadata,
structured IDs/context, public provider-page references, provenance,
confidence, fetch times, history, and the private job evidence required for
resume/review.

App Status contains only aggregate enable/readiness/job counts. It excludes the
token, query, uploader, track/release/artist IDs, candidate details, image URLs,
review reasons, item errors, and raw responses. Runtime databases, token files,
cover caches, jobs, provider evidence, reports, backups, and screenshots remain
private and are rejected by publication and release verification.

The current official references are the
[Discogs API documentation](https://www.discogs.com/developers/),
[API Terms of Use](https://support.discogs.com/hc/en-us/articles/360009334593-API-Terms-of-Use),
and [Application Name and Description Policy](https://support.discogs.com/hc/en-us/articles/360009207054-Application-Name-and-Description-Policy).
Users should review the current provider terms before enabling access.
