# Local runtime data

Music Vault creates and populates this directory locally at runtime. A public
source checkout intentionally contains no user library or credentials.

Never commit user databases, API keys, configuration, status files, download
archives, failed-item records, media, cover art, artist images, metadata reports,
backups, logs, or private playlist information from this directory.

Music Vault may create timestamped SQLite migration backups under `backups/`
and a neutral external App Status document named `music_vault_status.json`.
Both are private runtime data. The status document contains no API-key value and
does not represent a Watchtower integration.

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

Only this README and `.gitkeep` are intended to be tracked.
