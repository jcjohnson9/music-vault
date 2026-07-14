from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from music_vault.version import APP_VERSION
from tools.release import build_portable_release as builder
from tools.release import release_common
from tools.release import validate_release_payload as payload
from tools.release import verify_portable_release as verifier


TAGGED_VERSION = "1.0.0"
SOURCE_TAG = f"v{TAGGED_VERSION}"
SOURCE_TAG_OBJECT = "a" * 40
SOURCE_COMMIT = "b" * 40
SOURCE_TREE = "c" * 40
TOOLING_COMMIT = "d" * 40
TOOLING_TREE = "e" * 40
INVENTORY_BLOB = "1" * 40
INVENTORY_HASH = "f" * 64


def _provenance() -> dict[str, str]:
    return {
        "source_tag": SOURCE_TAG,
        "source_tag_object": SOURCE_TAG_OBJECT,
        "source_commit": SOURCE_COMMIT,
        "source_tree_hash": SOURCE_TREE,
        "release_tooling_commit": TOOLING_COMMIT,
        "release_tooling_tree_hash": TOOLING_TREE,
        "release_license_inventory_git_blob": INVENTORY_BLOB,
        "release_license_inventory_sha256": INVENTORY_HASH,
    }


def _build_environment() -> dict[str, object]:
    return {
        "python": "3.11.9",
        "python_implementation": "CPython",
        "openssl": "OpenSSL 3.0.13 synthetic",
        "sqlite": "3.45.1",
        "pyinstaller": "6.21.0",
        "dependencies": release_common.exact_requirements(),
    }


def _tagged_application(tmp_path: Path) -> Path:
    root = tmp_path / "tagged-application"
    package = root / "music_vault"
    package.mkdir(parents=True)
    (package / "version.py").write_text(
        'APP_VERSION = "1.0.0"\nRELEASE_CHANNEL = "stable"\n',
        encoding="utf-8",
    )
    (root / "requirements-release.txt").write_text("synthetic==1.0\n", encoding="utf-8")
    return root


def test_tooling_1_1_builds_names_and_manifest_from_tagged_1_0_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert APP_VERSION == "1.1.0"  # The release-tooling checkout is newer.
    application = _tagged_application(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "MusicVault.exe").write_bytes(b"synthetic-pe")
    inventory = tmp_path / "third_party_licenses.json"
    inventory.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "release-output"
    resolved: dict[str, object] = {}

    def resolve_tagged(root, tag, commit, *, require_clean, release_version):
        resolved.update(
            root=root,
            tag=tag,
            commit=commit,
            require_clean=require_clean,
            release_version=release_version,
        )
        return SOURCE_TAG_OBJECT, SOURCE_COMMIT, SOURCE_TREE

    def copy_distribution(dist_root, portable_root, *_args):
        shutil.copy2(dist_root / "MusicVault.exe", portable_root / "MusicVault.exe")

    def compliance(output_dir, _staging, _commit, **kwargs):
        assert kwargs["release_version"] == TAGGED_VERSION
        name = release_common.compliance_filename_for(TAGGED_VERSION)
        archive = output_dir / name
        archive.write_bytes(b"synthetic-compliance")
        checksum = output_dir / f"{name}.sha256"
        checksum.write_text(
            f"{release_common.sha256_file(archive)}  {name}\n", encoding="ascii"
        )
        return archive, checksum

    monkeypatch.setattr(builder, "_resolve_tagged_source", resolve_tagged)
    monkeypatch.setattr(
        builder,
        "_resolve_source",
        lambda *_args, **_kwargs: (TOOLING_COMMIT, TOOLING_TREE),
    )
    monkeypatch.setattr(
        builder,
        "_resolve_release_inventory",
        lambda *_args: (inventory, INVENTORY_BLOB, INVENTORY_HASH),
    )
    monkeypatch.setattr(builder, "_verified_build_environment", lambda *_args: _build_environment())
    monkeypatch.setattr(builder, "copy_distribution", copy_distribution)
    monkeypatch.setattr(builder, "copy_release_documents", lambda *_args: None)
    monkeypatch.setattr(builder, "build_source_compliance", compliance)

    result = builder.build_release(
        output,
        dist,
        SOURCE_COMMIT,
        application_root=application,
        source_tag=SOURCE_TAG,
        release_tooling_commit=TOOLING_COMMIT,
        license_inventory=inventory,
        release_version=TAGGED_VERSION,
    )

    portable = Path(result["portable_zip"])
    assert portable.name == "MusicVault-v1.0.0-Windows-x64-Portable.zip"
    manifest = json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == TAGGED_VERSION
    assert manifest["release_channel"] == "stable"
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["release_tooling_commit"] == TOOLING_COMMIT
    assert resolved == {
        "root": application,
        "tag": SOURCE_TAG,
        "commit": SOURCE_COMMIT,
        "require_clean": True,
        "release_version": TAGGED_VERSION,
    }

    with pytest.raises(release_common.ReleaseError, match="does not match"):
        builder.build_release(
            tmp_path / "mismatch-output",
            dist,
            application_root=application,
            release_version=APP_VERSION,
        )


def test_newer_verifier_accepts_explicit_immutable_application_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / release_common.package_directory_for(TAGGED_VERSION)
    root.mkdir()
    for relative in verifier.REQUIRED_ROOT_FILES:
        if relative in {
            "MusicVault.exe",
            release_common.PORTABLE_MARKER,
            "release-manifest.json",
            "SHA256SUMS.txt",
        }:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic\n", encoding="utf-8")
    (root / "MusicVault.exe").write_bytes(b"synthetic-pe")
    release_common.write_json(
        root / release_common.PORTABLE_MARKER,
        {
            "schema_version": 1,
            "product": "Music Vault",
            "version": TAGGED_VERSION,
            "portable": True,
            "data_directory": "data",
        },
    )
    manifest = builder.build_manifest(
        root,
        **_provenance(),
        build_timestamp="2026-01-01T00:00:00Z",
        build_environment=_build_environment(),
        release_version=TAGGED_VERSION,
        release_channel="stable",
    )
    release_common.write_json(root / "release-manifest.json", manifest)
    builder.write_package_checksums(root)

    monkeypatch.setattr(verifier, "scan_sensitive_bytes", lambda *_args: [])
    monkeypatch.setattr(verifier, "verify_release_inventory_anchor", lambda *_args: [])
    monkeypatch.setattr(verifier, "native_artifact_owners", lambda *_args: {})
    monkeypatch.setattr(verifier, "missing_embedded_artifact_mappings", lambda *_args: [])
    monkeypatch.setattr(verifier, "verify_license_inventory", lambda *_args: [])
    monkeypatch.setattr(
        verifier,
        "_pe_version",
        lambda *_args: (
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

    assert verifier.verify_directory(root, release_version=TAGGED_VERSION) == []
    default_rules = {
        finding.rule for finding in verifier.verify_directory(root)
    }
    assert "product version mismatch" in default_rules


def test_transfer_index_uses_tagged_version_filenames_with_newer_tooling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    filenames = payload.payload_filenames_for(TAGGED_VERSION)
    assert filenames != payload.PAYLOAD_FILENAMES
    for name in filenames:
        (root / name).write_bytes(f"synthetic:{name}".encode("utf-8"))
    manifest = {
        "manifest_schema_version": 2,
        "product_name": "Music Vault",
        "version": TAGGED_VERSION,
        **_provenance(),
    }
    release_common.write_json(root / "release-manifest.json", manifest)

    payload.write_payload_index(
        root,
        expected_release_version=TAGGED_VERSION,
        expected_source_tag=SOURCE_TAG,
        expected_source_commit=SOURCE_COMMIT,
        expected_tooling_commit=TOOLING_COMMIT,
    )
    index = payload.verify_payload_index(
        root,
        expected_release_version=TAGGED_VERSION,
        expected_source_tag=SOURCE_TAG,
        expected_source_commit=SOURCE_COMMIT,
        expected_tooling_commit=TOOLING_COMMIT,
    )
    assert index["version"] == TAGGED_VERSION
    assert index["source_commit"] == SOURCE_COMMIT
    assert index["release_tooling_commit"] == TOOLING_COMMIT
    assert {row["name"] for row in index["files"]} == set(filenames)


def test_workflow_passes_tag_derived_version_to_current_release_tooling() -> None:
    workflow = (release_common.PROJECT_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert '--release-version "${{ steps.meta.outputs.release_version }}"' in workflow
    assert workflow.count("--expected-release-version") == 3
    assert workflow.count("verify_portable_release.py") >= 2
    assert workflow.count('--release-version "${{') >= 3
    assert "tests/test_batch9_release_version_compat.py" in workflow

    rehearsal = (
        release_common.PROJECT_ROOT / "tools/release/rehearse_tagged_release.ps1"
    ).read_text(encoding="utf-8")
    assert rehearsal.count('--release-version "1.0.0"') == 3
    assert rehearsal.count('--expected-release-version "1.0.0"') == 2

    readme = (release_common.PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    stable_example = readme.split(
        ".\\tools\\release\\verify_portable_release.ps1", 1
    )[1].split("```", 1)[0]
    assert "MusicVault-v1.0.0-Windows-x64-Portable.zip" in stable_example
    assert "--release-version 1.0.0" in stable_example
