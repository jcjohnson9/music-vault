# Data and Privacy

Music Vault is a local-first application. Its runtime state is stored locally
under the project's `data/` directory rather than in the public source
repository.

Depending on which features are used, local runtime data can include:

- a YouTube Data API key;
- the SQLite library database and its sidecar files;
- local configuration and status files;
- synchronization archives and failed-item records;
- downloaded audio and other media;
- extracted or downloaded cover and artist artwork;
- metadata-remediation reports; and
- local backups.

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
