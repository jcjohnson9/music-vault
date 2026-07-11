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

Only this README and `.gitkeep` are intended to be tracked.
