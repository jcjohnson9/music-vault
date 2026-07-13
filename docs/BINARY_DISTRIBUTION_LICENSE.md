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
