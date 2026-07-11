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

When optional artist-photo lookup is used, Music Vault stores its versioned
provenance and negative-result manifest at `artist_images/index.json` and
content-addressed validated image files under `artist_images/files/`. Fetching
is disabled by default, requires no provider or YouTube API key, and can be
disabled or cleared from the application. These images may belong to third
parties; neither the files nor their manifest belongs in source control or a
public build.

Only this README and `.gitkeep` are intended to be tracked.
