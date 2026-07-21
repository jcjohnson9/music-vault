# Multiple Source Playlists

Multiple Source Playlists is part of the Music Vault v1.1.0 development line.
It replaces the old one-playlist-at-a-time screen with saved synchronization
sources. The latest public stable release remains v1.0.0.

## Saved sources

A source represents one authorized public or unlisted YouTube playlist. Music
Vault stores its normalized playlist identity, an optional local label, the
latest remote title, enabled state, deterministic Sync All order, destination,
stable download-storage key, item occurrences, recent run history, and
source-specific failures in the private SQLite database.

The source URL, label, remote title, playlist-item IDs, membership snapshots,
errors, and storage folder are private runtime data. They are excluded from App
Status, source control, screenshots, logs intended for publication, and release
packages.

The supported workflow does not use browser cookies, Google-account login, or
private-playlist OAuth. Saving a source performs local validation only and never
starts a network request. Synchronization occurs only after an explicit user
action; no source runs automatically at startup.

## Source management

Sync Center supports:

- **Add Source** for a standard YouTube playlist URL, a
  `music.youtube.com` playlist URL, or a valid raw playlist ID;
- **Edit Source** for its label, enabled state, order, and destination;
- **Enable/Disable** without losing membership or history;
- **Move Up/Move Down** to define deterministic sequential execution;
- **Remove Source** to archive and safely detach it without deleting media;
- **Sync Selected** for one or more selected enabled sources;
- **Sync All Enabled** in the displayed source order; and
- **Stop After Current** to finish the active source safely and start no later
  source.

An external playlist ID cannot be edited in place. Add a new source and archive
the old source instead so occurrence identity and history remain truthful.
Re-adding an archived external ID restores its existing definition rather than
creating a duplicate.

## Download quality override

A saved source may use the global download profile or explicitly request Best
Original or MP3 320 Compatibility. Saving or changing this override is local
configuration work: it does not start synchronization, replace a file, or
change source membership.

The override applies only when a future item has no reusable canonical media.
Best Original retains the selected supported source codec and remuxes only when
needed for a practical audio-only file. MP3 320 is a lossy compatibility
transcode and is never presented as a fidelity improvement. See
[Audio Quality Profiles](AUDIO_QUALITY.md).

## Destinations

### Library Only

Downloaded or existing tracks are mapped into the global library. No local
Music Vault playlist is modified.

### Managed Local Playlist

The source also contributes a source origin to one linked local playlist. The
current source-managed tracks appear first in remote order. Manual-only tracks
remain supported and follow them in their stable manual order. A track with
both origins appears once; its manual origin remains available if the remote
source later removes that item.

A source may create a new playlist or link an eligible existing playlist. Only
one active source may manage a local playlist. Renaming the local playlist does
not break the ID-based link, and later remote-title changes never overwrite the
local name.

Detaching, changing destination, or archiving a source converts its currently
visible managed origins into manual origins. The old playlist, its visible
order, the library tracks, metadata, artwork, lyrics, histories, and media all
remain intact. Music Vault offers no destructive “delete source tracks” action.

## Identity and deduplication

Each YouTube video ID maps to one canonical Music Vault track identity across
all saved sources. A later source reuses a valid existing database/file mapping
or a file discovered within the configured Music Vault download tree. It does
not redownload a track just because the same video appears in another source.

The first successful acquisition also determines that track's stored media
representation. If a later source requests another quality profile, Music Vault
reuses the existing file and reports its actual stored quality facts rather
than creating a second representation.

If pre-existing track rows already claim the same video, migration selects a
canonical mapping deterministically—preferring a row whose file exists, then
the lower stable track ID. Every track row is preserved and the discrepancy is
recorded as a private aggregate diagnostic; Batch 10 does not merge duplicates.

New files for a saved source are placed under:

```text
<configured-download-root>/sources/<stable-storage-key>/
```

The key is derived from normalized source identity, is safe for Windows, and
does not change with labels or remote titles. Existing files are never moved or
renamed. The global archive remains atomic compatibility history, but cannot
override missing database/file evidence or suppress required recovery.

## Occurrences, snapshots, and removals

Music Vault stores every remote playlist-item occurrence by its stable YouTube
playlist-item ID and current position. If one playlist contains the same video
twice, both occurrences remain in source history while the linked local
playlist shows one track at the first current position. The result reports how
many duplicate occurrences were collapsed locally; it never creates duplicate
track rows or media files.

A complete multi-page enumeration is one authoritative snapshot. Only after
every page succeeds may Music Vault mark a prior occurrence removed. A removed
occurrence remains in history; only that source origin is removed from its
managed playlist. Manual origin, another source's membership, the global track,
metadata, artwork, lyrics, and media are preserved.

If enumeration fails or pagination is incomplete, the last known-good source
snapshot and linked playlist order remain untouched. The failed run is recorded
truthfully instead of inferring removals.

Unavailable/deleted/private items may lack a usable video ID. Their occurrence
and sanitized source-item error remain source-specific. A failure or later
success for one source does not clear another source's independent failure for
the same video.

## Sequential synchronization

Multi-source batches run one source at a time in persisted order. Each source
is enumerated, downloaded as needed, imported, identity-mapped, reconciled, and
materialized before the next source starts. This allows later sources in the
same batch to reuse files imported by earlier sources and avoids duplicate
download races.

Results remain truthful per source and in aggregate:

- **Complete** means every attempted source and item completed without issue;
- **Complete with issues** means useful work completed but at least one source
  or item had an issue; and
- **Failed** means no selected source completed useful work.

Stop After Current never hard-cancels yt-dlp or an active media conversion. It
lets the current source reconcile and then returns without starting another
source. Ordinary playback remains available during synchronization.

## Privacy and maintenance

The YouTube Data API key remains only in the private key file and is never
stored in a source definition. App Status exposes aggregate counts and batch
state only—not URLs, labels, titles, IDs, destinations, folders, membership, or
per-item errors. Source activity is bounded and sanitized; raw provider
responses, credentials, authorization headers, cookies, and unrestricted local
paths are not stored.

Settings may report aggregate active/archived source, unresolved-failure, and
identity-conflict counts and may open the configured source-download root. It
does not publish source definitions. Removing a source, clearing failure
history, or reconciling remote removals never deletes personal media.
