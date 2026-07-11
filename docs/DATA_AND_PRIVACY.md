# Data and Privacy

Music Vault is a local-first application. Its runtime state is stored locally
under the project's `data/` directory rather than in the public source
repository.

Depending on which features are used, local runtime data can include:

- a YouTube Data API key;
- the SQLite library database and its sidecar files;
- local configuration and status files;
- synchronization archive history and structured failed-item records;
- downloaded audio and other media;
- extracted or downloaded cover and artist artwork;
- metadata-remediation reports; and
- local backups.

Before an existing non-empty database is upgraded to a newer schema, Music
Vault uses SQLite's backup API to create a timestamped copy under
`data/backups/`. Backups are private runtime data and remain ignored by Git.

The generic `data/music_vault_status.json` App Status file contains operational
counts, paths, playback state, and the latest sanitized synchronization result.
It does not contain the YouTube API key. Music Vault has no Watchtower
relationship or integration.

Synchronization supports public and unlisted playlists and performs anonymous
media extraction. It does not silently read Firefox, Chrome, Edge, or other
browser cookie profiles.

These categories can contain credentials, private library information,
personal playlist information, local paths, and copyrighted media. They are
private runtime data and are ignored by Git. They must not be added to commits,
issues, pull requests, release archives, or public logs.

A source checkout does not include a user's music library, credentials, media,
artwork, synchronization state, or private reports. The public repository is
source-only.

Runtime files should not be deleted casually. Deleting the database or related
state may destroy library organization, playlists, metadata, or synchronization
history. Deleting downloaded media or artwork can also leave library records
incomplete. Back up the local `data/` directory before future metadata
remediation, database-schema work, or other maintenance that may alter runtime
state.

A later blank release package will bootstrap empty runtime data on first use.
It will not contain a maintainer's or another user's library.
