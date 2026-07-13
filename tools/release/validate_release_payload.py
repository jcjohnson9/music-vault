from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from .release_common import (
        APP_VERSION,
        COMPLIANCE_FILENAME,
        PACKAGE_FILENAME,
        PRODUCT_NAME,
        ReleaseError,
        sha256_file,
        write_json,
    )
except ImportError:  # Direct script execution.
    from release_common import (
        APP_VERSION,
        COMPLIANCE_FILENAME,
        PACKAGE_FILENAME,
        PRODUCT_NAME,
        ReleaseError,
        sha256_file,
        write_json,
    )


INDEX_FILENAME = "release-payload-index.json"
PAYLOAD_FILENAMES = (
    PACKAGE_FILENAME,
    f"{PACKAGE_FILENAME}.sha256",
    COMPLIANCE_FILENAME,
    f"{COMPLIANCE_FILENAME}.sha256",
    "release-manifest.json",
)
PROVENANCE_KEYS = (
    "source_tag",
    "source_tag_object",
    "source_commit",
    "source_tree_hash",
    "release_tooling_commit",
    "release_tooling_tree_hash",
    "release_license_inventory_git_blob",
)
ATTESTATION_KEYS = PROVENANCE_KEYS + ("release_license_inventory_sha256",)


def _load_manifest(directory: Path) -> dict[str, object]:
    try:
        value = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("Release payload manifest is unavailable or invalid.") from exc
    if not isinstance(value, dict):
        raise ReleaseError("Release payload manifest is invalid.")
    return value


def _validate_provenance(
    value: dict[str, object],
    *,
    expected_source_tag: str | None = None,
    expected_source_commit: str | None = None,
    expected_tooling_commit: str | None = None,
) -> None:
    if value.get("source_tag") != (expected_source_tag or f"v{APP_VERSION}"):
        raise ReleaseError("Release payload source tag mismatch.")
    for key in PROVENANCE_KEYS[1:]:
        if not re.fullmatch(r"[0-9a-f]{40}", str(value.get(key) or "")):
            raise ReleaseError(f"Release payload {key} is invalid.")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(value.get("release_license_inventory_sha256") or "")
    ):
        raise ReleaseError("Release payload license inventory hash is invalid.")
    if expected_source_commit and value.get("source_commit") != expected_source_commit:
        raise ReleaseError("Release payload source commit mismatch.")
    if expected_tooling_commit and value.get("release_tooling_commit") != expected_tooling_commit:
        raise ReleaseError("Release payload tooling commit mismatch.")


def _require_exact_files(directory: Path, *, include_index: bool) -> None:
    expected = set(PAYLOAD_FILENAMES)
    if include_index:
        expected.add(INDEX_FILENAME)
    entries = list(directory.iterdir())
    actual = {path.name for path in entries}
    non_files = sorted(
        (path.name for path in entries if not path.is_file()), key=str.casefold
    )
    if actual != expected or non_files:
        missing = sorted(expected - actual, key=str.casefold)
        extra = sorted(actual - expected, key=str.casefold)
        raise ReleaseError(
            "Release payload file set mismatch; "
            f"missing={missing}, extra={extra}, non_files={non_files}."
        )


def write_payload_index(
    directory: Path,
    *,
    expected_source_tag: str | None = None,
    expected_source_commit: str | None = None,
    expected_tooling_commit: str | None = None,
) -> Path:
    directory = directory.expanduser().resolve()
    _require_exact_files(directory, include_index=False)
    manifest = _load_manifest(directory)
    _validate_provenance(
        manifest,
        expected_source_tag=expected_source_tag,
        expected_source_commit=expected_source_commit,
        expected_tooling_commit=expected_tooling_commit,
    )
    files = [
        {
            "name": name,
            "size": (directory / name).stat().st_size,
            "sha256": sha256_file(directory / name),
        }
        for name in PAYLOAD_FILENAMES
    ]
    value = {
        "schema_version": 1,
        "product": PRODUCT_NAME,
        "version": APP_VERSION,
        **{key: manifest[key] for key in ATTESTATION_KEYS},
        "files": files,
    }
    path = directory / INDEX_FILENAME
    write_json(path, value)
    return path


def verify_payload_index(
    directory: Path,
    *,
    expected_source_tag: str | None = None,
    expected_source_commit: str | None = None,
    expected_tooling_commit: str | None = None,
) -> dict[str, object]:
    directory = directory.expanduser().resolve()
    _require_exact_files(directory, include_index=True)
    try:
        index = json.loads((directory / INDEX_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("Release payload index is unavailable or invalid.") from exc
    if not isinstance(index, dict) or index.get("schema_version") != 1:
        raise ReleaseError("Release payload index schema is invalid.")
    if index.get("product") != PRODUCT_NAME or index.get("version") != APP_VERSION:
        raise ReleaseError("Release payload index identity mismatch.")
    _validate_provenance(
        index,
        expected_source_tag=expected_source_tag,
        expected_source_commit=expected_source_commit,
        expected_tooling_commit=expected_tooling_commit,
    )
    manifest = _load_manifest(directory)
    for key in ATTESTATION_KEYS:
        if index.get(key) != manifest.get(key):
            raise ReleaseError(f"Release payload {key} disagrees with its manifest.")
    records = index.get("files")
    if not isinstance(records, list) or len(records) != len(PAYLOAD_FILENAMES):
        raise ReleaseError("Release payload index inventory is invalid.")
    by_name = {str(row.get("name") or ""): row for row in records if isinstance(row, dict)}
    if set(by_name) != set(PAYLOAD_FILENAMES):
        raise ReleaseError("Release payload index file names are invalid.")
    for name in PAYLOAD_FILENAMES:
        path = directory / name
        row = by_name[name]
        if row.get("size") != path.stat().st_size or row.get("sha256") != sha256_file(path):
            raise ReleaseError(f"Release payload transfer integrity mismatch: {name}")
    return index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write or verify an exact release transfer index.")
    parser.add_argument("mode", choices=("write", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-source-tag")
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--expected-tooling-commit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    kwargs = {
        "expected_source_tag": args.expected_source_tag,
        "expected_source_commit": args.expected_source_commit,
        "expected_tooling_commit": args.expected_tooling_commit,
    }
    try:
        if args.mode == "write":
            result: object = str(write_payload_index(args.directory, **kwargs))
        else:
            result = verify_payload_index(args.directory, **kwargs)
    except (OSError, ReleaseError, ValueError) as exc:
        print(f"Release payload validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
