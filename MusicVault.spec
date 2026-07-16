# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

from music_vault.version import (
    APP_NAME,
    APP_VERSION,
    ORIGINAL_FILENAME,
    PUBLISHER,
    WINDOWS_VERSION,
)


windows_version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=WINDOWS_VERSION,
        prodvers=WINDOWS_VERSION,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", PUBLISHER),
                        StringStruct("FileDescription", APP_NAME),
                        StringStruct("FileVersion", f"{APP_VERSION}.0"),
                        StringStruct("InternalName", "MusicVault"),
                        StringStruct("OriginalFilename", ORIGINAL_FILENAME),
                        StringStruct("ProductName", APP_NAME),
                        StringStruct("ProductVersion", f"{APP_VERSION}.0"),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=[
        'yt_dlp',
        'mutagen.id3',
        'mutagen.flac',
        'musicbrainzngs',
        'music_vault.metadata.providers.discogs',
        'music_vault.metadata.discogs_artwork',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# PyInstaller's broad Qt plugin collection pulls PDF/QML/Quick/Virtual Keyboard
# runtimes into an otherwise Widgets/Multimedia application. Music Vault does
# not import those modules or use their two plugins, so keep the public binary's
# dependency and license surface aligned with the application. The legacy Mesa
# software-OpenGL fallback is excluded: Qt 6 uses the Windows graphics stack for
# this Widgets application, and the old Mesa/LLVM binary has a disproportionate
# and ambiguous redistribution surface.
_unused_qt_runtime_prefixes = (
    "pyside6\\qt6pdf",
    "pyside6\\qt6qml",
    "pyside6\\qt6quick",
    "pyside6\\qt6virtualkeyboard",
)
_unused_qt_plugin_paths = {
    "pyside6\\plugins\\imageformats\\qpdf.dll",
    "pyside6\\plugins\\platforminputcontexts\\qtvirtualkeyboardplugin.dll",
}

_unused_native_names = {
    # Supported releases run on Windows 10/11, which provide the Universal CRT.
    # Excluding AppLocal copies also prevents PyInstaller from borrowing these
    # DLLs from an unrelated application found on PATH.
    "ucrtbase.dll",
}


def _keep_qt_runtime(entry):
    destination = str(entry[0]).replace("/", "\\").casefold()
    return not (
        destination.startswith(_unused_qt_runtime_prefixes)
        or destination in _unused_qt_plugin_paths
        or destination == "pyside6\\opengl32sw.dll"
        or destination in _unused_native_names
        or destination.startswith("api-ms-win-")
    )


a.binaries = [entry for entry in a.binaries if _keep_qt_runtime(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MusicVault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icons\\music_vault.ico'],
    version=windows_version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MusicVault',
)
