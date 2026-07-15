from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.version import APP_VERSION, RELEASE_CHANNEL  # noqa: E402


PRODUCT_NAME = "Music Vault"
RELEASE_LICENSE_INVENTORY_PATH = "tools/release/third_party_licenses.json"
PORTABLE_MARKER = "music-vault.portable.json"
PORTABLE_MARKER_VERSION = 1
PACKAGE_DIRECTORY = f"MusicVault-v{APP_VERSION}-Windows-x64-Portable"
PACKAGE_FILENAME = f"{PACKAGE_DIRECTORY}.zip"
COMPLIANCE_FILENAME = f"MusicVault-v{APP_VERSION}-Source-Compliance.zip"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
MEDIA_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm", ".wma"
}
DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
ARCHIVE_EXTENSIONS = {".7z", ".rar", ".tar", ".tgz", ".gz", ".zip"}
ALLOWED_RUNTIME_ARCHIVES = {"_internal/base_library.zip"}
FORBIDDEN_EXACT_NAMES = {
    "youtube_api_key.txt",
    "music_vault_config.json",
    "music_vault_status.json",
    "youtube_failed_ids.txt",
    "youtube_download_archive.txt",
}
FORBIDDEN_PARTS = {
    ".git", ".github", ".venv", ".codex", ".agents", "__pycache__",
    "build", "data", "dist", "release_artifacts", "metadata_reports", "provider_cache",
    "media_backups", "youtube_downloads", "artist_images", "covers", "backups",
}
LYRIC_CACHE_PARTS = {"lyric_cache", "lyrics_cache"}
LYRIC_FIXTURE_PARTS = {
    "lyric_fixtures",
    "lyrics_fixtures",
    "lyrics_provider_fixtures",
    "provider_fixtures",
    "provider_lyrics_fixtures",
}
SECRET_PATTERNS = (
    ("likely Google API key", re.compile(rb"AIza[0-9A-Za-z_-]{30,}")),
    ("bearer token", re.compile(rb"(?i)bearer[ \t]+[A-Za-z0-9._~+/-]{20,}")),
    # Qt's TLS backends contain PEM *format labels* as parser constants. A
    # credential requires a header plus a substantive encoded body; requiring
    # both preserves the secret gate without treating a parser string as a key.
    (
        "private key",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n"
            rb"(?:[A-Za-z0-9+/]{32,}={0,2}\r?\n){2,}"
        ),
    ),
)
PERSONAL_PATH_PATTERNS = (
    re.compile(rb"(?i)[A-Z]:\\Users\\[^\\\r\n]+"),
    re.compile(rb"(?i)[A-Z]:/Users/[^/\r\n]+"),
    # Split the POSIX prefix so this scanner does not match its own source.
    re.compile(rb"(?i)/" + rb"home/[^/\r\n]+"),
)
QT_VENDOR_BUILD_PATH_PATTERNS = (
    re.compile(rb"(?i)[A-Z]:\\Users\\qt(?:\\|$)"),
    re.compile(rb"(?i)[A-Z]:/Users/qt(?:/|$)"),
)

# This is the reviewed Batch 8 overlay for components proven present in the
# final PyInstaller tree, including statically embedded native dependencies.
# It prevents a self-consistent but incomplete inventory from passing merely
# because both the builder and verifier read the same omissions.
AUDITED_BUNDLED_COMPONENT_VERSIONS = {
    "CPython runtime": "3.11.9",
    "bzip2/libbzip2 used by CPython": "1.0.8",
    "XZ/liblzma used by CPython": "5.2.5",
    "libmpdec used by CPython": "2.5.1",
    "Expat used by CPython": "2.6.0",
    "libffi used by CPython": "3.4.4",
    "PySide6, PySide6 Essentials/Addons, and Shiboken6": "6.11.1",
    "CPython 3.7 buffer-protocol code used by Shiboken6": "3.7.0",
    "CPython 3.7 signature support code embedded by Shiboken6": "3.7.0",
    "Qt libraries and plugins": "6.11.1",
    "libtiff statically linked into the Qt TIFF plugin": "4.7.1",
    "libwebp statically linked into the Qt WebP plugin": "1.6.0",
    "PCRE2 statically linked into Qt Core": "10.47",
    "FreeType statically linked into Qt GUI": "2.14.3",
    "HarfBuzz statically linked into Qt GUI": "14.2.0",
    "libpng statically linked into Qt GUI": "1.6.58",
    "libjpeg-turbo statically linked into the Qt JPEG plugin": "3.1.4",
    "TLSF statically linked into Qt Multimedia": "3.1",
    "TinyCBOR statically linked into Qt Core": "7.0",
    "BLAKE2 reference implementation in Qt Core": "ed1974ea83433eba7b2d95c5dcd9ac33cb847913",
    "MD4 implementation in Qt Core": "not stated",
    "MD5 implementation in Qt Core": "not stated",
    "SHA-1 implementation in Qt Core": "not stated",
    "SHA-3 brg_endian in Qt Core": "1.0.0",
    "SHA-3 Keccak in Qt Core": "3.2",
    "RFC6234 SHA-384/SHA-512 in Qt Core": "not stated",
    "SipHash implementation in Qt Core": "not stated",
    "Easing equations in Qt Core": "not stated",
    "double-conversion statically linked into Qt Core": "3.4.0",
    "Apache Tika MIME definitions embedded in Qt Core": "408c26e1e03e018a623e732dff6fb047a2fb8e19",
    "Unicode Character Database in Qt Core": "36",
    "Unicode CLDR data in Qt Core": "v48.1",
    "Emoji Segmenter in Qt GUI": "0.4.0",
    "MD4C in Qt GUI": "0.5.2",
    "D3D12 Memory Allocator in Qt GUI": "f128d39b7a95b4235bd228d231646278dc6c24b2",
    "Vulkan Memory Allocator in Qt GUI": "3.2.1",
    "Mipmap generator for D3D12 in Qt GUI": "0aa79bad78992da0b6a8279ddb9002c1753cb849",
    "sRGB color profile ICC file in Qt GUI": "not stated",
    "Adobe Glyph List For New Fonts in Qt GUI": "1.7",
    "Anti-aliasing rasterizer from FreeType 2 in Qt GUI": "not stated",
    "Smooth Scaling Algorithm in Qt GUI": "not stated",
    "X Server helper in Qt GUI": "not stated",
    "FreeType BDF support in Qt GUI": "not stated",
    "FreeType PCF support in Qt GUI": "not stated",
    "FreeType zlib/gzip code in Qt GUI": "not stated",
    "Vulkan API Registry used by Qt GUI": "1.4.308",
    "WebGradients in Qt GUI": "not stated",
    "OpenGL Headers used by Qt OpenGL": "Revision 27684",
    "libpsl lookup code in Qt Network": "664f3dc85259ec65e30248a61fa1c45b7b0e4c3f",
    "Public Suffix List embedded in Qt Network": "2026-01-16_13-11-47_UTC",
    "Wintab API in Qt Windows plugins": "not stated",
    "tl::expected used by Qt Multimedia": "41d3e1f48d682992a2230b2a715bca38b848b269",
    "DR Libs/dr_wav in Qt Multimedia": "0.14.5",
    "Signalsmith Stretch in Qt FFmpeg backend": "1.0.0",
    "XSVG arc-handling code in Qt SVG": "not stated",
    "libjpeg-derived DCT code in FFmpeg": "not stated",
    "Boost math algorithms in FFmpeg": "not stated",
    "zlib Adler-32 code in FFmpeg": "not stated",
    "FFmpeg shared libraries used by Qt Multimedia": "7.1.3",
    "zlib 1.3.1 used by CPython and Qt Multimedia FFmpeg": "1.3.1",
    "zlib 1.3.2 used by PyInstaller and Qt": "1.3.2",
    "Mutagen": "1.47.0",
    "musicbrainzngs": "0.7.1",
    "Requests": "2.34.2",
    "Certifi": "2026.6.17",
    "Charset Normalizer": "3.4.7",
    "IDNA": "3.18",
    "urllib3": "2.7.0",
    "yt-dlp": "2026.6.9",
    "PyInstaller bootloader/runtime": "6.21.0",
    "OpenSSL libraries from CPython": "3.0.13",
    "SQLite": "3.45.1",
    "Microsoft Visual C++ runtime for CPython": "14.38.33126.1",
    "Microsoft Visual C++ runtime for PySide6 and Shiboken6": "14.44.35211.0",
}
AUDITED_SOURCE_ARCHIVES = {
    "Python-3.11.9.tar.xz",
    "bzip2-1.0.8.tar.gz",
    "xz-v5.2.5-source.tar.gz",
    "libffi-3.4.4.tar.gz",
    "pyside-setup-everywhere-src-6.11.1.tar.xz",
    "qtbase-everywhere-src-6.11.1.tar.xz",
    "qtmultimedia-everywhere-src-6.11.1.tar.xz",
    "qtsvg-everywhere-src-6.11.1.tar.xz",
    "qtimageformats-everywhere-src-6.11.1.tar.xz",
    "ffmpeg-7.1.3.tar.xz",
    "zlib-1.3.1.tar.gz",
    "mutagen-1.47.0.tar.gz",
    "musicbrainzngs-0.7.1.tar.gz",
    "requests-2.34.2.tar.gz",
    "certifi-2026.6.17.tar.gz",
    "charset_normalizer-3.4.7.tar.gz",
    "idna-3.18.tar.gz",
    "urllib3-2.7.0.tar.gz",
    "yt_dlp-2026.6.9.tar.gz",
    "pyinstaller-6.21.0.tar.gz",
    "openssl-3.0.13.tar.gz",
    "sqlite-src-3450100.zip",
}


class ReleaseError(RuntimeError):
    pass


def validate_release_version(version: str) -> str:
    """Return a canonical numeric release version or fail closed."""
    value = str(version).strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ReleaseError("Release version must use numeric major.minor.patch form.")
    return value


def package_directory_for(version: str) -> str:
    return f"MusicVault-v{validate_release_version(version)}-Windows-x64-Portable"


def package_filename_for(version: str) -> str:
    return f"{package_directory_for(version)}.zip"


def compliance_filename_for(version: str) -> str:
    return f"MusicVault-v{validate_release_version(version)}-Source-Compliance.zip"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def safe_files(root: Path) -> list[Path]:
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise ReleaseError(f"Directory does not exist: {root}")
    if is_reparse_or_link(root):
        raise ReleaseError(f"Root directory may not be a symlink or reparse point: {root}")
    files: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or is_reparse_or_link(path):
                    raise ReleaseError(
                        f"Symlink or reparse point is not allowed: {path.relative_to(root)}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
    return sorted(files, key=lambda value: value.relative_to(root).as_posix().casefold())


def validate_zip_name(name: str) -> PurePosixPath:
    if not name or "\\" in name or any(ord(character) < 32 for character in name):
        raise ReleaseError(f"Invalid ZIP entry: {name!r}")
    raw_name = name[:-1] if name.endswith("/") else name
    raw_parts = raw_name.split("/")
    if not raw_name or any(part in {"", ".", ".."} for part in raw_parts):
        raise ReleaseError(f"Unsafe ZIP entry: {name!r}")
    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    for part in raw_parts:
        # Windows treats colons as alternate-data-stream separators and silently
        # normalizes trailing dots/spaces and DOS device names. Reject all three
        # so verification and extraction cannot disagree about the destination.
        if ":" in part or part.endswith((".", " ")):
            raise ReleaseError(f"Windows-unsafe ZIP entry: {name!r}")
        if part.split(".", 1)[0].upper() in reserved_names:
            raise ReleaseError(f"Reserved Windows ZIP entry: {name!r}")
    value = PurePosixPath(raw_name)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise ReleaseError(f"Unsafe ZIP entry: {name!r}")
    return value


def deterministic_zip(source_root: Path, destination: Path, *, prefix: str | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    prefix_path = PurePosixPath(prefix) if prefix else None
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True
    ) as archive:
        for path in safe_files(source_root):
            relative = PurePosixPath(path.relative_to(source_root).as_posix())
            arcname = (prefix_path / relative).as_posix() if prefix_path else relative.as_posix()
            validate_zip_name(arcname)
            info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(block)


def exact_requirements(path: Path | None = None) -> dict[str, str]:
    requirements = path or PROJECT_ROOT / "requirements-release.txt"
    result: dict[str, str] = {}
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise ReleaseError(f"Release requirement is not exact: {line}")
        name, version = line.split("==", 1)
        if not name.strip() or not version.strip():
            raise ReleaseError(f"Invalid release requirement: {line}")
        result[name.strip()] = version.strip()
    return result


def git_value_at(repository_root: Path, *args: str) -> str:
    repository_root = repository_root.expanduser().resolve()
    command = [
        "git", "-c", f"safe.directory={repository_root.as_posix()}", *args
    ]
    completed = subprocess.run(
        command,
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise ReleaseError("Git release metadata could not be resolved.")
    return completed.stdout.strip()


def git_value(*args: str) -> str:
    return git_value_at(PROJECT_ROOT, *args)


def git_blob_sha1_file(path: Path) -> str:
    path = path.expanduser().resolve()
    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def git_tree_entries_at(
    repository_root: Path, commit: str
) -> list[tuple[str, str, str, str]]:
    repository_root = repository_root.expanduser().resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("Git tree commit identity is invalid.")
    command = [
        "git",
        "-c",
        f"safe.directory={repository_root.as_posix()}",
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
    ]
    completed = subprocess.run(
        command,
        cwd=repository_root,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode:
        raise ReleaseError("Git source tree entries could not be resolved.")
    entries: list[tuple[str, str, str, str]] = []
    try:
        for raw in completed.stdout.split(b"\0"):
            if not raw:
                continue
            metadata, separator, encoded_path = raw.partition(b"\t")
            if not separator:
                raise ValueError
            mode, kind, object_id = metadata.decode("ascii").split()
            relative = encoded_path.decode("utf-8")
            entries.append((mode, kind, object_id, relative))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReleaseError("Git source tree contains an unsupported entry.") from exc
    return entries


def canonical_file_records(root: Path, *, excluded: Iterable[str] = ()) -> list[dict[str, object]]:
    excluded_set = {value.replace("\\", "/") for value in excluded}
    records: list[dict[str, object]] = []
    for path in safe_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in excluded_set:
            continue
        records.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def canonical_payload_hash(records: list[dict[str, object]]) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def is_lyrics_payload_path(relative: str) -> bool:
    """Identify private lyric content without rejecting lyrics source code/docs."""

    normalized = relative.replace("\\", "/")
    path = PurePosixPath(normalized)
    lowered_parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if suffix in {".lrc", ".lyrics"}:
        return True
    if lowered_parts & (LYRIC_CACHE_PARTS | LYRIC_FIXTURE_PARTS):
        return True
    fixture_markers = ("fixture", "payload", "response")
    if suffix in {".json", ".txt"} and (
        ("lrclib" in name and any(marker in name for marker in fixture_markers))
        or ("lyrics" in name and any(marker in name for marker in fixture_markers))
    ):
        return True
    if suffix != ".txt":
        return False
    return "lyrics" in lowered_parts or name in {"lyric.txt", "lyrics.txt"} or name.endswith(
        (".lyrics.txt", "-lyrics.txt", "_lyrics.txt")
    )


def violation_for_path(relative: str, *, allow_package_zip: bool = False) -> str | None:
    normalized = relative.replace("\\", "/")
    path = PurePosixPath(normalized)
    lowered_parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name in FORBIDDEN_EXACT_NAMES:
        return "runtime or secret filename"
    if is_lyrics_payload_path(normalized):
        return "lyrics cache or provider-fixture payload"
    if suffix in MEDIA_EXTENSIONS:
        return "media file"
    if suffix in DATABASE_EXTENSIONS or name.endswith((".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")):
        return "database or sidecar"
    if (
        suffix in ARCHIVE_EXTENSIONS
        and not allow_package_zip
        and normalized.casefold() not in ALLOWED_RUNTIME_ARCHIVES
    ):
        return "unexpected nested archive"
    if name in {"ffmpeg.exe", "ffprobe.exe"}:
        return "FFmpeg command-line binary"
    if lowered_parts & {value.casefold() for value in FORBIDDEN_PARTS}:
        return "private/development directory"
    if re.search(r"(?i)(screenshot|ui-review).+\.(png|jpg|jpeg)$", name):
        return "review screenshot"
    return None


def _scan_blocks(handle, *, allow_qt_vendor_path: bool) -> list[str]:
    findings: list[str] = []
    overlap = 16 * 1024
    tail = b""
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        data = tail + block
        for label, pattern in SECRET_PATTERNS:
            if label not in findings and pattern.search(data):
                findings.append(label)
        if "personal absolute path" not in findings:
            for pattern in PERSONAL_PATH_PATTERNS:
                matches = list(pattern.finditer(data))
                if not matches:
                    continue
                if allow_qt_vendor_path and all(
                    any(vendor.match(match.group()) for vendor in QT_VENDOR_BUILD_PATH_PATTERNS)
                    for match in matches
                ):
                    continue
                findings.append("personal absolute path")
                break
        tail = data[-overlap:]
    return findings


def _scan_python_runtime_archive(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(entries) > 2048 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
                return ["invalid Python runtime archive"]
            for entry in entries:
                validate_zip_name(entry.filename)
                if not entry.filename.casefold().endswith(".pyc") or entry.file_size > 8 * 1024 * 1024:
                    return ["invalid Python runtime archive"]
                if entry.compress_size and entry.file_size / entry.compress_size > 200:
                    return ["invalid Python runtime archive"]
                with archive.open(entry) as handle:
                    for finding in _scan_blocks(handle, allow_qt_vendor_path=False):
                        if finding not in findings:
                            findings.append(finding)
    except (OSError, zipfile.BadZipFile, ReleaseError):
        return ["invalid Python runtime archive"]
    return findings


def scan_sensitive_bytes(path: Path) -> list[str]:
    with path.open("rb") as handle:
        header = handle.read(32)
        handle.seek(0)
        findings = _scan_blocks(handle, allow_qt_vendor_path=header.startswith(b"MZ"))

    if path.name.casefold() == "base_library.zip":
        for finding in _scan_python_runtime_archive(path):
            if finding not in findings:
                findings.append(finding)

    if path.suffix.casefold() not in MEDIA_EXTENSIONS | DATABASE_EXTENSIONS:
        if header.startswith(b"SQLite format 3\x00"):
            findings.append("renamed SQLite database")
        if (
            header.startswith((b"ID3", b"fLaC", b"OggS", b"\x1aE\xdf\xa3"))
            or (header.startswith(b"RIFF") and header[8:12] == b"WAVE")
            or (len(header) >= 12 and header[4:8] == b"ftyp")
        ):
            findings.append("renamed media file")
    return findings


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalized_tree_hash(root: Path) -> str:
    records = canonical_file_records(
        root,
        excluded={"release-manifest.json", "SHA256SUMS.txt"},
    )
    return canonical_payload_hash(records)


def load_license_inventory(path: Path | None = None) -> dict[str, object]:
    source = path or PROJECT_ROOT / "tools" / "release" / "third_party_licenses.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise ReleaseError("Third-party license inventory is empty.")
    for component in components:
        if component.get("bundled") is True and not str(
            component.get("license_identifier") or ""
        ).strip():
            raise ReleaseError(
                "Bundled component has no license identity: "
                f"{component.get('component', 'unknown')}"
            )
    component_versions = {
        str(component.get("component") or ""): str(component.get("version") or "")
        for component in components
        if component.get("bundled") is True
    }
    if component_versions != AUDITED_BUNDLED_COMPONENT_VERSIONS:
        raise ReleaseError("Bundled component inventory differs from the audited release overlay.")
    archives = payload.get("corresponding_source_archives")
    if not isinstance(archives, list) or not archives:
        raise ReleaseError("Corresponding-source inventory is empty.")
    archive_names: set[str] = set()
    archive_components: set[str] = set()
    for archive in archives:
        filename = str(archive.get("filename") or "")
        component_name = str(archive.get("component") or "")
        source_url = str(archive.get("url") or "")
        digest = str(archive.get("sha256") or "")
        if (
            not filename
            or PurePosixPath(filename).name != filename
            or filename.casefold() in {value.casefold() for value in archive_names}
            or not component_name
            or not source_url.startswith("https://")
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ReleaseError(f"Invalid corresponding-source row: {filename or 'unnamed'}")
        archive_names.add(filename)
        archive_components.add(component_name)
        covered = archive.get("covers_components", [])
        if covered is not None and not isinstance(covered, list):
            raise ReleaseError(f"Invalid corresponding-source coverage: {filename}")
        for covered_component in covered or []:
            if not isinstance(covered_component, str) or not covered_component.strip():
                raise ReleaseError(f"Invalid corresponding-source coverage: {filename}")
            archive_components.add(covered_component)
    for component in components:
        if (
            component.get("bundled") is True
            and component.get("source_or_offer_required") is True
            and str(component.get("component") or "") not in archive_components
        ):
            raise ReleaseError(
                "Bundled source-required component has no exact source archive: "
                f"{component.get('component', 'unknown')}"
            )
    if archive_names != AUDITED_SOURCE_ARCHIVES:
        raise ReleaseError("Corresponding-source set differs from the audited release overlay.")
    return payload


def unmatched_native_artifacts(
    root: Path,
    inventory_path: Path | None = None,
) -> list[str]:
    return [
        relative
        for relative, owners in native_artifact_owners(root, inventory_path).items()
        if not owners
    ]


def native_artifact_owners(
    root: Path,
    inventory_path: Path | None = None,
) -> dict[str, list[str]]:
    root = root.resolve()
    inventory = load_license_inventory(inventory_path)
    component_patterns = [
        (
            str(component.get("component") or "unnamed component"),
            [str(pattern).replace("\\", "/") for pattern in component.get("artifact_patterns", [])],
        )
        for component in inventory["components"]
        if component.get("bundled") is True
    ]
    result: dict[str, list[str]] = {}
    for path in safe_files(root):
        if path.suffix.casefold() not in {".exe", ".dll", ".pyd"}:
            continue
        relative = path.relative_to(root).as_posix()
        result[relative] = [
            component
            for component, patterns in component_patterns
            if any(
                fnmatch.fnmatchcase(relative.casefold(), pattern.casefold())
                for pattern in patterns
                if "/" in pattern
                or pattern.casefold().endswith((".exe", ".dll", ".pyd"))
            )
        ]
    return result


def missing_embedded_artifact_mappings(
    root: Path,
    inventory_path: Path | None = None,
) -> list[tuple[str, str]]:
    root = root.resolve()
    inventory = load_license_inventory(inventory_path)
    release_paths = [path.relative_to(root).as_posix() for path in safe_files(root)]
    missing: list[tuple[str, str]] = []
    for component in inventory["components"]:
        name = str(component.get("component") or "unnamed component")
        relationships = component.get("embedded_in_artifacts", [])
        if relationships is None:
            relationships = []
        if not isinstance(relationships, list):
            raise ReleaseError(f"Invalid embedded-artifact mapping for {name}")
        for pattern in relationships:
            if not isinstance(pattern, str) or not pattern.strip() or not any(
                fnmatch.fnmatchcase(relative.casefold(), pattern.casefold())
                for relative in release_paths
            ):
                missing.append((name, str(pattern)))
    return missing
