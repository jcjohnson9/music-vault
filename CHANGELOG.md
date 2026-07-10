# Changelog

Notable changes to Music Vault will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

This section is reserved for work after the public source-control baseline,
including the planned V1 trust, synchronization-correctness, and safety work.

## 1.0.0-rc.1 - Unreleased RC baseline

### Established

- Local SQLite music library and Windows playback
- Authorized public or unlisted source-playlist synchronization
- Full YouTube Data API pagination for large playlists
- Incremental acquisition based on stable video IDs
- yt-dlp and FFmpeg media acquisition workflow
- Embedded and downloaded artwork support
- Albums, artists, and custom local playlists
- Temporary FIFO queue with original base-context resume
- Local settings and configuration
- Hardened source and packaged-application data paths
- PyInstaller one-folder Windows EXE workflow
- Developer verification, build, launch, and shortcut tooling

### Known release-candidate limitations

- A YouTube source upload date may appear as a track's release year.
- Partial synchronization failures may be reported inaccurately.
- Manual metadata correction is not yet complete.
- A clean, blank public distribution has not yet been published.
- The interface still requires its planned premium overhaul.
