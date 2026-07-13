from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from music_vault.version import APP_VERSION, RELEASE_CHANNEL
from tools.release import build_portable_release as builder
from tools.release import generate_qt_attributions
from tools.release import release_common
from tools.release import verify_portable_release as verifier


@pytest.fixture()
def synthetic_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    dist = tmp_path / "official-dist"
    dist.mkdir()
    (dist / "MusicVault.exe").write_bytes(b"synthetic-pe")
    (dist / "_internal").mkdir()
    (dist / "_internal" / "runtime.bin").write_bytes(b"synthetic-runtime")

    def compliance(output_dir: Path, staging_owner: Path, source_commit: str, **kwargs):
        archive = output_dir / release_common.COMPLIANCE_FILENAME
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("source/README.txt", "synthetic source")
        checksum = output_dir / f"{archive.name}.sha256"
        checksum.write_text(
            f"{release_common.sha256_file(archive)}  {archive.name}\n", encoding="ascii"
        )
        return archive, checksum

    def release_documents(portable_root: Path) -> None:
        documents = {
            "README_FIRST_RUN.md": "Synthetic first-run guide\n",
            "LICENSE": "MIT License\n",
            "THIRD_PARTY_NOTICES.md": "This is not an MIT-only binary.\n",
            "BINARY_DISTRIBUTION_LICENSE.md": "Synthetic binary terms\n",
            "AUTHORIZED_USE.md": "Synthetic authorized-use notice\n",
        }
        for name, contents in documents.items():
            (portable_root / name).write_text(contents, encoding="utf-8")
        licenses = portable_root / "licenses"
        licenses.mkdir()
        shutil.copy2(
            release_common.PROJECT_ROOT / "tools" / "release" / "third_party_licenses.json",
            licenses / "third_party_licenses.json",
        )
        (licenses / "GPL-3.0.txt").write_text("Synthetic GPL text\n", encoding="utf-8")

    monkeypatch.setattr(builder, "build_source_compliance", compliance)
    monkeypatch.setattr(builder, "copy_release_documents", release_documents)
    monkeypatch.setattr(builder, "missing_embedded_artifact_mappings", lambda *args: [])
    def git_value(*args: str) -> str:
        value = args[-1]
        if value.endswith("^{tree}"):
            return "c" * 40
        if value.endswith("^{commit}"):
            return "b" * 40
        if value == "HEAD":
            return "b" * 40
        return ""

    monkeypatch.setattr(builder, "git_value", git_value)
    output = tmp_path / "release-output"
    result = builder.build_release(output, dist, source_commit="b" * 40)
    monkeypatch.setattr(
        verifier,
        "_pe_version",
        lambda path: (
            "1.0.0.0",
            "1.0.0.0",
            {
                "ProductName": "Music Vault",
                "FileDescription": "Music Vault",
                "OriginalFilename": "MusicVault.exe",
                "FileVersion": "1.0.0.0",
            },
        ),
    )
    monkeypatch.setattr(verifier, "verify_source_compliance", lambda *args, **kwargs: [])
    monkeypatch.setattr(verifier, "verify_license_inventory", lambda *args, **kwargs: [])
    monkeypatch.setattr(verifier, "missing_embedded_artifact_mappings", lambda *args: [])
    extract = tmp_path / "extracted"
    extract.mkdir()
    root = verifier.safe_extract(Path(result["portable_zip"]), extract)
    return {
        "dist": dist,
        "output": output,
        "zip": Path(result["portable_zip"]),
        "root": root,
    }


def test_central_release_version_and_lock_are_exact() -> None:
    assert APP_VERSION == "1.0.0"
    assert RELEASE_CHANNEL == "stable"
    requirements = release_common.exact_requirements()
    assert {
        "PySide6": "6.11.1",
        "mutagen": "1.47.0",
        "musicbrainzngs": "0.7.1",
        "requests": "2.34.2",
        "yt-dlp": "2026.6.9",
        "pyinstaller": "6.21.0",
        "pytest": "8.4.2",
    }.items() <= requirements.items()
    assert all(version and not any(marker in version for marker in (">", "<", "~", "*")) for version in requirements.values())


def test_builder_writes_clean_versioned_release(synthetic_release: dict[str, Path]) -> None:
    root = synthetic_release["root"]
    findings = verifier.verify(synthetic_release["zip"])
    assert not findings, [finding.render() for finding in findings]
    marker = json.loads((root / release_common.PORTABLE_MARKER).read_text(encoding="utf-8"))
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    assert marker == {
        "data_directory": "data",
        "portable": True,
        "product": "Music Vault",
        "schema_version": 1,
        "version": APP_VERSION,
    }
    assert manifest["version"] == APP_VERSION
    assert manifest["release_channel"] == RELEASE_CHANNEL
    assert manifest["sqlite_schema_version"] == 4
    assert manifest["app_status_schema_version"] == 1
    assert manifest["ffmpeg_bundled"] is False
    assert manifest["api_credentials_bundled"] is False
    assert manifest["runtime_data_bundled"] is False
    assert not (root / "data").exists()
    assert (root / "THIRD_PARTY_NOTICES.md").is_file()
    assert (root / "licenses" / "GPL-3.0.txt").is_file()


def test_builder_rejects_live_data_as_staging(tmp_path: Path) -> None:
    with pytest.raises(release_common.ReleaseError, match="live data"):
        builder.validate_output(release_common.PROJECT_ROOT / "data", tmp_path / "dist")


def test_public_builder_rejects_dirty_or_non_head_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40

    def dirty_git(*args: str) -> str:
        if args[0] == "status":
            return " M music_vault/app.py"
        if args[-1].endswith("^{commit}") or args[-1] == "HEAD":
            return head
        if args[-1].endswith("^{tree}"):
            return "b" * 40
        return ""

    monkeypatch.setattr(builder, "git_value", dirty_git)
    with pytest.raises(release_common.ReleaseError, match="clean working tree"):
        builder._resolve_source(head, require_clean=True)

    def wrong_head_git(*args: str) -> str:
        if args[0] == "status":
            return ""
        if args[-1].endswith("^{commit}"):
            return head
        if args[-1].endswith("^{tree}"):
            return "b" * 40
        if args[-1] == "HEAD":
            return "c" * 40
        return ""

    monkeypatch.setattr(builder, "git_value", wrong_head_git)
    with pytest.raises(release_common.ReleaseError, match="checked-out HEAD"):
        builder._resolve_source(head, require_clean=True)


@pytest.mark.parametrize(
    ("relative", "expected_rule"),
    [
        ("data/music_vault.sqlite3", "database"),
        ("data/track.mp3", "media"),
        ("data/youtube_api_key.txt", "runtime"),
        ("covers/private.png", "private"),
        ("ffmpeg.exe", "FFmpeg"),
    ],
)
def test_builder_rejects_forbidden_distribution_inputs(
    tmp_path: Path, relative: str, expected_rule: str
) -> None:
    dist = tmp_path / "dist"
    (dist / "MusicVault.exe").parent.mkdir(parents=True)
    (dist / "MusicVault.exe").write_bytes(b"exe")
    injected = dist / relative
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_bytes(b"private")
    with pytest.raises(release_common.ReleaseError, match=expected_rule):
        builder.copy_distribution(dist, tmp_path / "portable")


def test_builder_rejects_unmapped_native_artifact(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "MusicVault.exe").write_bytes(b"exe")
    (dist / "_internal").mkdir()
    (dist / "_internal" / "unknown.dll").write_bytes(b"native")
    with pytest.raises(release_common.ReleaseError, match="license mapping"):
        builder.copy_distribution(dist, tmp_path / "portable")


def test_builder_allows_only_pyinstaller_base_library_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    internal = dist / "_internal"
    internal.mkdir(parents=True)
    (dist / "MusicVault.exe").write_bytes(b"synthetic-pe")
    with zipfile.ZipFile(internal / "base_library.zip", "w") as archive:
        archive.writestr("types.pyc", b"synthetic-bytecode")

    monkeypatch.setattr(builder, "missing_embedded_artifact_mappings", lambda *args: [])
    builder.copy_distribution(dist, tmp_path / "portable")
    assert (tmp_path / "portable" / "_internal" / "base_library.zip").is_file()

    with zipfile.ZipFile(internal / "unexpected.zip", "w") as archive:
        archive.writestr("note.txt", b"unexpected")
    with pytest.raises(release_common.ReleaseError, match="nested archive"):
        builder.copy_distribution(dist, tmp_path / "portable-2")


def test_native_scanner_distinguishes_qt_markers_from_real_private_data(
    tmp_path: Path,
) -> None:
    private_key_header = b"-----BEGIN " + b"PRIVATE KEY-----"
    qt_binary = tmp_path / "Qt6Network.dll"
    qt_binary.write_bytes(
        b"MZ" + b"C:\\Users\\" + b"qt\\work\\qtbase" + private_key_header
    )
    assert release_common.scan_sensitive_bytes(qt_binary) == []

    local_binary = tmp_path / "local.dll"
    local_binary.write_bytes(b"MZ" + str(release_common.PROJECT_ROOT).encode("utf-8"))
    assert "personal absolute path" in release_common.scan_sensitive_bytes(local_binary)

    key_binary = tmp_path / "key.dll"
    key_binary.write_bytes(
        b"MZ" + private_key_header + b"\n"
        + b"A" * 64
        + b"\n"
        + b"B" * 64
        + b"\n-----END PRIVATE KEY-----\n"
    )
    assert "private key" in release_common.scan_sensitive_bytes(key_binary)


def test_base_library_archive_is_scanned_after_decompression(tmp_path: Path) -> None:
    archive_path = tmp_path / "base_library.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("types.pyc", b"AIza" + b"A" * 35)
    assert "likely Google API key" in release_common.scan_sensitive_bytes(archive_path)


def test_source_compliance_scanner_reports_personal_path_without_crashing(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / f"MusicVault-v{APP_VERSION}-Source-Compliance"
    source_root.mkdir()
    (source_root / "note.txt").write_bytes(
        b"C:\\Users\\" + b"private\\Music"
    )
    archive = tmp_path / release_common.COMPLIANCE_FILENAME
    release_common.deterministic_zip(source_root, archive, prefix=source_root.name)
    archive.with_name(archive.name + ".sha256").write_text(
        f"{release_common.sha256_file(archive)}  {archive.name}\n",
        encoding="ascii",
    )

    findings = verifier.verify_source_compliance(archive)
    assert any(
        finding.path == "note.txt" and finding.rule == "personal absolute path"
        for finding in findings
    )


def test_zip_entries_are_sorted_relative_and_unique(synthetic_release: dict[str, Path]) -> None:
    with zipfile.ZipFile(synthetic_release["zip"]) as archive:
        names = archive.namelist()
    assert names == sorted(names, key=str.casefold)
    assert len(names) == len({name.casefold() for name in names})
    assert all(not Path(name).is_absolute() for name in names)
    assert all(".." not in Path(name).parts for name in names)


def test_manifest_and_package_hashes_reconcile(synthetic_release: dict[str, Path]) -> None:
    root = synthetic_release["root"]
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        path = root / record["path"]
        assert path.stat().st_size == record["size"]
        assert release_common.sha256_file(path) == record["sha256"]
    assert release_common.canonical_payload_hash(manifest["files"]) == manifest["package_sha256"]
    external = json.loads((synthetic_release["output"] / "release-manifest.json").read_text(encoding="utf-8"))
    assert external["package_sha256"] == release_common.sha256_file(synthetic_release["zip"])


def test_verifier_rejects_external_package_checksum_mismatch(
    synthetic_release: dict[str, Path],
) -> None:
    checksum = synthetic_release["zip"].with_name(synthetic_release["zip"].name + ".sha256")
    checksum.write_text("0" * 64 + f"  {synthetic_release['zip'].name}\n", encoding="ascii")
    with pytest.raises(release_common.ReleaseError, match="checksum mismatch"):
        verifier.verify(synthetic_release["zip"])


def test_verifier_rejects_external_manifest_hash_mismatch(
    synthetic_release: dict[str, Path],
) -> None:
    manifest_path = synthetic_release["output"] / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(release_common.ReleaseError, match="manifest package hash"):
        verifier.verify(synthetic_release["zip"])


def test_zip_verifier_rejects_traversal_and_absolute_entries(tmp_path: Path) -> None:
    for name in ("../escape.txt", "/absolute.txt", "C:/absolute.txt"):
        archive = tmp_path / (name.replace("/", "_").replace(":", "_") + ".zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(name, "unsafe")
        with pytest.raises(release_common.ReleaseError):
            verifier.inspect_zip_structure(archive)


@pytest.mark.parametrize(
    "name",
    [
        "package/note.txt:secret",
        "package/CON.txt",
        "package/com1",
        "package/trailing.",
        "package/trailing ",
        "package//duplicate-separator.txt",
        "package/control\x01.txt",
    ],
)
def test_zip_verifier_rejects_windows_unsafe_entries(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "windows-unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(name, "unsafe")
    with pytest.raises(release_common.ReleaseError):
        verifier.inspect_zip_structure(archive)


def test_safe_extract_rejects_extra_macos_top_level(tmp_path: Path) -> None:
    archive = tmp_path / "extra-root.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("MusicVault/MusicVault.exe", "synthetic")
        handle.writestr("__MACOSX/metadata", "unexpected")
    with pytest.raises(release_common.ReleaseError, match="one top-level"):
        verifier.safe_extract(archive, tmp_path / "extract")


def test_safe_extract_requires_empty_destination(tmp_path: Path) -> None:
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("MusicVault/MusicVault.exe", "synthetic")
    destination = tmp_path / "extract"
    destination.mkdir()
    (destination / "preexisting.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(release_common.ReleaseError, match="empty"):
        verifier.safe_extract(archive, destination)


def test_safe_files_rejects_symlink_or_reparse_point(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("The test identity cannot create symlinks on this Windows host.")
    with pytest.raises(release_common.ReleaseError, match="reparse point"):
        release_common.safe_files(root)


def _copy_release(synthetic_release: dict[str, Path], tmp_path: Path) -> Path:
    destination = tmp_path / "mutated"
    shutil.copytree(synthetic_release["root"], destination)
    return destination


@pytest.mark.parametrize(
    ("mutation", "category"),
    [
        ("database", "forbidden content"),
        ("media", "forbidden content"),
        ("api-key", "sensitive content"),
        ("personal-path", "sensitive content"),
        ("ffmpeg", "forbidden content"),
    ],
)
def test_verifier_rejects_private_or_unexpected_injections(
    synthetic_release: dict[str, Path], tmp_path: Path, mutation: str, category: str
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    if mutation == "database":
        (root / "music_vault.sqlite3").write_bytes(b"db")
    elif mutation == "media":
        (root / "private.mp3").write_bytes(b"media")
    elif mutation == "api-key":
        (root / "note.txt").write_text("AIza" + "A" * 35, encoding="utf-8")
    elif mutation == "personal-path":
        (root / "note.txt").write_text(
            "C:\\Users\\" + "private\\Music", encoding="utf-8"
        )
    else:
        (root / "ffmpeg.exe").write_bytes(b"cli")
    findings = verifier.verify_directory(root)
    assert any(finding.category == category for finding in findings)
    rendered = "\n".join(finding.render() for finding in findings)
    assert "AIza" not in rendered


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("benign.txt", b"private", "unexpected portable-root content"),
        ("renamed.bin", b"SQLite format 3\x00" + b"\x00" * 32, "renamed SQLite database"),
        ("renamed.dat", b"ID3" + b"\x00" * 32, "renamed media file"),
    ],
)
def test_verifier_rejects_unexpected_root_and_renamed_payloads(
    synthetic_release: dict[str, Path], tmp_path: Path, name: str, payload: bytes, expected: str
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    (root / name).write_bytes(payload)
    findings = verifier.verify_directory(root)
    assert any(expected in finding.rule for finding in findings)


def test_verifier_rejects_large_file_secret(
    synthetic_release: dict[str, Path], tmp_path: Path
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    payload = b"x" * (6 * 1024 * 1024) + b"AIza" + b"A" * 35
    (root / "_internal" / "large.bin").write_bytes(payload)
    findings = verifier.verify_directory(root)
    assert any(finding.rule == "likely Google API key" for finding in findings)


def test_verifier_rejects_incomplete_checksum_inventory(
    synthetic_release: dict[str, Path], tmp_path: Path
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    checksum = root / "SHA256SUMS.txt"
    checksum.write_text(checksum.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    findings = verifier.verify_directory(root)
    assert any(finding.rule == "checksum inventory is incomplete or has unexpected paths" for finding in findings)


def test_verifier_rejects_missing_license(
    synthetic_release: dict[str, Path], tmp_path: Path
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    (root / "licenses" / "GPL-3.0.txt").unlink()
    findings = verifier.verify_directory(root)
    paths = {finding.path for finding in findings}
    assert "licenses/GPL-3.0.txt" in paths


def test_verifier_rejects_missing_portable_marker(
    synthetic_release: dict[str, Path], tmp_path: Path
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    (root / release_common.PORTABLE_MARKER).unlink()
    findings = verifier.verify_directory(root)
    assert any(finding.path == release_common.PORTABLE_MARKER for finding in findings)


def test_verifier_rejects_wrong_version_and_manifest_hash(
    synthetic_release: dict[str, Path], tmp_path: Path
) -> None:
    root = _copy_release(synthetic_release, tmp_path)
    manifest_path = root / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.0.0"
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    findings = verifier.verify_directory(root)
    rules = {finding.rule for finding in findings}
    assert "product version mismatch" in rules
    assert "manifest file hash/size mismatch" in rules


def test_license_inventory_is_complete_and_binary_is_not_mit_only() -> None:
    inventory = json.loads(
        (release_common.PROJECT_ROOT / "tools/release/third_party_licenses.json").read_text(
            encoding="utf-8"
        )
    )
    direct = {
        "PySide6, PySide6 Essentials/Addons, and Shiboken6",
        "Mutagen",
        "musicbrainzngs",
        "Requests",
        "yt-dlp",
        "PyInstaller bootloader/runtime",
    }
    names = {component["component"] for component in inventory["components"]}
    assert direct <= names
    assert all(component.get("license_identifier") for component in inventory["components"])
    assert any("GPL" in component["license_identifier"] for component in inventory["components"])
    assert inventory["source_compliance_required"] is True
    assert inventory["public_distribution_approved"] is True
    assert len(inventory["corresponding_source_archives"]) == len(
        release_common.AUDITED_SOURCE_ARCHIVES
    )
    source_components = set()
    for row in inventory["corresponding_source_archives"]:
        source_components.add(row["component"])
        source_components.update(row.get("covers_components", []))
    assert all(
        not component.get("source_or_offer_required")
        or component["component"] in source_components
        for component in inventory["components"]
        if component.get("bundled")
    )
    assert {
        "libffi-3.4.4.tar.gz",
        "sqlite-src-3450100.zip",
        "musicbrainzngs-0.7.1.tar.gz",
        "requests-2.34.2.tar.gz",
        "charset_normalizer-3.4.7.tar.gz",
        "idna-3.18.tar.gz",
        "urllib3-2.7.0.tar.gz",
        "yt_dlp-2026.6.9.tar.gz",
        "bzip2-1.0.8.tar.gz",
        "xz-v5.2.5-source.tar.gz",
        "qtimageformats-everywhere-src-6.11.1.tar.xz",
    } <= {row["filename"] for row in inventory["corresponding_source_archives"]}
    assert {
        component["component"]: component["version"]
        for component in inventory["components"]
        if component.get("bundled")
    } == release_common.AUDITED_BUNDLED_COMPONENT_VERSIONS
    assert inventory["product_source_license"] == "MIT"
    assert "GPL" in inventory["binary_distribution_license"]
    notices = (release_common.PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "not an MIT-only binary" in notices
    assert (release_common.PROJECT_ROOT / "licenses" / "qt-attrib" / "INDEX.md").is_file()


def test_exact_embedded_dependency_overlay_and_qt_notices_are_locked() -> None:
    inventory = release_common.load_license_inventory()
    components = {
        component["component"]: component for component in inventory["components"]
    }
    assert len(components) == 74
    assert components["TinyCBOR statically linked into Qt Core"]["version"] == "7.0"
    assert components["DR Libs/dr_wav in Qt Multimedia"]["license_identifier"] == (
        "Unlicense OR MIT-0"
    )
    assert components["XSVG arc-handling code in Qt SVG"]["license_identifier"] == (
        "HPND-sell-variant"
    )
    assert components["FFmpeg shared libraries used by Qt Multimedia"][
        "license_identifier"
    ] == (
        "LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND "
        "BSD-Source-Code AND ISC AND MIT AND MPL-2.0"
    )
    assert components["zlib 1.3.2 used by PyInstaller and Qt"][
        "embedded_in_artifacts"
    ] == ["MusicVault.exe", "_internal/PySide6/Qt6Core.dll"]
    assert components["Wintab API in Qt Windows plugins"]["embedded_in_artifacts"] == [
        "_internal/PySide6/plugins/platforms/qwindows.dll",
        "_internal/PySide6/plugins/platforms/qdirect2d.dll",
    ]

    provenance = json.loads(
        (release_common.PROJECT_ROOT / "licenses/qt-attrib/SOURCE_ARCHIVES.json").read_text(
            encoding="utf-8"
        )
    )
    referenced = {
        (row["field"], row["source_path"], row["packaged_path"])
        for row in provenance["referenced_notice_files"]
    }
    assert any(
        field == "LicenseFiles" and packaged == "qtmultimedia/r/0b3a98707b568d29"
        for field, _source, packaged in referenced
    )
    assert any(
        field == "CopyrightFile" and packaged == "qtbase/r/6ceb7ecf7e167a77.txt"
        for field, _source, packaged in referenced
    )
    for relative in (
        "licenses/qt-attrib/qtmultimedia/r/0b3a98707b568d29",
        "licenses/qt-attrib/qtbase/r/32ab4653be851842",
        "licenses/qt-attrib/qtbase/r/19398e38166d28b8.txt",
        "licenses/qt-attrib/qtbase/r/6ceb7ecf7e167a77.txt",
    ):
        assert (release_common.PROJECT_ROOT / relative).is_file()


def test_qt_notice_reference_fields_include_singular_plural_and_copyright() -> None:
    item = {
        "LicenseFile": "LICENSE",
        "LicenseFiles": ["COPYING", "NOTICE"],
        "CopyrightFile": "COPYRIGHT",
        "CopyrightFiles": ["AUTHORS", "NOTICE"],
    }
    assert generate_qt_attributions._referenced_notice_files(item) == [
        ("LicenseFile", "LICENSE"),
        ("LicenseFiles", "COPYING"),
        ("LicenseFiles", "NOTICE"),
        ("CopyrightFile", "COPYRIGHT"),
        ("CopyrightFiles", "AUTHORS"),
        ("CopyrightFiles", "NOTICE"),
    ]
