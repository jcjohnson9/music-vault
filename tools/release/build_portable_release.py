from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import os
import platform
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from .release_common import (
        APP_VERSION,
        PORTABLE_MARKER,
        PORTABLE_MARKER_VERSION,
        PRODUCT_NAME,
        PROJECT_ROOT,
        RELEASE_LICENSE_INVENTORY_PATH,
        RELEASE_CHANNEL,
        ReleaseError,
        canonical_file_records,
        canonical_payload_hash,
        compliance_filename_for,
        deterministic_zip,
        exact_requirements,
        git_value,
        git_value_at,
        is_reparse_or_link,
        missing_embedded_artifact_mappings,
        native_artifact_owners,
        package_directory_for,
        package_filename_for,
        safe_files,
        scan_sensitive_bytes,
        sha256_file,
        validate_zip_name,
        validate_release_version,
        violation_for_path,
        write_json,
    )
except ImportError:  # Direct script execution.
    from release_common import (
        APP_VERSION,
        PORTABLE_MARKER,
        PORTABLE_MARKER_VERSION,
        PRODUCT_NAME,
        PROJECT_ROOT,
        RELEASE_LICENSE_INVENTORY_PATH,
        RELEASE_CHANNEL,
        ReleaseError,
        canonical_file_records,
        canonical_payload_hash,
        compliance_filename_for,
        deterministic_zip,
        exact_requirements,
        git_value,
        git_value_at,
        is_reparse_or_link,
        missing_embedded_artifact_mappings,
        native_artifact_owners,
        package_directory_for,
        package_filename_for,
        safe_files,
        scan_sensitive_bytes,
        sha256_file,
        validate_zip_name,
        validate_release_version,
        violation_for_path,
        write_json,
    )


ROOT_DOCUMENTS = {
    "README_FIRST_RUN.md": "README_FIRST_RUN.md",
    "LICENSE": "LICENSE",
    "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "docs/BINARY_DISTRIBUTION_LICENSE.md": "BINARY_DISTRIBUTION_LICENSE.md",
    "docs/AUTHORIZED_USE.md": "AUTHORIZED_USE.md",
}
IMAGE_EXTENSIONS = {".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


def _inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _application_release_identity(application_root: Path) -> tuple[str, str]:
    """Read immutable product identity without importing the application checkout."""
    version_file = application_root / "music_vault" / "version.py"
    try:
        tree = ast.parse(version_file.read_text(encoding="utf-8"), filename=str(version_file))
    except (OSError, SyntaxError) as exc:
        raise ReleaseError("The tagged application version metadata is unavailable.") from exc
    values: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        value = statement.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"APP_VERSION", "RELEASE_CHANNEL"}:
                values[target.id] = value.value
    try:
        version = validate_release_version(values["APP_VERSION"])
        channel = values["RELEASE_CHANNEL"].strip()
    except KeyError as exc:
        raise ReleaseError("The tagged application release identity is incomplete.") from exc
    if channel not in {"stable", "development"}:
        raise ReleaseError("The tagged application release channel is invalid.")
    return version, channel


def _git(repository_root: Path, *args: str) -> str:
    root = repository_root.expanduser().resolve()
    if root == PROJECT_ROOT.resolve():
        return git_value(*args)
    return git_value_at(root, *args)


def _resolve_release_inventory(
    inventory_path: Path, tooling_commit: str
) -> tuple[Path, str, str]:
    expected = (PROJECT_ROOT / RELEASE_LICENSE_INVENTORY_PATH).resolve()
    selected = inventory_path.expanduser().resolve()
    if selected != expected:
        raise ReleaseError(
            "Release license inventory must be the tracked tooling inventory."
        )
    if not selected.is_file() or is_reparse_or_link(selected):
        raise ReleaseError("The tracked release license inventory is unavailable or unsafe.")
    object_expression = f"{tooling_commit}:{RELEASE_LICENSE_INVENTORY_PATH}"
    if _git(PROJECT_ROOT, "cat-file", "-t", object_expression) != "blob":
        raise ReleaseError("The release tooling commit does not contain the inventory blob.")
    blob_id = _git(PROJECT_ROOT, "rev-parse", object_expression)
    if not re.fullmatch(r"[0-9a-f]{40}", blob_id):
        raise ReleaseError("The release license inventory Git blob identity is invalid.")
    filtered_blob_id = _git(
        PROJECT_ROOT,
        "hash-object",
        f"--path={RELEASE_LICENSE_INVENTORY_PATH}",
        "--",
        str(selected),
    )
    if filtered_blob_id != blob_id:
        raise ReleaseError(
            "The selected release license inventory differs from the tooling commit."
        )
    return selected, blob_id, sha256_file(selected)


def validate_output(output: Path, dist_dir: Path, application_root: Path = PROJECT_ROOT) -> Path:
    expanded = output.expanduser()
    if expanded.exists() and is_reparse_or_link(expanded):
        raise ReleaseError("Release output may not be a symlink or reparse point.")
    if dist_dir.exists() and is_reparse_or_link(dist_dir):
        raise ReleaseError("Official distribution may not be a symlink or reparse point.")
    resolved = expanded.resolve()
    live_data_roots = {
        (application_root / "data").resolve(),
        (PROJECT_ROOT / "data").resolve(),
    }
    if any(resolved == live_data or _inside(resolved, live_data) for live_data in live_data_roots):
        raise ReleaseError("Release staging may not use a live data directory.")
    if resolved == dist_dir.resolve() or _inside(resolved, dist_dir.resolve()):
        raise ReleaseError("Release staging may not be placed inside the PyInstaller distribution.")
    return resolved


def copy_distribution(
    dist_dir: Path,
    portable_root: Path,
    application_root: Path = PROJECT_ROOT,
    license_inventory: Path | None = None,
) -> None:
    if not (dist_dir / "MusicVault.exe").is_file():
        raise ReleaseError(f"Official executable is missing: {dist_dir / 'MusicVault.exe'}")
    files = safe_files(dist_dir)
    for path in files:
        relative = path.relative_to(dist_dir)
        violation = violation_for_path(relative.as_posix())
        if violation:
            raise ReleaseError(
                f"Official distribution contains {violation}: {relative.as_posix()}"
            )
        if relative.parts[0].casefold() not in {"musicvault.exe", "_internal"}:
            raise ReleaseError(
                f"Official distribution has an unexpected root entry: {relative.as_posix()}"
            )
        for issue in scan_sensitive_bytes(path):
            raise ReleaseError(
                f"Official distribution failed the {issue} safety rule: {relative.as_posix()}"
            )
        if path.suffix.casefold() in IMAGE_EXTENSIONS:
            expected_prefix = ("_internal", "assets")
            if tuple(part.casefold() for part in relative.parts[:2]) != expected_prefix:
                raise ReleaseError(f"Unexpected image in official distribution: {relative.as_posix()}")
            source_asset = application_root / "assets" / Path(*relative.parts[2:])
            if not source_asset.is_file() or sha256_file(source_asset) != sha256_file(path):
                raise ReleaseError(f"Unreviewed image in official distribution: {relative.as_posix()}")
    assignments = native_artifact_owners(dist_dir, license_inventory)
    unmatched = [relative for relative, owners in assignments.items() if not owners]
    if unmatched:
        raise ReleaseError(
            "Native release artifacts lack a license mapping: " + ", ".join(unmatched)
        )
    ambiguous = [relative for relative, owners in assignments.items() if len(owners) != 1]
    if ambiguous:
        raise ReleaseError(
            "Native release artifacts have ambiguous license ownership: "
            + ", ".join(ambiguous)
        )
    missing_embedded = missing_embedded_artifact_mappings(dist_dir, license_inventory)
    if missing_embedded:
        raise ReleaseError(
            "Embedded component mapping does not match the release: "
            + ", ".join(f"{component}:{pattern}" for component, pattern in missing_embedded)
        )
    for path in files:
        relative = path.relative_to(dist_dir)
        destination = portable_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def copy_release_documents(
    portable_root: Path,
    application_root: Path = PROJECT_ROOT,
    license_inventory: Path | None = None,
) -> None:
    for source_name, destination_name in ROOT_DOCUMENTS.items():
        source = application_root / source_name
        if not source.is_file():
            raise ReleaseError(f"Required release document is missing: {source_name}")
        if is_reparse_or_link(source):
            raise ReleaseError(f"Release document may not be a link/reparse point: {source_name}")
        shutil.copy2(source, portable_root / destination_name)
    source_licenses = application_root / "licenses"
    destination_licenses = portable_root / "licenses"
    destination_licenses.mkdir()
    for source in safe_files(source_licenses):
        target = destination_licenses / source.relative_to(source_licenses)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(
        license_inventory or application_root / "tools/release/third_party_licenses.json",
        destination_licenses / "third_party_licenses.json",
    )


def _verified_build_environment(requirements_path: Path | None = None) -> dict[str, object]:
    expected = exact_requirements(requirements_path)
    installed: dict[str, str] = {}
    for name, version in expected.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ReleaseError(f"Release dependency is not installed: {name}") from exc
        if actual != version:
            raise ReleaseError(
                f"Release dependency version mismatch for {name}: expected {version}, found {actual}"
            )
        installed[name] = actual
    if platform.python_version() != "3.11.9":
        raise ReleaseError(
            f"Release Python must be exactly 3.11.9, found {platform.python_version()}."
        )
    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.0.13 "):
        raise ReleaseError("Release Python must use the audited OpenSSL 3.0.13 runtime.")
    if sqlite3.sqlite_version != "3.45.1":
        raise ReleaseError("Release Python must use the audited SQLite 3.45.1 runtime.")
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "openssl": ssl.OPENSSL_VERSION,
        "sqlite": sqlite3.sqlite_version,
        "pyinstaller": installed["pyinstaller"],
        "dependencies": installed,
    }


def _resolve_source(
    source_commit: str,
    *,
    repository_root: Path = PROJECT_ROOT,
    require_clean: bool,
) -> tuple[str, str]:
    resolved = _git(repository_root, "rev-parse", f"{source_commit}^{{commit}}")
    tree = _git(repository_root, "rev-parse", f"{resolved}^{{tree}}")
    if require_clean:
        if _git(repository_root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ReleaseError("An exact public source snapshot requires a clean working tree.")
        if resolved != _git(repository_root, "rev-parse", "HEAD"):
            raise ReleaseError("The requested public source commit is not the checked-out HEAD.")
    return resolved, tree


def _resolve_tagged_source(
    application_root: Path,
    source_tag: str,
    source_commit: str | None,
    *,
    require_clean: bool,
    release_version: str = APP_VERSION,
) -> tuple[str, str, str]:
    version = validate_release_version(release_version)
    if source_tag != f"v{version}" or source_tag != Path(source_tag).name:
        raise ReleaseError("The public source tag is invalid.")
    tag_ref = f"refs/tags/{source_tag}"
    if _git(application_root, "cat-file", "-t", tag_ref) != "tag":
        raise ReleaseError("The public source tag must be an exact annotated tag.")
    tag_object = _git(application_root, "rev-parse", tag_ref)
    tagged_commit = _git(application_root, "rev-parse", f"{tag_ref}^{{commit}}")
    if source_commit is not None:
        requested = _git(application_root, "rev-parse", f"{source_commit}^{{commit}}")
        if requested != tagged_commit:
            raise ReleaseError("The requested source commit does not match the public tag.")
    commit, tree = _resolve_source(
        tagged_commit,
        repository_root=application_root,
        require_clean=require_clean,
    )
    return tag_object, commit, tree


def build_manifest(
    portable_root: Path,
    *,
    source_tag: str,
    source_tag_object: str,
    source_commit: str,
    source_tree_hash: str,
    release_tooling_commit: str,
    release_tooling_tree_hash: str,
    release_license_inventory_git_blob: str,
    release_license_inventory_sha256: str,
    build_timestamp: str,
    build_environment: dict[str, object],
    release_version: str = APP_VERSION,
    release_channel: str = RELEASE_CHANNEL,
) -> dict[str, object]:
    records = canonical_file_records(
        portable_root,
        excluded={"release-manifest.json", "SHA256SUMS.txt"},
    )
    executable = portable_root / "MusicVault.exe"
    return {
        "manifest_schema_version": 2,
        "product_name": PRODUCT_NAME,
        "version": validate_release_version(release_version),
        "release_channel": release_channel,
        "platform": "Windows",
        "architecture": "x64",
        "python_version": build_environment["python"],
        "pyinstaller_version": build_environment["pyinstaller"],
        "sqlite_schema_version": 4,
        "app_status_schema_version": 1,
        "source_tag": source_tag,
        "source_tag_object": source_tag_object,
        "source_commit": source_commit,
        "source_tree_hash": source_tree_hash,
        "release_tooling_commit": release_tooling_commit,
        "release_tooling_tree_hash": release_tooling_tree_hash,
        "release_license_inventory_git_blob": release_license_inventory_git_blob,
        "release_license_inventory_sha256": release_license_inventory_sha256,
        "build_timestamp_utc": build_timestamp,
        "portable_root_marker_version": PORTABLE_MARKER_VERSION,
        "build_environment": build_environment,
        "dependencies": build_environment["dependencies"],
        "executable_sha256": sha256_file(executable),
        "package_sha256": canonical_payload_hash(records),
        "package_sha256_scope": "canonical package payload before manifest/checksum files",
        "file_count": len(records) + 2,
        "license_notice_version": 1,
        "ffmpeg_bundled": False,
        "ffmpeg_note": "Qt Multimedia contains LGPL FFmpeg libraries; ffmpeg.exe/ffprobe.exe are not bundled.",
        "api_credentials_bundled": False,
        "runtime_data_bundled": False,
        "files": records,
    }


def write_package_checksums(portable_root: Path) -> None:
    records = canonical_file_records(portable_root, excluded={"SHA256SUMS.txt"})
    lines = [f"{record['sha256']}  {record['path']}" for record in records]
    (portable_root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_git_commit(
    destination: Path, source_commit: str, application_root: Path = PROJECT_ROOT
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination.parent / "music-vault-exact-source.zip"
    command = [
        "git", "-c", f"safe.directory={application_root.resolve().as_posix()}",
        "archive", "--format=zip", f"--output={archive_path}", source_commit,
    ]
    completed = subprocess.run(
        command, cwd=application_root, check=False, capture_output=True, timeout=60
    )
    if completed.returncode:
        raise ReleaseError("The exact Git source archive could not be created.")
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                relative = validate_zip_name(info.filename)
                folded = relative.as_posix().casefold()
                if folded in seen:
                    raise ReleaseError("The exact Git source archive has a duplicate path.")
                seen.add(folded)
                mode = (info.external_attr >> 16) & 0xFFFF
                if (mode & 0o170000) == 0o120000:
                    raise ReleaseError("The exact Git source archive contains a symlink.")
                target = destination.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    finally:
        archive_path.unlink(missing_ok=True)


def _copy_corresponding_sources(
    destination: Path,
    source_cache: Path,
    license_inventory: Path,
) -> list[dict[str, object]]:
    try:
        from .fetch_compliance_sources import fetch_sources
    except ImportError:
        from fetch_compliance_sources import fetch_sources

    rows = fetch_sources(
        source_cache,
        offline=True,
        inventory_path=license_inventory,
    )
    target_root = destination / "third-party-sources"
    target_root.mkdir()
    for row in rows:
        source = source_cache / str(row["filename"])
        target = target_root / source.name
        shutil.copy2(source, target)
        if sha256_file(target) != row["sha256"]:
            raise ReleaseError(f"Copied source archive hash mismatch: {source.name}")
    return rows


def _write_source_availability(
    destination: Path,
    source_tag: str,
    source_tag_object: str,
    source_commit: str,
    source_tree_hash: str,
    release_tooling_commit: str,
    release_tooling_tree_hash: str,
    release_license_inventory_git_blob: str,
    release_license_inventory_sha256: str,
    archives: list[dict[str, object]],
    release_version: str = APP_VERSION,
) -> None:
    lines = [
        f"# Music Vault v{validate_release_version(release_version)} source compliance",
        "",
        f"Music Vault source tag: `{source_tag}`",
        f"Music Vault tag object: `{source_tag_object}`",
        f"Music Vault source commit: `{source_commit}`",
        f"Music Vault source tree: `{source_tree_hash}`",
        f"Release tooling commit: `{release_tooling_commit}`",
        f"Release tooling tree: `{release_tooling_tree_hash}`",
        f"Corrected release license inventory Git blob: `{release_license_inventory_git_blob}`",
        f"Corrected release license inventory SHA-256: `{release_license_inventory_sha256}`",
        "",
        "This archive contains the exact Git-committed Music Vault source and build inputs.",
        "It also carries the unmodified, hash-pinned corresponding-source archives under",
        "`third-party-sources/`; public compliance does not depend on an upstream link",
        "remaining available after this release.",
        "",
    ]
    for row in archives:
        lines.append(
            f"- `{row['filename']}` - {row['component']} - SHA-256 `{row['sha256']}`"
        )
    lines.extend([
        "",
        "## LGPL replacement and relinking",
        "",
        "The portable build is deliberately one-folder. Qt/PySide, Shiboken, and Qt's",
        "FFmpeg libraries are separate files under `_internal/PySide6` and",
        "`_internal/shiboken6`. A recipient may rebuild from this source using Python",
        "3.11 and `requirements-release.txt`, or replace compatible LGPL-covered DLLs",
        "in a copied portable folder. Keep matching ABI/version families and preserve",
        "the original folder layout. No term of this distribution prohibits reverse",
        "engineering for debugging a modification to an LGPL-covered component.",
        "",
        "The default release does not contain ffmpeg.exe or ffprobe.exe.",
    ])
    (destination / "SOURCE_COMPLIANCE_README.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def build_source_compliance(
    output_dir: Path,
    staging_owner: Path,
    source_commit: str,
    *,
    application_root: Path = PROJECT_ROOT,
    source_tag: str,
    source_tag_object: str,
    source_tree_hash: str | None = None,
    release_tooling_commit: str,
    release_tooling_tree_hash: str,
    license_inventory: Path,
    release_license_inventory_git_blob: str,
    release_license_inventory_sha256: str,
    source_cache: Path | None = None,
    release_version: str = APP_VERSION,
) -> tuple[Path, Path]:
    release_version = validate_release_version(release_version)
    staging = staging_owner / "source-compliance"
    if staging.exists():
        raise ReleaseError("Unique source-compliance staging unexpectedly exists.")
    tree_hash = source_tree_hash or _git(
        application_root, "rev-parse", f"{source_commit}^{{tree}}"
    )
    _export_git_commit(staging, source_commit, application_root)
    tooling_inventory = staging / "release-tooling" / "third_party_licenses.json"
    tooling_inventory.parent.mkdir()
    shutil.copy2(license_inventory, tooling_inventory)
    inventory_sha256 = sha256_file(tooling_inventory)
    if (
        inventory_sha256 != release_license_inventory_sha256
        or _git(
            PROJECT_ROOT,
            "hash-object",
            f"--path={RELEASE_LICENSE_INVENTORY_PATH}",
            "--",
            str(tooling_inventory),
        )
        != release_license_inventory_git_blob
    ):
        raise ReleaseError("Copied release license inventory lost its Git provenance.")
    archive_rows = _copy_corresponding_sources(
        staging,
        (source_cache or output_dir / ".source-cache").resolve(),
        license_inventory,
    )
    write_json(staging / "source-snapshot.json", {
        "schema_version": 2,
        "source_kind": "exact Git commit",
        "source_tag": source_tag,
        "source_tag_object": source_tag_object,
        "source_commit": source_commit,
        "source_tree_hash": tree_hash,
        "release_tooling_commit": release_tooling_commit,
        "release_tooling_tree_hash": release_tooling_tree_hash,
        "release_license_inventory_git_blob": release_license_inventory_git_blob,
        "release_license_inventory_sha256": inventory_sha256,
        "repository": "https://github.com/jcjohnson9/music-vault",
    })
    _write_source_availability(
        staging,
        source_tag,
        source_tag_object,
        source_commit,
        tree_hash,
        release_tooling_commit,
        release_tooling_tree_hash,
        release_license_inventory_git_blob,
        inventory_sha256,
        archive_rows,
        release_version,
    )
    records = canonical_file_records(staging, excluded={"source-compliance-manifest.json"})
    write_json(staging / "source-compliance-manifest.json", {
        "manifest_schema_version": 2,
        "product": PRODUCT_NAME,
        "version": release_version,
        "source_tag": source_tag,
        "source_tag_object": source_tag_object,
        "source_commit": source_commit,
        "source_tree_hash": tree_hash,
        "release_tooling_commit": release_tooling_commit,
        "release_tooling_tree_hash": release_tooling_tree_hash,
        "release_license_inventory_git_blob": release_license_inventory_git_blob,
        "release_license_inventory_sha256": inventory_sha256,
        "payload_sha256": canonical_payload_hash(records),
        "file_count": len(records) + 1,
        "corresponding_source_archives": archive_rows,
        "files": records,
    })
    compliance_filename = compliance_filename_for(release_version)
    destination = output_dir / compliance_filename
    deterministic_zip(
        staging,
        destination,
        prefix=f"MusicVault-v{release_version}-Source-Compliance",
    )
    checksum = output_dir / f"{compliance_filename}.sha256"
    checksum.write_text(f"{sha256_file(destination)}  {destination.name}\n", encoding="ascii")
    return destination, checksum


def build_release(
    output_dir: Path,
    dist_dir: Path,
    source_commit: str | None = None,
    *,
    application_root: Path = PROJECT_ROOT,
    source_tag: str | None = None,
    release_tooling_commit: str | None = None,
    license_inventory: Path | None = None,
    require_clean_source: bool = True,
    source_cache: Path | None = None,
    release_version: str | None = None,
) -> dict[str, object]:
    if platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise ReleaseError("The Windows x64 portable release requires a 64-bit x86 build host.")
    application_root = application_root.expanduser().resolve()
    dist_dir = dist_dir.expanduser().resolve()
    inventory_candidate = (
        license_inventory
        or PROJECT_ROOT / RELEASE_LICENSE_INVENTORY_PATH
    )
    requirements_path = application_root / "requirements-release.txt"
    if not requirements_path.is_file():
        raise ReleaseError("The tagged application release lock is missing.")
    application_version, application_channel = _application_release_identity(application_root)
    if release_version is not None and validate_release_version(release_version) != application_version:
        raise ReleaseError("The requested release version does not match the tagged application.")
    release_version = application_version
    output_dir = validate_output(output_dir, dist_dir, application_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_tag = source_tag or f"v{release_version}"
    tag_object, commit, tree_hash = _resolve_tagged_source(
        application_root,
        resolved_tag,
        source_commit,
        require_clean=require_clean_source,
        release_version=release_version,
    )
    tooling_commit, tooling_tree_hash = _resolve_source(
        release_tooling_commit or _git(PROJECT_ROOT, "rev-parse", "HEAD"),
        repository_root=PROJECT_ROOT,
        require_clean=require_clean_source,
    )
    inventory_path, inventory_blob_id, inventory_sha256 = _resolve_release_inventory(
        inventory_candidate, tooling_commit
    )
    build_environment = _verified_build_environment(requirements_path)
    staging_owner = Path(tempfile.mkdtemp(prefix=".staging-", dir=output_dir))
    try:
        package_directory = package_directory_for(release_version)
        package_filename = package_filename_for(release_version)
        portable_root = staging_owner / package_directory
        portable_root.mkdir()

        copy_distribution(
            dist_dir,
            portable_root,
            application_root,
            inventory_path,
        )
        copy_release_documents(portable_root, application_root, inventory_path)
        write_json(portable_root / PORTABLE_MARKER, {
            "schema_version": PORTABLE_MARKER_VERSION,
            "product": PRODUCT_NAME,
            "version": release_version,
            "portable": True,
            "data_directory": "data",
        })
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        internal_manifest = build_manifest(
            portable_root,
            source_tag=resolved_tag,
            source_tag_object=tag_object,
            source_commit=commit,
            source_tree_hash=tree_hash,
            release_tooling_commit=tooling_commit,
            release_tooling_tree_hash=tooling_tree_hash,
            release_license_inventory_git_blob=inventory_blob_id,
            release_license_inventory_sha256=inventory_sha256,
            build_timestamp=timestamp,
            build_environment=build_environment,
            release_version=release_version,
            release_channel=application_channel,
        )
        write_json(portable_root / "release-manifest.json", internal_manifest)
        write_package_checksums(portable_root)

        for path in safe_files(portable_root):
            relative = path.relative_to(portable_root).as_posix()
            violation = violation_for_path(relative)
            if violation:
                raise ReleaseError(f"Portable package contains {violation}: {relative}")
            for issue in scan_sensitive_bytes(path):
                raise ReleaseError(f"Portable package failed the {issue} scan: {relative}")

        package_path = output_dir / package_filename
        deterministic_zip(portable_root, package_path, prefix=package_directory)
        actual_package_hash = sha256_file(package_path)
        package_checksum = output_dir / f"{package_filename}.sha256"
        package_checksum.write_text(
            f"{actual_package_hash}  {package_path.name}\n", encoding="ascii"
        )
        external_manifest = dict(internal_manifest)
        external_manifest["package_sha256"] = actual_package_hash
        external_manifest["package_sha256_scope"] = "final ZIP file"
        external_manifest["package_filename"] = package_path.name
        write_json(output_dir / "release-manifest.json", external_manifest)

        compliance_path, compliance_checksum = build_source_compliance(
            output_dir,
            staging_owner,
            commit,
            application_root=application_root,
            source_tag=resolved_tag,
            source_tag_object=tag_object,
            source_tree_hash=tree_hash,
            release_tooling_commit=tooling_commit,
            release_tooling_tree_hash=tooling_tree_hash,
            license_inventory=inventory_path,
            release_license_inventory_git_blob=inventory_blob_id,
            release_license_inventory_sha256=inventory_sha256,
            source_cache=source_cache,
            release_version=release_version,
        )
        return {
            "portable_zip": str(package_path),
            "portable_sha256": actual_package_hash,
            "portable_size": package_path.stat().st_size,
            "portable_checksum": str(package_checksum),
            "release_manifest": str(output_dir / "release-manifest.json"),
            "source_compliance_zip": str(compliance_path),
            "source_compliance_sha256": sha256_file(compliance_path),
            "source_compliance_checksum": str(compliance_checksum),
        }
    finally:
        if is_reparse_or_link(staging_owner):
            raise ReleaseError("Release staging became a symlink or reparse point.")
        shutil.rmtree(staging_owner)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the clean Music Vault portable release.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "release_artifacts")
    parser.add_argument("--dist-dir", type=Path, default=PROJECT_ROOT / "dist" / "MusicVault")
    parser.add_argument("--application-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--source-tag")
    parser.add_argument("--release-version")
    parser.add_argument("--source-commit")
    parser.add_argument("--release-tooling-commit")
    parser.add_argument(
        "--license-inventory",
        type=Path,
        default=PROJECT_ROOT / RELEASE_LICENSE_INVENTORY_PATH,
    )
    parser.add_argument("--source-cache", type=Path)
    # Retained for explicit CI readability; public release builds are always
    # clean-source builds even when the flag is omitted by the local wrapper.
    parser.add_argument("--require-clean-source", action="store_true", default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_release(
            args.output_dir,
            args.dist_dir,
            args.source_commit,
            application_root=args.application_root,
            source_tag=args.source_tag,
            release_tooling_commit=args.release_tooling_commit,
            license_inventory=args.license_inventory,
            require_clean_source=args.require_clean_source,
            source_cache=args.source_cache,
            release_version=args.release_version,
        )
    except (OSError, ReleaseError, ValueError, json.JSONDecodeError) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
