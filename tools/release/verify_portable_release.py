from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

try:
    from .release_common import (
        APP_VERSION,
        COMPLIANCE_FILENAME,
        PACKAGE_DIRECTORY,
        PORTABLE_MARKER,
        PROJECT_ROOT,
        RELEASE_LICENSE_INVENTORY_PATH,
        ReleaseError,
        canonical_payload_hash,
        exact_requirements,
        git_tree_entries_at,
        git_value,
        is_reparse_or_link,
        load_license_inventory,
        missing_embedded_artifact_mappings,
        native_artifact_owners,
        safe_files,
        scan_sensitive_bytes,
        sha256_file,
        validate_zip_name,
        violation_for_path,
    )
except ImportError:  # Direct script execution.
    from release_common import (
        APP_VERSION,
        COMPLIANCE_FILENAME,
        PACKAGE_DIRECTORY,
        PORTABLE_MARKER,
        PROJECT_ROOT,
        RELEASE_LICENSE_INVENTORY_PATH,
        ReleaseError,
        canonical_payload_hash,
        exact_requirements,
        git_tree_entries_at,
        git_value,
        is_reparse_or_link,
        load_license_inventory,
        missing_embedded_artifact_mappings,
        native_artifact_owners,
        safe_files,
        scan_sensitive_bytes,
        sha256_file,
        validate_zip_name,
        violation_for_path,
    )


REQUIRED_ROOT_FILES = {
    "MusicVault.exe",
    PORTABLE_MARKER,
    "README_FIRST_RUN.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "BINARY_DISTRIBUTION_LICENSE.md",
    "AUTHORIZED_USE.md",
    "release-manifest.json",
    "SHA256SUMS.txt",
    "licenses/third_party_licenses.json",
}


class Finding:
    def __init__(self, path: str, rule: str, category: str) -> None:
        self.path = path
        self.rule = rule
        self.category = category

    def render(self) -> str:
        return f"{self.path}: {self.rule} [{self.category}]"


PROVENANCE_KEYS = (
    "source_tag",
    "source_tag_object",
    "source_commit",
    "source_tree_hash",
    "release_tooling_commit",
    "release_tooling_tree_hash",
    "release_license_inventory_git_blob",
)


def verify_provenance_fields(
    manifest: dict[str, object], path: str
) -> list[Finding]:
    findings: list[Finding] = []
    if manifest.get("manifest_schema_version") != 2:
        findings.append(Finding(path, "dual-provenance manifest schema mismatch", "provenance"))
    if manifest.get("source_tag") != f"v{APP_VERSION}":
        findings.append(Finding(path, "source tag does not match product version", "provenance"))
    for key in PROVENANCE_KEYS[1:]:
        if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get(key) or "")):
            findings.append(Finding(path, f"{key} is not an exact SHA-1", "provenance"))
    return findings


def verify_release_inventory_anchor(
    manifest: dict[str, object], inventory_path: Path, display_path: str
) -> list[Finding]:
    findings: list[Finding] = []
    inventory_hash = str(manifest.get("release_license_inventory_sha256") or "")
    inventory_blob = str(manifest.get("release_license_inventory_git_blob") or "")
    tooling_commit = str(manifest.get("release_tooling_commit") or "")
    valid_hash = re.fullmatch(r"[0-9a-f]{64}", inventory_hash) is not None
    valid_blob = re.fullmatch(r"[0-9a-f]{40}", inventory_blob) is not None
    valid_commit = re.fullmatch(r"[0-9a-f]{40}", tooling_commit) is not None
    if not valid_hash:
        findings.append(Finding(
            display_path,
            "corrected release license inventory hash is invalid",
            "provenance",
        ))
    if not valid_blob:
        findings.append(Finding(
            display_path,
            "corrected release license inventory Git blob is invalid",
            "provenance",
        ))
    if not inventory_path.is_file() or is_reparse_or_link(inventory_path):
        findings.append(Finding(
            display_path,
            "corrected release license inventory is absent or unsafe",
            "provenance",
        ))
        return findings
    if valid_hash and sha256_file(inventory_path) != inventory_hash:
        findings.append(Finding(
            display_path,
            "corrected release license inventory hash mismatch",
            "provenance",
        ))
    if valid_blob:
        try:
            packaged_blob = git_value(
                "hash-object",
                f"--path={RELEASE_LICENSE_INVENTORY_PATH}",
                "--",
                str(inventory_path.resolve()),
            )
        except ReleaseError:
            findings.append(Finding(
                display_path,
                "packaged release inventory blob could not be canonicalized",
                "provenance",
            ))
        else:
            if packaged_blob != inventory_blob:
                findings.append(Finding(
                    display_path,
                    "corrected release license inventory blob mismatch",
                    "provenance",
                ))
    if valid_blob and valid_commit:
        object_expression = f"{tooling_commit}:{RELEASE_LICENSE_INVENTORY_PATH}"
        try:
            if git_value("cat-file", "-t", object_expression) != "blob":
                findings.append(Finding(
                    display_path,
                    "release tooling commit inventory object is not a blob",
                    "provenance",
                ))
            elif git_value("rev-parse", object_expression) != inventory_blob:
                findings.append(Finding(
                    display_path,
                    "inventory blob does not belong to the release tooling commit",
                    "provenance",
                ))
        except ReleaseError:
            findings.append(Finding(
                display_path,
                "release tooling inventory blob is unavailable for verification",
                "provenance",
            ))
    return findings


def _export_expected_source_snapshot(source_commit: str, destination: Path) -> Path:
    archive_path = destination.parent / "expected-tagged-source.zip"
    command = [
        "git",
        "-c",
        f"safe.directory={PROJECT_ROOT.resolve().as_posix()}",
        "archive",
        "--format=zip",
        "--prefix=expected-tagged-source/",
        f"--output={archive_path}",
        source_commit,
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode:
        raise ReleaseError("Exact tagged source archive could not be reconstructed.")
    return safe_extract(archive_path, destination)


def verify_tagged_source_snapshot(source_root: Path, source_commit: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        entries = git_tree_entries_at(PROJECT_ROOT, source_commit)
    except ReleaseError:
        return [Finding(
            "source-snapshot.json",
            "tagged Git tree is unavailable for exact source comparison",
            "provenance",
        )]
    with tempfile.TemporaryDirectory(prefix="MusicVault_Tagged_Source_Verify_") as temp:
        try:
            expected_root = _export_expected_source_snapshot(
                source_commit, Path(temp) / "extract"
            )
        except ReleaseError:
            return findings + [Finding(
                "source-snapshot.json",
                "tagged Git source bytes are unavailable for exact comparison",
                "provenance",
            )]
        seen: set[str] = set()
        for mode, kind, object_id, raw_relative in entries:
            try:
                relative = validate_zip_name(raw_relative).as_posix()
            except ReleaseError:
                findings.append(Finding(
                    "source-snapshot.json",
                    "tagged Git tree contains an unsafe path",
                    "provenance",
                ))
                continue
            folded = unicodedata.normalize("NFC", relative).casefold()
            if folded in seen:
                findings.append(Finding(
                    relative,
                    "tagged Git tree contains a conflicting path",
                    "provenance",
                ))
                continue
            seen.add(folded)
            if mode == "160000" or kind == "commit":
                findings.append(Finding(
                    relative,
                    "tagged Git tree contains an unsupported gitlink",
                    "provenance",
                ))
                continue
            if kind != "blob" or mode not in {"100644", "100755"}:
                findings.append(Finding(
                    relative,
                    "tagged Git tree contains an unsupported tracked entry",
                    "provenance",
                ))
                continue
            if not re.fullmatch(r"[0-9a-f]{40}", object_id):
                findings.append(Finding(
                    relative,
                    "tagged Git tree blob identity is invalid",
                    "provenance",
                ))
                continue
            parts = PurePosixPath(relative).parts
            target = source_root.joinpath(*parts)
            expected = expected_root.joinpath(*parts)
            if (
                not target.is_file()
                or is_reparse_or_link(target)
                or not expected.is_file()
                or is_reparse_or_link(expected)
            ):
                findings.append(Finding(
                    relative,
                    "tagged tracked source blob is missing or unsafe",
                    "provenance",
                ))
            elif (
                target.stat().st_size != expected.stat().st_size
                or sha256_file(target) != sha256_file(expected)
            ):
                findings.append(Finding(
                    relative,
                    "tagged tracked source blob does not match the Git tree",
                    "provenance",
                ))
    return findings


def verify_corresponding_source_semantics(
    source_root: Path, inventory_path: Path
) -> tuple[list[dict[str, object]], list[Finding]]:
    try:
        from .fetch_compliance_sources import fetch_sources
    except ImportError:  # Direct script execution.
        from fetch_compliance_sources import fetch_sources
    try:
        rows = fetch_sources(
            source_root / "third-party-sources",
            True,
            inventory_path,
        )
    except (OSError, ReleaseError, ValueError, json.JSONDecodeError):
        return [], [Finding(
            "third-party-sources",
            "corresponding-source semantic validation failed",
            "licensing",
        )]
    return rows, []


def inspect_zip_structure(path: Path) -> None:
    seen: dict[str, bool] = {}
    total_size = 0
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 100_000:
            raise ReleaseError("ZIP contains too many entries.")
        for info in archive.infolist():
            normalized = validate_zip_name(info.filename).as_posix()
            folded = unicodedata.normalize("NFC", normalized).casefold()
            if folded in seen:
                raise ReleaseError(f"Duplicate conflicting ZIP entry: {normalized}")
            parts = folded.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if parent in seen and not seen[parent]:
                    raise ReleaseError(f"ZIP file/directory namespace collision: {normalized}")
            if not info.is_dir() and any(
                existing.startswith(folded + "/") for existing in seen
            ):
                raise ReleaseError(f"ZIP file/directory namespace collision: {normalized}")
            seen[folded] = info.is_dir()
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                raise ReleaseError(f"Symlink ZIP entry is not allowed: {normalized}")
            if info.flag_bits & 0x1:
                raise ReleaseError(f"Encrypted ZIP entry is not allowed: {normalized}")
            total_size += info.file_size
            if total_size > 8 * 1024 * 1024 * 1024:
                raise ReleaseError("ZIP expands beyond the release verification limit.")
            if info.file_size > 64 * 1024 * 1024 and info.compress_size:
                if info.file_size / info.compress_size > 1_000:
                    raise ReleaseError(f"ZIP entry has an unsafe compression ratio: {normalized}")


def safe_extract(path: Path, destination: Path) -> Path:
    inspect_zip_structure(path)
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ReleaseError("ZIP extraction destination must be an empty directory.")
    else:
        destination.mkdir(parents=True)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            relative = validate_zip_name(info.filename)
            target = destination.joinpath(*relative.parts).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ReleaseError(f"ZIP entry escapes extraction root: {relative}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ReleaseError("Portable ZIP must contain one top-level package directory.")
    return children[0]


def _pe_version(executable: Path) -> tuple[str | None, str | None, dict[str, str]]:
    try:
        import pefile
    except ImportError as exc:
        raise ReleaseError("pefile is required to verify Windows version metadata.") from exc
    pe = pefile.PE(str(executable), fast_load=False)
    fixed_file = fixed_product = None
    if getattr(pe, "VS_FIXEDFILEINFO", None):
        info = pe.VS_FIXEDFILEINFO[0]
        fixed_file = ".".join(str(value) for value in (
            info.FileVersionMS >> 16, info.FileVersionMS & 0xFFFF,
            info.FileVersionLS >> 16, info.FileVersionLS & 0xFFFF,
        ))
        fixed_product = ".".join(str(value) for value in (
            info.ProductVersionMS >> 16, info.ProductVersionMS & 0xFFFF,
            info.ProductVersionLS >> 16, info.ProductVersionLS & 0xFFFF,
        ))
    strings: dict[str, str] = {}
    for group in getattr(pe, "FileInfo", []) or []:
        for item in group:
            if getattr(item, "Key", b"") == b"StringFileInfo":
                for table in item.StringTable:
                    strings.update({
                        key.decode(errors="replace"): value.decode(errors="replace")
                        for key, value in table.entries.items()
                    })
    pe.close()
    return fixed_file, fixed_product, strings


def parse_checksum_file(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (root / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or not re.fullmatch(r"[0-9A-Fa-f]{64}", digest)
        ):
            raise ReleaseError("SHA256SUMS.txt contains an invalid row.")
        validate_zip_name(relative)
        if relative.casefold() in {value.casefold() for value in result}:
            raise ReleaseError("SHA256SUMS.txt contains a duplicate path.")
        result[relative] = digest.casefold()
    return result


def verify_license_inventory(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    inventory_path = root / "licenses" / "third_party_licenses.json"
    try:
        inventory = load_license_inventory(inventory_path)
    except (ReleaseError, json.JSONDecodeError) as exc:
        return [Finding(
            "licenses/third_party_licenses.json",
            f"invalid license/source inventory: {exc}",
            "licensing",
        )]
    components = inventory.get("components")
    if not isinstance(components, list) or not components:
        findings.append(Finding("licenses/third_party_licenses.json", "empty license inventory", "licensing"))
        return findings
    copyleft_found = False
    source_compliance = inventory.get("source_compliance_required") is True
    if inventory.get("public_distribution_approved") is not True:
        findings.append(Finding("licenses/third_party_licenses.json", "public distribution is not approved", "licensing"))
    for component in components:
        name = str(component.get("component") or "unnamed component")
        identifier = str(component.get("license_identifier") or "").strip()
        if component.get("bundled") is True and not identifier:
            findings.append(Finding("licenses/third_party_licenses.json", f"missing license identity for {name}", "licensing"))
        if any(value in identifier.upper() for value in ("GPL", "LGPL", "MPL")):
            copyleft_found = True
        if component.get("license_text_required"):
            references = [value.strip() for value in str(component.get("license_source") or "").split(";")]
            if not references:
                findings.append(Finding("licenses/third_party_licenses.json", f"missing license text reference for {name}", "licensing"))
            for reference in references:
                if reference.startswith("licenses/"):
                    target = root / reference
                    if not target.is_file():
                        findings.append(Finding(reference, f"missing required license text for {name}", "licensing"))
                    elif target.stat().st_size < 32:
                        findings.append(Finding(reference, f"required license text is implausibly short for {name}", "licensing"))
    if not copyleft_found:
        findings.append(Finding("licenses/third_party_licenses.json", "bundled copyleft component not identified", "licensing"))
    if not source_compliance:
        findings.append(Finding("licenses/third_party_licenses.json", "source-compliance requirement is absent", "licensing"))
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    if "not an MIT-only binary" not in notices:
        findings.append(Finding("THIRD_PARTY_NOTICES.md", "portable binary could be mistaken for MIT-only", "licensing"))
    if "MIT License" not in (root / "LICENSE").read_text(encoding="utf-8"):
        findings.append(Finding("LICENSE", "root project source license is not MIT", "licensing"))
    required_substantive = {
        "licenses/GPL-3.0.txt": 30_000,
        "licenses/LGPL-3.0.txt": 7_000,
        "licenses/LGPL-2.1.txt": 20_000,
        "licenses/MPL-2.0.txt": 15_000,
        "licenses/OPENSSL-APACHE-2.0.txt": 9_000,
    }
    for relative, minimum_size in required_substantive.items():
        target = root / relative
        if not target.is_file() or target.stat().st_size < minimum_size:
            findings.append(Finding(relative, "full required license text is absent", "licensing"))
    attribution_root = root / "licenses" / "qt-attrib"
    if not (attribution_root / "INDEX.md").is_file():
        findings.append(Finding("licenses/qt-attrib/INDEX.md", "exact Qt attribution index is absent", "licensing"))
    if not (attribution_root / "SOURCE_ARCHIVES.json").is_file():
        findings.append(Finding("licenses/qt-attrib/SOURCE_ARCHIVES.json", "Qt attribution provenance is absent", "licensing"))
    versions = {
        str(component.get("component")): str(component.get("version"))
        for component in components
    }
    if versions.get("CPython runtime") != "3.11.9":
        findings.append(Finding("licenses/third_party_licenses.json", "CPython inventory version mismatch", "licensing"))
    if versions.get("OpenSSL libraries from CPython") != "3.0.13":
        findings.append(Finding("licenses/third_party_licenses.json", "OpenSSL inventory version mismatch", "licensing"))
    return findings


def verify_directory(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    existing = {path.relative_to(root).as_posix() for path in safe_files(root)}
    for required in sorted(REQUIRED_ROOT_FILES):
        if required not in existing:
            findings.append(Finding(required, "required release file is missing", "layout"))

    for path in safe_files(root):
        relative = path.relative_to(root).as_posix()
        top = PurePosixPath(relative).parts[0]
        allowed_top = {
            "MusicVault.exe", "_internal", "licenses", PORTABLE_MARKER,
            "README_FIRST_RUN.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
            "BINARY_DISTRIBUTION_LICENSE.md", "AUTHORIZED_USE.md",
            "release-manifest.json", "SHA256SUMS.txt",
        }
        if top not in allowed_top:
            findings.append(Finding(relative, "unexpected portable-root content", "layout"))
        violation = violation_for_path(relative)
        if violation:
            findings.append(Finding(relative, violation, "forbidden content"))
        for issue in scan_sensitive_bytes(path):
            findings.append(Finding(relative, issue, "sensitive content"))

    if findings:
        return findings

    marker = json.loads((root / PORTABLE_MARKER).read_text(encoding="utf-8"))
    if marker.get("version") != APP_VERSION or marker.get("schema_version") != 1:
        findings.append(Finding(PORTABLE_MARKER, "portable marker version mismatch", "version"))

    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != APP_VERSION:
        findings.append(Finding("release-manifest.json", "product version mismatch", "version"))
    findings.extend(verify_provenance_fields(manifest, "release-manifest.json"))
    findings.extend(verify_release_inventory_anchor(
        manifest,
        root / "licenses/third_party_licenses.json",
        "licenses/third_party_licenses.json",
    ))
    for key in ("ffmpeg_bundled", "api_credentials_bundled", "runtime_data_bundled"):
        if manifest.get(key) is not False:
            findings.append(Finding("release-manifest.json", f"{key} must be false", "manifest"))
    environment = manifest.get("build_environment")
    if not isinstance(environment, dict):
        findings.append(Finding("release-manifest.json", "verified build environment is absent", "provenance"))
    else:
        expected_environment = {
            "python": "3.11.9",
            "sqlite": "3.45.1",
            "pyinstaller": "6.21.0",
        }
        for key, expected in expected_environment.items():
            if environment.get(key) != expected:
                findings.append(Finding("release-manifest.json", f"build environment {key} mismatch", "provenance"))
        if not str(environment.get("openssl") or "").startswith("OpenSSL 3.0.13 "):
            findings.append(Finding("release-manifest.json", "build environment OpenSSL mismatch", "provenance"))
        if environment.get("dependencies") != exact_requirements():
            findings.append(Finding("release-manifest.json", "installed dependency set does not match the release lock", "provenance"))
    records = manifest.get("files")
    if not isinstance(records, list):
        findings.append(Finding("release-manifest.json", "file inventory is missing", "manifest"))
        records = []
    listed_paths: set[str] = set()
    for record in records:
        relative = str(record.get("path") or "")
        try:
            validate_zip_name(relative)
        except ReleaseError:
            findings.append(Finding("release-manifest.json", "unsafe manifest path", "integrity"))
            continue
        if relative.casefold() in listed_paths:
            findings.append(Finding(relative, "duplicate manifest path", "integrity"))
            continue
        listed_paths.add(relative.casefold())
        target = root / relative
        if not target.is_file():
            findings.append(Finding(relative or "release-manifest.json", "manifest file is missing", "integrity"))
            continue
        if sha256_file(target) != record.get("sha256") or target.stat().st_size != record.get("size"):
            findings.append(Finding(relative, "manifest file hash/size mismatch", "integrity"))
    if records and canonical_payload_hash(records) != manifest.get("package_sha256"):
        findings.append(Finding("release-manifest.json", "canonical package payload hash mismatch", "integrity"))
    if manifest.get("file_count") != len(existing):
        findings.append(Finding("release-manifest.json", "package file count mismatch", "integrity"))
    expected_inventory = listed_paths | {
        "release-manifest.json",
        "sha256sums.txt",
    }
    unexpected = sorted(
        relative for relative in existing if relative.casefold() not in expected_inventory
    )
    for relative in unexpected:
        findings.append(Finding(relative, "file is absent from the release manifest", "integrity"))

    checksums = parse_checksum_file(root)
    expected_checksum_paths = {
        relative for relative in existing if relative != "SHA256SUMS.txt"
    }
    if {value.casefold() for value in checksums} != {
        value.casefold() for value in expected_checksum_paths
    }:
        findings.append(Finding("SHA256SUMS.txt", "checksum inventory is incomplete or has unexpected paths", "integrity"))
    for relative, expected in checksums.items():
        target = root / relative
        if not target.is_file() or sha256_file(target) != expected:
            findings.append(Finding(relative, "package checksum mismatch", "integrity"))

    if manifest.get("executable_sha256") != sha256_file(root / "MusicVault.exe"):
        findings.append(Finding("release-manifest.json", "executable hash mismatch", "integrity"))

    executable = root / "MusicVault.exe"
    fixed_file, fixed_product, strings = _pe_version(executable)
    if fixed_file != "1.0.0.0" or fixed_product != "1.0.0.0":
        findings.append(Finding("MusicVault.exe", "Windows version resource mismatch", "version"))
    expected_strings = {
        "ProductName": "Music Vault",
        "FileDescription": "Music Vault",
        "OriginalFilename": "MusicVault.exe",
    }
    for key, expected in expected_strings.items():
        if strings.get(key) != expected:
            findings.append(Finding("MusicVault.exe", f"missing/incorrect {key} version resource", "version"))
    if strings.get("FileVersion") not in {"1.0.0.0", "1.0.0"}:
        findings.append(Finding("MusicVault.exe", "string FileVersion mismatch", "version"))

    assignments = native_artifact_owners(
        root, root / "licenses" / "third_party_licenses.json"
    )
    for relative, owners in assignments.items():
        if not owners:
            findings.append(Finding(relative, "native artifact has no license mapping", "licensing"))
        elif len(owners) != 1:
            findings.append(Finding(relative, "native artifact has ambiguous license ownership", "licensing"))

    for component, pattern in missing_embedded_artifact_mappings(
        root, root / "licenses" / "third_party_licenses.json"
    ):
        findings.append(Finding(
            "licenses/third_party_licenses.json",
            f"embedded component mapping does not match: {component}:{pattern}",
            "licensing",
        ))

    findings.extend(verify_license_inventory(root))
    return findings


def verify_external_zip_checksum(path: Path) -> None:
    checksum = path.with_name(path.name + ".sha256")
    if not checksum.is_file():
        raise ReleaseError(f"Portable checksum file is missing: {checksum.name}")
    row = checksum.read_text(encoding="ascii").strip()
    expected, separator, name = row.partition("  ")
    if not separator or name != path.name or expected.casefold() != sha256_file(path):
        raise ReleaseError("Portable ZIP checksum mismatch.")


def verify_external_release_manifest(path: Path) -> dict[str, object]:
    manifest_path = path.parent / "release-manifest.json"
    if not manifest_path.is_file():
        raise ReleaseError("External release-manifest.json is missing.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != APP_VERSION:
        raise ReleaseError("External release manifest version mismatch.")
    if manifest.get("package_filename") != path.name:
        raise ReleaseError("External release manifest package name mismatch.")
    if manifest.get("package_sha256") != sha256_file(path):
        raise ReleaseError("External release manifest package hash mismatch.")
    if manifest.get("package_sha256_scope") != "final ZIP file":
        raise ReleaseError("External release manifest hash scope is invalid.")
    return manifest


def verify_source_compliance(
    path: Path, *, expected_release_manifest: dict[str, object] | None = None
) -> list[Finding]:
    checksum = path.with_name(path.name + ".sha256")
    if not checksum.is_file():
        raise ReleaseError("Source-compliance checksum file is missing.")
    row = checksum.read_text(encoding="ascii").strip()
    expected, separator, filename = row.partition("  ")
    if (
        not separator
        or filename != path.name
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", expected)
        or expected.casefold() != sha256_file(path)
    ):
        raise ReleaseError("Source-compliance ZIP checksum mismatch.")

    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="MusicVault_Compliance_Verify_") as temp:
        root = safe_extract(path, Path(temp))
        expected_root = f"MusicVault-v{APP_VERSION}-Source-Compliance"
        if root.name != expected_root:
            return [Finding(root.name, "source-compliance top-level name mismatch", "compliance")]
        existing = {item.relative_to(root).as_posix() for item in safe_files(root)}
        required = {
            "LICENSE", "MusicVault.spec", "requirements-release.txt",
            "music_vault/version.py", "SOURCE_COMPLIANCE_README.md",
            "source-snapshot.json", "source-compliance-manifest.json",
            "tools/release/third_party_licenses.json",
            "release-tooling/third_party_licenses.json",
        }
        for relative in sorted(required - existing):
            findings.append(Finding(relative, "required corresponding source input is absent", "compliance"))
        for item in safe_files(root):
            relative = item.relative_to(root).as_posix()
            parts = {part.casefold() for part in PurePosixPath(relative).parts}
            name = item.name.casefold()
            if parts & {".git", ".venv", ".codex", ".agents", "__pycache__", "release_artifacts", "build", "dist"}:
                findings.append(Finding(relative, "private/generated source content", "compliance"))
            if name in {
                "youtube_api_key.txt", "music_vault_config.json", "music_vault_status.json",
                "music_vault.sqlite3", "youtube_failed_ids.txt", "youtube_download_archive.txt",
            }:
                findings.append(Finding(relative, "runtime/private source content", "compliance"))
            if item.suffix.casefold() in {".exe", ".dll", ".pyd", ".pyc", ".pyo", ".mp3", ".flac", ".m4a", ".wav", ".webm"}:
                findings.append(Finding(relative, "binary/runtime content in source artifact", "compliance"))
            for issue in scan_sensitive_bytes(item):
                findings.append(Finding(relative, issue, "compliance safety"))

        manifest_path = root / "source-compliance-manifest.json"
        snapshot_path = root / "source-snapshot.json"
        if manifest_path.is_file() and snapshot_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            findings.extend(
                verify_provenance_fields(manifest, "source-compliance-manifest.json")
            )
            if snapshot.get("schema_version") != 2:
                findings.append(Finding(
                    "source-snapshot.json",
                    "dual-provenance snapshot schema mismatch",
                    "provenance",
                ))
            records = manifest.get("files")
            if not isinstance(records, list):
                findings.append(Finding("source-compliance-manifest.json", "file inventory is absent", "compliance"))
                records = []
            listed: set[str] = set()
            for record in records:
                relative = str(record.get("path") or "")
                try:
                    validate_zip_name(relative)
                except ReleaseError:
                    findings.append(Finding("source-compliance-manifest.json", "unsafe source inventory path", "compliance"))
                    continue
                listed.add(relative.casefold())
                target = root / relative
                if (
                    not target.is_file()
                    or target.stat().st_size != record.get("size")
                    or sha256_file(target) != record.get("sha256")
                ):
                    findings.append(Finding(relative, "source inventory hash/size mismatch", "compliance"))
            expected_files = {
                relative.casefold()
                for relative in existing
                if relative != "source-compliance-manifest.json"
            }
            if listed != expected_files:
                findings.append(Finding("source-compliance-manifest.json", "source inventory is incomplete or unexpected", "compliance"))
            if canonical_payload_hash(records) != manifest.get("payload_sha256"):
                findings.append(Finding("source-compliance-manifest.json", "source payload hash mismatch", "compliance"))
            if manifest.get("file_count") != len(existing):
                findings.append(Finding("source-compliance-manifest.json", "source file count mismatch", "compliance"))
            for key in PROVENANCE_KEYS:
                if manifest.get(key) != snapshot.get(key):
                    findings.append(Finding("source-snapshot.json", f"{key} disagrees with compliance manifest", "compliance"))
                if expected_release_manifest and manifest.get(key) != expected_release_manifest.get(key):
                    findings.append(Finding("source-compliance-manifest.json", f"{key} disagrees with portable manifest", "provenance"))
            inventory_hash = str(manifest.get("release_license_inventory_sha256") or "")
            if snapshot.get("release_license_inventory_sha256") != inventory_hash:
                findings.append(Finding("source-snapshot.json", "license inventory hash disagrees with compliance manifest", "provenance"))
            if expected_release_manifest and expected_release_manifest.get("release_license_inventory_sha256") != inventory_hash:
                findings.append(Finding("source-compliance-manifest.json", "license inventory hash disagrees with portable manifest", "provenance"))
            tooling_inventory = root / "release-tooling" / "third_party_licenses.json"
            findings.extend(verify_release_inventory_anchor(
                manifest,
                tooling_inventory,
                "release-tooling/third_party_licenses.json",
            ))
            source_tag = str(manifest.get("source_tag") or "")
            tag_object = str(manifest.get("source_tag_object") or "")
            commit = str(manifest.get("source_commit") or "")
            tree = str(manifest.get("source_tree_hash") or "")
            tooling_commit = str(manifest.get("release_tooling_commit") or "")
            tooling_tree = str(manifest.get("release_tooling_tree_hash") or "")
            if any(
                not re.fullmatch(r"[0-9a-f]{40}", value)
                for value in (tag_object, commit, tree, tooling_commit, tooling_tree)
            ):
                findings.append(Finding("source-snapshot.json", "source commit/tree is not an exact SHA-1", "provenance"))
            else:
                try:
                    tag_ref = f"refs/tags/{source_tag}"
                    if git_value("cat-file", "-t", tag_ref) != "tag":
                        findings.append(Finding("source-snapshot.json", "source tag is not annotated", "provenance"))
                    if git_value("rev-parse", tag_ref) != tag_object:
                        findings.append(Finding("source-snapshot.json", "source tag object provenance mismatch", "provenance"))
                    if git_value("rev-parse", f"{tag_ref}^{{commit}}") != commit:
                        findings.append(Finding("source-snapshot.json", "source tag/commit provenance mismatch", "provenance"))
                    if git_value("rev-parse", f"{commit}^{{tree}}") != tree:
                        findings.append(Finding("source-snapshot.json", "Git commit/tree provenance mismatch", "provenance"))
                    if git_value("rev-parse", f"{tooling_commit}^{{tree}}") != tooling_tree:
                        findings.append(Finding("source-snapshot.json", "release tooling commit/tree provenance mismatch", "provenance"))
                except ReleaseError:
                    findings.append(Finding("source-snapshot.json", "source/tooling provenance is unavailable for verification", "provenance"))
                findings.extend(verify_tagged_source_snapshot(root, commit))

            semantic_rows, semantic_findings = verify_corresponding_source_semantics(
                root, tooling_inventory
            )
            findings.extend(semantic_findings)
            expected_archives = {
                str(row["filename"]): row
                for row in semantic_rows
            }
            manifest_archives = {
                str(row.get("filename")): row
                for row in manifest.get("corresponding_source_archives", [])
            }
            if set(expected_archives) != set(manifest_archives):
                findings.append(Finding("source-compliance-manifest.json", "corresponding-source archive set mismatch", "compliance"))
            for filename, expected_row in expected_archives.items():
                source = root / "third-party-sources" / filename
                manifest_row = manifest_archives.get(filename, {})
                if (
                    not source.is_file()
                    or sha256_file(source) != expected_row.get("sha256")
                    or manifest_row != expected_row
                ):
                    findings.append(Finding(f"third-party-sources/{filename}", "corresponding source or semantic attestation does not match independent validation", "licensing"))
    return findings


def verify(path: Path) -> list[Finding]:
    if path.is_file():
        if path.suffix.casefold() != ".zip":
            raise ReleaseError("Verifier input must be a portable ZIP or extracted directory.")
        verify_external_zip_checksum(path)
        external_manifest = verify_external_release_manifest(path)
        with tempfile.TemporaryDirectory(prefix="MusicVault_Release_Verify_") as temp:
            root = safe_extract(path, Path(temp))
            if root.name != PACKAGE_DIRECTORY:
                return [Finding(root.name, "top-level package version/name mismatch", "layout")]
            findings = verify_directory(root)
            internal_manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
            expected_external = dict(internal_manifest)
            expected_external.update({
                "package_sha256": sha256_file(path),
                "package_sha256_scope": "final ZIP file",
                "package_filename": path.name,
            })
            if external_manifest != expected_external:
                findings.append(Finding("release-manifest.json", "external manifest does not reconcile with package manifest", "provenance"))
        compliance = path.with_name(COMPLIANCE_FILENAME)
        if not compliance.is_file():
            findings.append(Finding(COMPLIANCE_FILENAME, "source-compliance artifact is missing", "licensing"))
        else:
            findings.extend(
                verify_source_compliance(
                    compliance, expected_release_manifest=internal_manifest
                )
            )
        return findings
    if path.is_dir():
        root = path
        if not (root / "MusicVault.exe").is_file():
            candidates = [entry for entry in root.iterdir() if entry.is_dir() and (entry / "MusicVault.exe").is_file()]
            if len(candidates) != 1 or len(list(root.iterdir())) != 1:
                raise ReleaseError("Extracted directory does not contain exactly one portable root.")
            root = candidates[0]
        return verify_directory(root)
    raise ReleaseError(f"Verifier input does not exist: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a Music Vault portable release.")
    parser.add_argument("path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        findings = verify(args.path.expanduser().resolve())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, ReleaseError) as exc:
        print(f"Portable release verification failed: {exc}", file=sys.stderr)
        return 1
    if findings:
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        print(f"Portable release verification failed with {len(findings)} finding(s).", file=sys.stderr)
        return 1
    print(f"Music Vault v{APP_VERSION} portable release verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
