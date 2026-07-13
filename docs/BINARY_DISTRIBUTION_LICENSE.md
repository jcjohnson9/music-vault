# Binary distribution license

Music Vault source written for this repository remains available under the
[MIT License](../LICENSE). That source license does not erase the licenses of
third-party code combined into the Windows portable executable.

## Portable binary conclusion

The v1.0.0 one-folder portable build embeds Mutagen 1.47.0, licensed
GPL-2.0-or-later. Music Vault's MIT code is GPL-compatible, so the combined
portable executable is conveyed under GPL-3.0-or-later while each component
retains its own notices and additional permissions. PyInstaller's bootloader
exception continues to apply.

PySide6, Shiboken, and Qt libraries are used under their LGPL-3.0 option. The
Qt Multimedia backend includes FFmpeg 7.1.3 shared libraries built without
`--enable-gpl`; those libraries are used under LGPL-2.1-or-later. They remain
separate DLLs in `_internal` so a recipient can inspect or replace compatible
library files. The release imposes no restriction on reverse engineering for
debugging modifications to LGPL-covered components.

The FFmpeg record also preserves its exact BSD-3-Clause, BSD-2-Clause,
BSD-Source-Code, ISC, MIT, and MPL-2.0 notice expression. Component-specific
BSD-Source-Code and ISC notices are reproduced from the hash-pinned 7.1.3
source, and the exact libjpeg-, Boost-, and zlib-derived code records are listed
separately. The generated Qt attribution set likewise preserves singular and
plural license files and referenced copyright files from the official source
archives.

The default package does **not** contain `ffmpeg.exe` or `ffprobe.exe`. Those
optional command-line tools must be installed/configured separately for
synchronization and conversion features.

## Corresponding source and relinking

Every binary release is accompanied by
`MusicVault-v1.0.0-Source-Compliance.zip`. It contains the exact tagged Music
Vault source and build inputs, dependency lock, license inventory, license
texts, and the unmodified hash-pinned corresponding-source archives for every
bundled FOSS runtime component, including permissively licensed dependencies
that form part of the GPL combined work. The archive is generated from the stated Git
commit rather than from an unchecked working tree.
The one-folder form deliberately keeps the LGPL DLLs outside the executable;
the compliance instructions describe replacement and rebuilding.

The corrective v1.0.0 publication keeps application source and build inputs at
the existing annotated `v1.0.0` tag. Later release tooling may fetch and verify
the corresponding-source set, so the release manifest records the tagged
application commit and release-tooling commit separately. In particular, zlib
1.3.1 is identified by the official versioned fossil archive at
`https://zlib.net/fossils/zlib-1.3.1.tar.gz`, SHA-256
`9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23`.
The retrieval gate validates its transport metadata, gzip/tar structure, safe
member paths and links, `zlib-1.3.1` root, required source and license files,
and internal version declaration. An offline source cache never bypasses those
checks.

The first automated publication attempt failed closed because the bytes
returned for that source did not match the established pin. The same official
fossil currently matches the documented size and hash, so the pin itself was
not replaced. The former one-shot downloader retained neither the received
digest nor final response metadata; consequently, the historical transport
subtype (for example, an incomplete or intermediary response) cannot be
distinguished after the fact. The corrective downloader emits sanitized
mismatch diagnostics, retries the fossil within fixed bounds, and permits only
an equally verified official upstream fallback that yields identical bytes.

The Microsoft Visual C++ runtime DLLs are treated as Windows compiler/runtime
System Libraries under GPL section 1 and remain subject to Microsoft's own
redistribution terms; they are not represented as open-source components.

The machine-readable inventory is
[`tools/release/third_party_licenses.json`](../tools/release/third_party_licenses.json).
The human-readable portable notice is
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

This document records the distribution approach used for this release. It is
not legal advice and does not grant rights to music, artwork, APIs, or other
user-supplied content.
