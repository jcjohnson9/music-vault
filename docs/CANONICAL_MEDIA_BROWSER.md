# Canonical media browser

Music Vault 1.1 groups the Albums and Artists browsers by durable media
identity while preserving every track row, source membership, local file, and
user-visible metadata value.

This document describes Batch 10.3 on the v1.1.0 development line. The latest
public stable release remains immutable v1.0.0. Batch 11 remains next; Batch
10.3 does not create a tag or public Release.

## Canonical albums and editions

One top-level album card represents one album or release family. Discogs
master identity is preferred, followed by MusicBrainz release-group identity,
accepted provider release-family evidence, and finally a conservative key made
from the normalized base title, primary album artist, and album kind.

Ordinary deluxe, expanded, anniversary, remaster, reissue, format, country,
and alternate-cover editions share a card. Their track-level edition labels,
dates, provider references, and artwork remain distinct. Year and `cover_path`
are never part of the top-level identity.

Different works remain separate, including studio and live albums, soundtrack
and score releases, stage and film casts, remix albums, compilations, EPs,
singles, demo collections, greatest-hits releases, and separate soundtrack
franchise entries. Ambiguous cases remain separate instead of being merged
speculatively.

Schema version 7 stores the top-level identity in `canonical_albums` and one
active per-track link in `track_album_memberships`. The link retains edition
label/date, provider release/reference, position/disc, provenance, confidence,
and timestamps. Migration preserves all legacy track album/artist/date fields,
media paths, artwork paths, playlists, source memberships, observations,
history, and jobs.

### Representative covers

An album card selects one existing valid track cover for display. Manual or
locked artwork wins, followed by existing local or embedded art, Discogs,
Cover Art Archive, YouTube, other existing art, and finally the placeholder.
This is a browser choice only: Music Vault does not copy the path to other
tracks, replace valid art, delete alternate covers, or rewrite media tags.

## Canonical artists

Artist aliases preserve safe display variants and corrected legacy forms.
Artist relationships store verified facts such as a person being a member of a
group. A relationship requires structured provider evidence or explicit manual
confirmation; co-occurrence is not enough.

The corresponding schema-v7 tables are `artist_aliases` and
`artist_relationships`. Consolidation is planned first as an aggregate dry run,
then executed transactionally. Provider conflicts, unrelated same-name artists,
or person/group uncertainty produce diagnostics instead of a merge.

Safe duplicates can consolidate when provider identity agrees or the difference
is limited to non-conflicting case, spacing, or presentation punctuation. A
deterministic canonical entity is selected using accepted provider identity,
portrait availability, primary-credit usage, and stable ID. Conflicting
provider identities, unrelated same-name artists, and person/group ambiguity
remain separate and are reported for review.

Credits keep their role, order, join phrase, provenance, confidence, manual
state, and lock state when reassigned. Alternate display forms become aliases.
A duplicate entity is removed only after all credits, aliases, relationships,
provider identity, and portrait provenance are safely retained.

### Artist-page sections

- **Tracks** contains primary credits.
- **Featured On** contains featured credits on another primary artist's track.
- **Collaborations** contains peer collaborator credits.
- **Group Appearances** contains tracks by a verified group of which the artist
  is a member.

Groups remain group entities. An ampersand, comma, slash, or the word “and” is
not treated as proof that a band name should be split. Labels, distributors,
uploaders, release companies, and `Various Artists` release context are not
performer cards.

## Correcting version text in artist identity

Strong stored evidence can correct a malformed identity such as `Artist Live
at Venue`. The canonical artist receives the credit, `Live` becomes the version
type, and `Live at Venue` becomes the version label. The malformed display form
is retained as evidence or an alias, and the live recording remains a separate
track. Music Vault does not invent a studio album or release date.

Complete multi-artist credit strings are reconstructed only from structured
stored provider credits. Explicit `feat.`, `featuring`, `with`, `x`, `vs.`, or
provider-supported `presents` roles may be used; punctuation alone is never a
split rule.

## Review outcomes

`Needs Review` is reserved for meaningful uncertainty in song identity,
primary artist or structured credits, version identity, severe duration
conflict, or competing critical provider matches.

`Applied with Gaps` means critical identity is accepted while secondary details
such as album, year, exact edition, artwork, label, catalogue number, or country
remain unavailable or ambiguous. These items do not inflate the manual review
count.

`Accepted Source Fallback` means no commercial provider supplied a credible
match, but strong source-title evidence provides a usable title, artist, and
version with no critical conflict. Unsupported album and year values remain
blank, and the uploader remains separate provenance.

Existing review items can be reclassified from stored proposals, confidence,
agreement, parsed hints, and history. Reclassification does not require a new
provider request when stored evidence is sufficient, and it never auto-accepts
a critical conflict.

Reclassification runs in bounded batches, updates aggregate job outcomes, and
is idempotent for terminal items. It can fill only an empty, unlocked critical
field supported by saved high-confidence evidence; it does not rewrite a
populated field or media tag.

## Soundtracks

Music Vault distinguishes songs soundtracks, scores, cast recordings, game,
film, and television soundtracks, and character-performance context. Strong
title and performer or composer identity may be accepted even when the exact
edition or year is unresolved. Soundtrack versus score, sequel entries, and
stage versus film cast remain distinct. `Various Artists` is retained as
release context rather than a performer identity.

## Artist portraits

Valid manual and cached portraits are preserved. Missing portraits use the
private fallback chain: high-confidence Discogs artist image, existing
MusicBrainz/Wikidata/Wikimedia lookup, strict canonical Wikimedia fallback, and
placeholder. Album covers are never used as portraits. Cached files and
attribution remain private runtime data, and fetching requires the existing
user opt-in.

## Global Spacebar

Space toggles the existing player's Play/Pause state from ordinary application
pages when a media source is loaded. It does nothing when no source exists.
Text editors, token and URL fields, search, metadata and lyrics editors, modal
dialogs, buttons, checkboxes, sliders, spin controls, combo controls, and active
item editors retain their normal Space behavior. Party Mode keeps its own
Space handler.

## Privacy, review evidence, and performance

Canonical IDs, edition memberships, aliases, relationships, review evidence,
provider references, portrait files, and attribution are private runtime data.
App Status may contain aggregate counts only. It excludes artist/album/track
names, provider IDs, review proposals/reasons, URLs, and paths.

The Batch 10.3 review tool renders ten focused states from the actual
`MusicVaultWindow` album/artist browser and detail pages and the actual
metadata-intelligence dialog. Fictional rows live only in a disposable
current-schema database. The 1280×720 and 1920×1080 matrix includes one 150%
scale state and validates canonical albums and editions, the four artist
sections, all three review outcomes, soundtrack policy, version-as-artist
repair, and portrait fallback. Network entry points and live
credential/runtime access are blocked; captures and the synthetic runtime are
deleted by default and must never be committed.

The synthetic performance profile covers 300, 1,000, and 5,000 tracks with
canonical albums/artists, multiple editions, aliases, featured/collaborator
credits, group relationships, and a reclassified job. Album/artist summary SQL
statement counts stay constant as card counts grow, album membership lookup is
indexed, the delegate grid creates zero per-card QWidgets, and thumbnail work
remains visible-range only. Timings are aggregate development evidence rather
than product guarantees.
