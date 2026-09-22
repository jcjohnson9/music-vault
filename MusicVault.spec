# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from importlib.metadata import version as distribution_version
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
from music_vault.core.acquisition_runtime import ACQUISITION_PINS, acquisition_readiness


acquisition = acquisition_readiness(verify_integrity=True)
if not acquisition.ready:
    raise RuntimeError(f"Acquisition build preflight failed: {acquisition.error_code}")
acquisition_datas = collect_data_files('yt_dlp_ejs', includes=['**/*.js'])
for dependency in ACQUISITION_PINS:
    # Distribution versions and license files remain inspectable when frozen.
    acquisition_datas += copy_metadata(dependency)

# PyWinRT namespaces load projection extensions dynamically. Keep the exact
# native closure proven by the Windows transport gate, not unrelated namespaces.
transport_distributions = {
    'winrt-runtime': '3.2.1', 'winrt-Windows.Foundation': '3.2.1',
    'winrt-Windows.Foundation.Collections': '3.2.1', 'winrt-Windows.Media': '3.2.1',
    'winrt-Windows.Media.Interop': '3.2.1', 'winrt-Windows.Storage.Streams': '3.2.1',
    'typing_extensions': '4.16.0',
}
transport_imports = [
    'winrt.runtime', 'winrt.system', 'winrt.windows.foundation',
    'winrt.windows.foundation.collections', 'winrt.windows.media',
    'winrt.windows.media.interop', 'winrt.windows.storage.streams',
    'winrt._winrt', 'winrt._winrt_windows_foundation',
    'winrt._winrt_windows_foundation_collections', 'winrt._winrt_windows_media',
    'winrt._winrt_windows_media_interop', 'winrt._winrt_windows_storage_streams',
]
transport_datas = []
for dependency, expected_version in transport_distributions.items():
    if distribution_version(dependency) != expected_version:
        raise RuntimeError(f"Native transport build preflight failed: {dependency} version")
    transport_datas += copy_metadata(dependency)
# The projection wheels omit their MIT text, so metadata alone is insufficient.
transport_datas += [
    ('licenses/PYWINRT-3.2.1-MIT.txt', 'licenses'),
    ('licenses/TYPING-EXTENSIONS-4.16.0-LICENSE.txt', 'licenses'),
]


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
    binaries=[(str(acquisition.runtime_path), 'acquisition')],
    datas=[('assets', 'assets'), *acquisition_datas, *transport_datas],
    hiddenimports=[
        'yt_dlp',
        'mutagen.id3',
        'mutagen.flac',
        'musicbrainzngs',
        'music_vault.metadata.providers.discogs',
        'music_vault.metadata.discogs_artwork',
        'tools.dev.verify_acquisition',
    ] + collect_submodules('yt_dlp_ejs') + transport_imports,
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
    upx_exclude=['deno.exe'],
    name='MusicVault',
)
