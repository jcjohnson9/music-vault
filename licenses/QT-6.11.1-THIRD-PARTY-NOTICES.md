# Qt 6.11.1 third-party notice map

Music Vault ships the open-source PySide6/Shiboken6 and Qt 6.11.1 Core, GUI,
Widgets, Network, SVG, Multimedia, and Image Formats runtime families. It does
not use Qt under a commercial license.

The portable package includes a generated, exact notice set under
`licenses/qt-attrib/`. That directory preserves, byte for byte from
the official 6.11.1 source archives:

- every `qt_attribution.json` outside unshipped examples;
- every license/copyright file referenced by those attribution records;
- each shipped module source archive's complete `LICENSES` directory;
- an index of component names, versions, license identifiers, and original
  attribution paths; and
- `SOURCE_ARCHIVES.json`, recording the exact archive filenames, sizes, and
  SHA-256 digests used to generate the set.

The notice set is intentionally a superset for Qt Base, Qt Multimedia, Qt SVG,
Qt Image Formats, and PySide/Shiboken. This includes the exact libtiff 4.7.1
and libwebp 1.6.0 notices for the shipped TIFF and WebP plugins. Extra
attribution does not change a component's license.
The corresponding unmodified source archives are carried in the companion
`MusicVault-v1.0.0-Source-Compliance.zip`.

The legacy `opengl32sw.dll` Mesa/LLVM software-rendering fallback is explicitly
excluded from this distribution. Qt Multimedia's separately inventoried FFmpeg
shared libraries remain included under LGPL-2.1-or-later; the command-line
`ffmpeg.exe` and `ffprobe.exe` programs are not included.

The portable release also supplies the GNU LGPL-3.0, GNU GPL-3.0, and GNU
LGPL-2.1 texts. This map is factual release documentation, not legal advice.
