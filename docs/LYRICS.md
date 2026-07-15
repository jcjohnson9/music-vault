# Lyrics

Lyrics are an optional Party Mode feature on the v1.1.0 development line.
They are **Off by default**. Enabling the display persists across Party Mode
close/reopen, application restart, and track changes without restarting or
changing playback. The `L` shortcut and the original Lyrics control toggle the
display independently of the auto-hiding transport overlay.

The lyrics panel is a separate overlay directly above the existing playback
bar. It does not move the centered album, title, artist, or controls.
Synchronized lyrics show restrained previous/current/next context and follow
the authoritative player position, including seeks and pauses. Plain lyrics
are labeled **Unsynced Lyrics** and remain an honestly scrollable text view;
Music Vault does not invent line timing.

## Local-first source order

Music Vault resolves the current track in this order:

1. a manually imported lyric file in Music Vault's private cache;
2. an adjacent same-stem `.lrc` file;
3. supported embedded synchronized lyrics, read only through Mutagen;
4. cached synchronized provider lyrics;
5. an adjacent same-stem `.txt` file;
6. supported embedded plain lyrics, read only;
7. cached plain provider lyrics; and
8. an online provider lookup, only when separately enabled.

Adjacent files are inspected only beside the current media file; Music Vault
does not recursively search neighboring folders. Imported `.lrc` and `.txt`
files are validated and copied into the managed cache rather than retained as
references to arbitrary external paths. Neither adjacent files nor embedded
audio tags are changed. Batch 9.1 never writes fetched lyrics into audio files.

## Private cache

Managed lyrics live under `data/lyrics/` in the selected private runtime data
directory. The versioned index records track identity, metadata fingerprint,
source/provenance, timestamps, confidence, and content hashes. Lyric bodies use
content-addressed hashed filenames rather than track titles. Index and content
writes are atomic and bounded.

Successful results load locally on later plays. A no-match or ambiguous result
is negatively cached for about 30 days; a temporary provider/network failure is
cached for about six hours. **Refresh Lyrics** can bypass an eligible negative
cache. A meaningful title, artist, or duration change makes an automatic cache
entry stale without deleting manually imported lyrics. Clearing the automatic
cache does not remove adjacent or embedded lyrics.

The cache is runtime-only third-party/personal data. It is ignored by Git and
rejected by publication, history, portable-build, and source-compliance safety
gates. Do not commit, attach, log, or bundle the index, provider response, `.lrc`
or `.txt` source content, or a generated `.lyrics` payload. Cached lyric content
may be subject to third-party rights and is retained only for the user's private
local use.

## Optional online lookup

Online lookup is a separate setting and is also **Off by default**. If no local
result exists, Music Vault asks for consent before the first provider request.
Choosing **Keep Local Only** leaves the display enabled and makes no request.
Choosing **Enable Online Lyrics** permits read-only lookup of the current track
through the provider-neutral interface; Batch 9.1's provider is the official
HTTPS LRCLIB API.

An enabled lookup may send only the current track's:

- title;
- artist;
- album, when available; and
- duration.

Music Vault sends no YouTube API key, browser cookie, personal media, audio
bytes, playlist, or bulk-library inventory. It performs no bulk lyric download,
contribution, or upload. Requests are limited to `lrclib.net`, use bounded
timeouts and responses, and reject HTTP, credentials in URLs, redirects or DNS
answers to private/local destinations, malformed JSON, and ambiguous or weak
matches. Errors shown to the user are sanitized. Provider results display
restrained attribution such as **Lyrics via LRCLIB**; Music Vault does not claim
authorship.

## Controls and settings

The Lyrics control supports refresh, validated `.lrc`/`.txt` import, clearing
the automatic cache for the current track, and opening Lyrics Settings. Clearing
automatic content preserves adjacent, embedded, and manual sources. Manually
imported cache content can be removed through private cache maintenance outside
Music Vault. The Party Mode settings show
the display and online-lookup toggles, LRCLIB provider identity, private cache
location and size, and bounded clear/open-folder actions. They do not store an
API key. Plain-lyrics scroll position is session-only and resets on track
change.

## Privacy boundary

Lyric text, provider queries, provider result identifiers, cached lyric paths,
and raw provider/network errors never enter App Status or public logs. App
Status may expose only meaningful boolean availability/synchronization state.
It is not rewritten for every displayed line. Lyrics lookup does not use the
YouTube API key and does not change the database schema, audio files, playback,
queue, base context, volume, Auto/Shuffle, or Repeat behavior.
