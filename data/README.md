# Local runtime data

Music Vault creates and populates a private data directory locally at runtime.
Source development uses this project directory. A default portable installation
uses `data` beside `MusicVault.exe`, while first-run setup can select another
writable location. A public source checkout and the v1.0.0 portable ZIP both
contain no user library or credentials.

Never commit user databases, API keys, configuration, status files, download
archives, failed-item records, media, cover art, artist images, metadata reports,
backups, logs, lyrics, lyric-provider results, Discogs tokens/results/images,
metadata-intelligence jobs, saved source definitions, source
URLs/labels/titles, source-item memberships, run history, destination mappings,
quality-profile overrides, per-track media-quality observations, or private
playlist information from this directory.

Music Vault may create timestamped SQLite migration backups under `backups/`
and a neutral external App Status document named `music_vault_status.json`.
Both are private runtime data. The status document contains no API-key value and
does not represent a Watchtower integration.

The portable release contains the `music-vault.portable.json` root marker but
does not contain a populated data folder. On first launch, Music Vault creates
only the selected runtime directories and an empty current-schema database.
Local import/playback works without an API key or FFmpeg. Optional YouTube setup keeps
the key in `youtube_api_key.txt`; it is never stored in JSON configuration or
bundled into a release. The command-line FFmpeg tools are installed separately.

Optional Discogs-first metadata intelligence stores a personal credential only
in `discogs_token.txt`. That file is not JSON configuration, is never printed or
placed in App Status, and must never be committed or bundled. Accepted catalogue
fields and private resumable-job evidence live in SQLite. Raw API responses are
memory-only and short-lived. Validated gap-only artwork is content-addressed
under `covers/discogs/`; provider-cache files, images, and attribution metadata
remain private runtime data and are excluded from public artifacts.

Schema version 4 keeps effective metadata, source observations, provenance,
confidence, field locks, and grouped change history inside the private SQLite
database and adds resumable remediation jobs, item snapshots, and a bounded
provider cache. Manual corrections and undo remain database-only. A separately
confirmed high-confidence remediation job may write verified tags to supported
media after creating an exact full-file backup; it must preserve the audio
payload and retain conflict-aware rollback state. Validated local artwork is copied into
content-addressed storage under `covers/manual/`, and explicitly approved
candidate artwork uses a provider-specific directory under `covers/`. These
files and schema-migration backups must never be committed or included in a public
build. Clear, reset, and undo do not automatically delete older cover files.

Batch 11 on the v1.1.0 development line advances new and migrated local
databases to schema version 8. Schema version 5 adds saved
synchronization sources, durable
playlist-item occurrences, source runs, global video-to-track identities,
identity-conflict diagnostics, and manual/source playlist origins. These tables
may reveal private source and library organization and belong only in the local
database. New source downloads use
`youtube_downloads/sources/<stable-storage-key>/`; existing media remains where
it is and is reused rather than moved. Source archive, remote removal, and safe
detachment do not delete media.

Schema version 6 adds structured artists and ordered track credits, normalized
release/label context, original-versus-version dates, version identity,
recording grouping, and resumable metadata-intelligence jobs. It preserves the
materialized artist text, every canonical track, media path, source occurrence,
playlist origin, and order. Labels and YouTube uploaders remain release/source
provenance rather than artist entities. Version grouping never merges or
deletes separate media.

Schema version 7 adds canonical album cards and per-track edition memberships,
artist aliases and verified relationships, and field-level intelligence
outcomes. These are private SQLite records. Album grouping does not rewrite
track album strings, dates, media paths, or `cover_path`; representative card
art is selected from existing member-track art without propagation. Safe artist
consolidation preserves credits, roles, aliases, provider IDs, relationships,
portrait provenance, locks, observations, and history. Conflicting identities
remain separate.

Schema version 8 adds one private `track_media_quality` row per canonical track.
It stores only known acquisition, source, stored-file, transformation,
inspection, provenance, and timestamp facts. Legacy migration may recognize an
existing YouTube MP3 conservatively, but leaves its original source codec and
bitrate unknown. Migration does not open media for writing, move files,
rewrite tags, replace artwork, or start a quality-upgrade job. Existing files
remain the representation reused by overlapping sources.

Stored review evidence may be reclassified locally as `Applied with Gaps`,
`Accepted Source Fallback`, or critical `Needs Review` without a provider
request. Private proposals, reasons, identities, and source/provider references
stay in SQLite. Only aggregate counts may appear in App Status. Canonical artist
portrait files and attribution remain private under `artist_images/`; Discogs
or Wikimedia fallback still requires the existing opt-in, and album artwork is
not used as an artist portrait.

Discogs artwork can fill only a missing, corrupt, or explicit-placeholder gap.
It never automatically replaces a valid existing cover and is never embedded
into an audio file automatically. Text-tag writeback, when separately enabled,
uses the existing verified full-file backup, temporary-copy, readback, and
unchanged-audio checks.

Private remediation reports live under `metadata_reports/<job-id>/`. Provider
cache rows remain in the SQLite database, and per-job original media backups
live under `backups/metadata_jobs/<job-id>/`. Reports, candidate snapshots,
manifests, cache data, generated artwork, and backups may identify a personal
library and must never be committed, attached publicly, or bundled in a build.

When optional artist-photo lookup is used, Music Vault stores its versioned
provenance and negative-result manifest at `artist_images/index.json` and
content-addressed validated image files under `artist_images/files/`. Fetching
is disabled by default, requires no provider or YouTube API key, and can be
disabled or cleared from the application. These images may belong to third
parties; neither the files nor their manifest belongs in source control or a
public build.

Optional Party Mode lyrics use a versioned private cache under `lyrics/`.
Content-addressed `.lyrics` payloads derived from imported or fetched LRC/plain
text, manual imports, the cache index, provider provenance, negative-cache
state, and lookup metadata are runtime-only data. Online lookup is disabled by
default and requires consent; it sends only the current track's title, artist,
optional album, and duration to LRCLIB. It does not send the YouTube API key,
audio, or a bulk library inventory. Fetched lyrics are never written into music
files, App Status, or public logs.

Lyric content may be copyrighted or otherwise subject to third-party rights and
is retained only for the user's private local use. Never commit or publicly
attach the `lyrics/` directory, an adjacent personal sidecar, raw provider
response, or test fixture containing lyric text. Publication and release gates
reject `.lrc`/`.lyrics` payloads, lyric-cache text, and lyric-provider fixture
paths.

Only this README and `.gitkeep` are intended to be tracked here. Never build a
public portable package from an initialized personal data directory.
