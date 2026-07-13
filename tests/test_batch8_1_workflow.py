from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_vault.version import APP_VERSION
from tools.release import build_portable_release as builder
from tools.release import fetch_compliance_sources as sources
from tools.release import release_common
from tools.release import validate_release_payload as payload
from tools.release import verify_portable_release as verifier


SOURCE_TAG_OBJECT = "a" * 40
SOURCE_COMMIT = "b" * 40
SOURCE_TREE = "c" * 40
TOOLING_COMMIT = "d" * 40
TOOLING_TREE = "e" * 40
INVENTORY_BLOB = "1" * 40
INVENTORY_HASH = "f" * 64


def _provenance() -> dict[str, str]:
    return {
        "source_tag": f"v{APP_VERSION}",
        "source_tag_object": SOURCE_TAG_OBJECT,
        "source_commit": SOURCE_COMMIT,
        "source_tree_hash": SOURCE_TREE,
        "release_tooling_commit": TOOLING_COMMIT,
        "release_tooling_tree_hash": TOOLING_TREE,
        "release_license_inventory_git_blob": INVENTORY_BLOB,
        "release_license_inventory_sha256": INVENTORY_HASH,
    }


def _payload_directory(tmp_path: Path) -> Path:
    root = tmp_path / "payload"
    root.mkdir()
    for name in payload.PAYLOAD_FILENAMES:
        (root / name).write_bytes(f"synthetic:{name}".encode("utf-8"))
    manifest = {
        "manifest_schema_version": 2,
        "product_name": "Music Vault",
        "version": APP_VERSION,
        **_provenance(),
    }
    (root / "release-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


def test_release_workflow_uses_separate_tagged_app_and_current_tooling() -> None:
    workflow = (release_common.PROJECT_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert "workflow_dispatch:" in workflow
    assert "path: tooling" in workflow and "path: application" in workflow
    assert "Check out exact current-main release tooling" in workflow
    assert "Check out immutable tagged application" in workflow
    assert "refs/tags/${{ needs.build-and-verify.outputs.source_tag }}" in workflow
    assert workflow.count("persist-credentials: false") >= 4
    assert "validate_release_payload.py write" in workflow
    assert workflow.count("validate_release_payload.py verify") >= 2
    assert "test_batch8_1_*.py" in workflow
    assert "pre_public_history_check.py --repo tooling" in workflow
    assert "fetch_compliance_sources.py" in workflow
    assert "--cache-dir tooling/release_artifacts/.source-cache" in workflow
    assert "--source-cache tooling/release_artifacts/.source-cache" in workflow
    assert workflow.count("verify_portable_release.py") >= 2
    assert "MusicVault-v1.0.0-Windows-x64-Portable.zip" not in workflow
    assert "--verify-tag" in workflow
    assert "gh release view" in workflow
    assert "git -C tooling ls-remote --tags origin" in workflow
    assert "Publish checkout tag object changed." in workflow
    assert "Remote release tag commit changed." in workflow
    assert "git tag " not in workflow
    assert "git push " not in workflow
    assert "fc47cb2a3ad9e084d382739b9bc4d2e7cf771437" in workflow
    assert "af00394fa1e6c5c0f18c7db70d2aaf6a26e84a6b" in workflow
    publish = workflow.split("\n  publish:\n", 1)[1]
    assert "-r application/requirements-release.txt" not in publish
    assert "--only-binary=:all: pefile==2024.8.26" in publish

    rehearsal = (
        release_common.PROJECT_ROOT / "tools/release/rehearse_tagged_release.ps1"
    ).read_text(encoding="utf-8")
    assert "fc47cb2a3ad9e084d382739b9bc4d2e7cf771437" in rehearsal
    assert "af00394fa1e6c5c0f18c7db70d2aaf6a26e84a6b" in rehearsal
    assert "git tag " not in rehearsal and "git push " not in rehearsal
    add_at = rehearsal.index("worktree add --detach")
    remove_at = rehearsal.index("worktree remove --force")
    assert rehearsal.rfind("Resolve-ContainedPath", 0, add_at) >= 0
    assert rehearsal.rfind("Resolve-ContainedPath", 0, remove_at) >= 0
    assert "Tagged application worktree removal failed." in rehearsal


def test_dual_provenance_manifest_is_explicit(tmp_path: Path) -> None:
    portable = tmp_path / "portable"
    portable.mkdir()
    (portable / "MusicVault.exe").write_bytes(b"synthetic")
    manifest = builder.build_manifest(
        portable,
        **_provenance(),
        build_timestamp="2026-01-01T00:00:00Z",
        build_environment={
            "python": "3.11.9",
            "pyinstaller": "6.21.0",
            "dependencies": {},
        },
    )
    assert manifest["manifest_schema_version"] == 2
    assert {key: manifest[key] for key in _provenance()} == _provenance()


def test_tag_resolution_requires_annotated_exact_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "application"
    root.mkdir()

    def git(_root: Path, *args: str) -> str:
        value = args[-1]
        if args[:2] == ("cat-file", "-t"):
            return "tag"
        if value == f"refs/tags/v{APP_VERSION}":
            return SOURCE_TAG_OBJECT
        if value.endswith("^{commit}"):
            return SOURCE_COMMIT
        if value.endswith("^{tree}"):
            return SOURCE_TREE
        if value == "HEAD":
            return SOURCE_COMMIT
        return ""

    monkeypatch.setattr(builder, "_git", git)
    assert builder._resolve_tagged_source(
        root, f"v{APP_VERSION}", SOURCE_COMMIT, require_clean=True
    ) == (SOURCE_TAG_OBJECT, SOURCE_COMMIT, SOURCE_TREE)

    def lightweight(_root: Path, *args: str) -> str:
        return "commit" if args[:2] == ("cat-file", "-t") else git(_root, *args)

    monkeypatch.setattr(builder, "_git", lightweight)
    with pytest.raises(release_common.ReleaseError, match="annotated"):
        builder._resolve_tagged_source(
            root, f"v{APP_VERSION}", SOURCE_COMMIT, require_clean=True
        )


def test_tagged_documents_use_explicit_corrected_inventory(tmp_path: Path) -> None:
    application = tmp_path / "tagged-application"
    portable = tmp_path / "portable"
    portable.mkdir()
    for source_name in builder.ROOT_DOCUMENTS:
        source = application / source_name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"tagged:{source_name}", encoding="utf-8")
    licenses = application / "licenses"
    licenses.mkdir()
    (licenses / "LICENSE.txt").write_text("tagged license", encoding="utf-8")
    corrected_inventory = tmp_path / "corrected.json"
    corrected_inventory.write_text('{"corrected": true}', encoding="utf-8")

    builder.copy_release_documents(portable, application, corrected_inventory)

    assert (portable / "README_FIRST_RUN.md").read_text(encoding="utf-8").startswith(
        "tagged:"
    )
    assert json.loads(
        (portable / "licenses/third_party_licenses.json").read_text(encoding="utf-8")
    ) == {"corrected": True}


def test_transfer_index_round_trip_and_fail_closed_mutations(tmp_path: Path) -> None:
    root = _payload_directory(tmp_path)
    index = payload.write_payload_index(
        root,
        expected_source_tag=f"v{APP_VERSION}",
        expected_source_commit=SOURCE_COMMIT,
        expected_tooling_commit=TOOLING_COMMIT,
    )
    assert index.name == payload.INDEX_FILENAME
    verified = payload.verify_payload_index(
        root,
        expected_source_tag=f"v{APP_VERSION}",
        expected_source_commit=SOURCE_COMMIT,
        expected_tooling_commit=TOOLING_COMMIT,
    )
    assert verified["release_tooling_commit"] == TOOLING_COMMIT

    portable = root / release_common.PACKAGE_FILENAME
    portable.write_bytes(portable.read_bytes() + b"tampered")
    with pytest.raises(release_common.ReleaseError, match="integrity mismatch"):
        payload.verify_payload_index(root)


def test_transfer_index_rejects_extra_file_and_wrong_provenance(tmp_path: Path) -> None:
    root = _payload_directory(tmp_path)
    with pytest.raises(release_common.ReleaseError, match="tooling commit mismatch"):
        payload.write_payload_index(root, expected_tooling_commit="f" * 40)
    (root / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(release_common.ReleaseError, match="file set mismatch"):
        payload.write_payload_index(root)


def test_transfer_index_rejects_unexpected_directory(tmp_path: Path) -> None:
    root = _payload_directory(tmp_path)
    (root / "unverified-cache").mkdir()
    with pytest.raises(release_common.ReleaseError, match="non_files"):
        payload.write_payload_index(root)


def test_builder_inventory_is_the_filtered_blob_at_tooling_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = (
        release_common.PROJECT_ROOT
        / release_common.RELEASE_LICENSE_INVENTORY_PATH
    )
    calls: list[tuple[str, ...]] = []
    blob_id = release_common.git_blob_sha1_file(expected)

    def git(_root: Path, *args: str) -> str:
        calls.append(args)
        if args[:2] == ("cat-file", "-t"):
            return "blob"
        return blob_id

    monkeypatch.setattr(builder, "_git", git)
    selected, recorded_blob, recorded_sha = builder._resolve_release_inventory(
        expected, TOOLING_COMMIT
    )
    assert selected == expected.resolve()
    assert recorded_blob == blob_id
    assert recorded_sha == release_common.sha256_file(expected)
    assert any(
        args[0] == "hash-object"
        and f"--path={release_common.RELEASE_LICENSE_INVENTORY_PATH}" in args
        for args in calls
    )

    arbitrary = tmp_path / "inventory.json"
    arbitrary.write_text("{}", encoding="utf-8")
    with pytest.raises(release_common.ReleaseError, match="tracked tooling inventory"):
        builder._resolve_release_inventory(arbitrary, TOOLING_COMMIT)


def test_verifier_anchors_inventory_and_every_tagged_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = tmp_path / "third_party_licenses.json"
    inventory.write_text('{"inventory": true}\n', encoding="utf-8")
    inventory_blob = release_common.git_blob_sha1_file(inventory)
    manifest = {
        **_provenance(),
        "release_license_inventory_git_blob": inventory_blob,
        "release_license_inventory_sha256": release_common.sha256_file(inventory),
    }
    calls: list[tuple[str, ...]] = []

    def git(*args: str) -> str:
        calls.append(args)
        if args[0] == "hash-object":
            return release_common.git_blob_sha1_file(Path(args[-1]))
        if args[:2] == ("cat-file", "-t"):
            return "blob"
        return inventory_blob

    monkeypatch.setattr(verifier, "git_value", git)
    assert verifier.verify_release_inventory_anchor(
        manifest, inventory, "inventory.json"
    ) == []
    assert any(
        args[0] == "hash-object"
        and f"--path={release_common.RELEASE_LICENSE_INVENTORY_PATH}" in args
        for args in calls
    )
    inventory.write_text('{"inventory": false}\n', encoding="utf-8")
    rules = {
        finding.rule
        for finding in verifier.verify_release_inventory_anchor(
            manifest, inventory, "inventory.json"
        )
    }
    assert "corrected release license inventory hash mismatch" in rules
    assert "corrected release license inventory blob mismatch" in rules

    source = tmp_path / "source"
    source.mkdir()
    tracked = source / "tracked.txt"
    exact_bytes = b"exact tagged bytes\n"
    tracked.write_bytes(exact_bytes)
    tracked_blob = release_common.git_blob_sha1_file(tracked)

    def export_expected(_commit: str, destination: Path) -> Path:
        root = destination / "expected-tagged-source"
        root.mkdir(parents=True)
        (root / "tracked.txt").write_bytes(exact_bytes)
        return root

    monkeypatch.setattr(
        verifier,
        "git_tree_entries_at",
        lambda *_args: [("100644", "blob", tracked_blob, "tracked.txt")],
    )
    monkeypatch.setattr(verifier, "_export_expected_source_snapshot", export_expected)
    assert verifier.verify_tagged_source_snapshot(source, SOURCE_COMMIT) == []
    tracked.write_text("tampered\n", encoding="utf-8")
    findings = verifier.verify_tagged_source_snapshot(source, SOURCE_COMMIT)
    assert any("does not match the Git tree" in finding.rule for finding in findings)
    tracked.unlink()
    findings = verifier.verify_tagged_source_snapshot(source, SOURCE_COMMIT)
    assert any("missing or unsafe" in finding.rule for finding in findings)
    monkeypatch.setattr(
        verifier,
        "git_tree_entries_at",
        lambda *_args: [("160000", "commit", SOURCE_COMMIT, "vendor/module")],
    )
    findings = verifier.verify_tagged_source_snapshot(source, SOURCE_COMMIT)
    assert any("gitlink" in finding.rule for finding in findings)


def test_verifier_detects_offline_zlib_internal_semantic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compliance = tmp_path / "compliance"
    sources_root = compliance / "third-party-sources"
    sources_root.mkdir(parents=True)
    inventory = compliance / "release-tooling/third_party_licenses.json"
    inventory.parent.mkdir()
    inventory.write_text("{}", encoding="utf-8")

    def fail_semantics(cache: Path, offline: bool, inventory_path: Path):
        assert cache == sources_root
        assert offline is True
        assert inventory_path == inventory
        raise release_common.ReleaseError("synthetic zlib internal semantic mismatch")

    monkeypatch.setattr(sources, "fetch_sources", fail_semantics)
    rows, findings = verifier.verify_corresponding_source_semantics(compliance, inventory)
    assert rows == []
    assert len(findings) == 1
    assert "semantic validation failed" in findings[0].rule
