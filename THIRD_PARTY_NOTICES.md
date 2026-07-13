# Music Vault portable binary: third-party notices

Music Vault's own source files remain licensed under the repository's MIT
License. The portable Windows application is a combined distribution that also
contains third-party software under other licenses. It is not an MIT-only binary.

The combined executable is distributed under GPL-3.0-or-later because it embeds
Mutagen (GPL-2.0-or-later), with the PyInstaller bootloader exception and the
separate terms of every other bundled component preserved. Qt/PySide and the
FFmpeg libraries used internally by Qt Multimedia are dynamically replaceable
one-folder components under LGPL terms. Music Vault does not bundle the
`ffmpeg.exe` or `ffprobe.exe` command-line tools.

Exact component versions, license identifiers, artifact mappings, license-text
locations, and source locations are recorded in
`tools/release/third_party_licenses.json`. The portable package includes the
applicable texts in its `licenses/` directory, and the companion source-
compliance archive contains the exact tagged Music Vault source, build materials,
license inventory, and hash-pinned unmodified corresponding-source archives.

The fail-closed inventory contains 74 exact component/version records. It covers
CPython and its native dependencies; PySide6/Qt/Shiboken; every binary/source-
proven embedded Qt algorithm, data set, header-derived component, image codec,
and multimedia dependency; Mutagen; yt-dlp; musicbrainzngs; Requests and its
bundled dependencies; PyInstaller's runtime; OpenSSL and SQLite; the Qt
Multimedia FFmpeg 7.1.3 libraries and their copied-code notices; and Microsoft
redistributable runtime files. The exact list is intentionally machine-readable
rather than duplicated incompletely here. Copyright and trademark rights remain
with their respective owners.

The companion compliance archive carries exact hash-pinned source for every
bundled FOSS runtime component. Microsoft runtime DLLs remain subject to their
redistribution terms and are treated as compiler/runtime System Libraries.

For the corrective v1.0.0 publication, corresponding-source retrieval remains
fail closed: every archive is constrained to declared authoritative HTTPS
origins, a pinned hash, response bounds, expected archive format, and safe
member/link structure. Component-specific internal checks are enforced where
the audited inventory declares them; the zlib 1.3.1 record additionally checks
its exact root, required implementation/header/license files, internal version,
and license identity. Cached archives undergo the same applicable validation.
The release manifest distinguishes the immutable tagged application source
from the later corrective release-tooling source.

This notice states the release's factual packaging and licensing basis; it is
not legal advice.
