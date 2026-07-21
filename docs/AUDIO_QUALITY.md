# Audio Quality Profiles

Batch 11 is complete on the Music Vault v1.1.0 development line. The latest
public stable release remains v1.0.0.

## Profiles

### Best Original — Recommended

`best_original` is the default for future authorized downloads. It selects the
best useful supported source audio stream and retains that stream's audio codec.
Music Vault avoids lossy re-encoding and remuxes only when a practical
audio-only container is needed.

Typical outcomes include:

- Opus stored as `.opus`;
- AAC stored as `.m4a`;
- Vorbis stored as `.ogg`; and
- MP3 retained as MP3 when it is the selected source stream.

A container-only remux is not an audio-quality upgrade. YouTube audio is not
described as lossless merely because its source codec was retained. Music Vault
does not convert YouTube audio to FLAC or WAV, invent Hi-Res audio, upscale its
sample rate, or increase its bit depth.

### MP3 320 Compatibility

`mp3_320_compatibility` selects the best useful source audio and converts it to
MP3 at 320 kbps for broad device and software compatibility. The conversion is
recorded as a lossy compatibility transcode. It cannot improve the fidelity of
the source and may use more storage.

Music Vault excludes an already-MP3 input from this profile because yt-dlp
would otherwise stream-copy it while falsely implying that a 320 kbps
transcode occurred. If no supported non-MP3 source is available, the item
fails closed; Best Original can still preserve a native MP3 source unchanged.

The compatibility profile is an explicit alternative, not the recommended
default. Changing either global profile does not start synchronization,
redownload media, or replace an existing file.

## Saved-source override

Each saved source can choose:

- **Use Global Setting** (`inherit`);
- **Best Original** (`best_original`); or
- **MP3 320 Compatibility** (`mp3_320_compatibility`).

The override applies only when that source must acquire a future missing track.
It does not change the source's identity, destination, membership, or history.

## Selection, verification, and provenance

Music Vault chooses one supported audio stream from yt-dlp's ranked format
facts rather than relying on a hardcoded format ID. DRM, no-audio, unsupported
codec, and unsuitable oversized candidates fail closed. A muxed source is a
fallback only when no usable audio-only stream exists and its audio can be
extracted without lossy re-encoding.

The final path is derived from deterministic downloader evidence and must stay
inside its intended source directory. The stored file must exist, be complete,
contain one supported audio stream and no video stream, and match the expected
source identity. Best Original also requires the inspected stored codec to
match the selected source codec; an unexpected codec change is a quality
failure and is not imported or marked successfully archived.

Schema 8 stores private per-track quality provenance in
`track_media_quality`, including known source and stored format, codec, bitrate,
sample rate, channels, size, acquisition profile, transformation, inspection
state, and timestamps. Unknown values remain unknown rather than appearing as
zero. Existing YouTube MP3 files may be identified conservatively as legacy
inferred transcodes, but migration does not invent their original source codec
or bitrate.

## One canonical track and one media file

One YouTube video continues to map to one canonical Music Vault track, one
media file, and any number of saved-source memberships. The first successful
acquisition determines the stored representation. A later source requesting a
different profile reuses that representation and reports its actual stored
facts; it does not create a second quality variant.

Removing or changing a source never deletes the media or erases another source
or manual playlist origin.

## Import, playback, artwork, and tags

The library retains import/playback support for MP3, M4A/AAC, FLAC, WAV, Ogg
Vorbis, and Opus. WebM is accepted only after read-only inspection confirms that
it has audio and no video. These formats use the existing Qt Multimedia player,
queue, playback context, transport controls, and Party Mode behavior.

Database metadata and Music Vault's private cover reference remain authoritative
when a native source-preserved container cannot safely embed artwork. A valid
audio file does not fail merely because embedded artwork is unavailable.

The verified automatic tag-writeback pipeline remains MP3-focused. Batch 11
does not claim broad safe M4A, Opus, AAC, Ogg, or WebM tag mutation. Any future
extension must meet the existing full-file backup, temporary-copy, readback,
unchanged-audio-payload, and conflict-aware rollback requirements.

## Existing library and privacy

Existing personal media remains in place and is not automatically redownloaded,
replaced, converted, renamed, retagged, or upgraded. An optional local quality
refresh may inspect supported media read-only, but it does not run at startup
and performs no provider request.

Quality inventory, saved-source overrides, and inspection evidence are private
runtime data. They are never bundled in a release. The download workflow does
not read browser cookies, and profile selection adds no new credential.

Remaining metadata-polish items are deferred; they are not Batch 11 blockers.
