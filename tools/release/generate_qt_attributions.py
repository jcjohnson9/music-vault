from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

try:
    from .fetch_compliance_sources import fetch_sources
    from .release_common import PROJECT_ROOT, ReleaseError, sha256_file, write_json
except ImportError:  # Direct script execution.
    from fetch_compliance_sources import fetch_sources
    from release_common import PROJECT_ROOT, ReleaseError, sha256_file, write_json


QT_ARCHIVES = {
    "pyside": "pyside-setup-everywhere-src-6.11.1.tar.xz",
    "qtbase": "qtbase-everywhere-src-6.11.1.tar.xz",
    "qtmultimedia": "qtmultimedia-everywhere-src-6.11.1.tar.xz",
    "qtsvg": "qtsvg-everywhere-src-6.11.1.tar.xz",
    "qtimageformats": "qtimageformats-everywhere-src-6.11.1.tar.xz",
}


def _safe_member(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise ReleaseError("A Qt source archive contains an unsafe member name.")
    return value


def _resolve_member(parent: PurePosixPath, relative_name: str) -> PurePosixPath:
    parts = list(parent.parts)
    for part in PurePosixPath(relative_name).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if len(parts) <= 1:
                raise ReleaseError("A Qt attribution license path escapes its source archive.")
            parts.pop()
        else:
            parts.append(part)
    return _safe_member(PurePosixPath(*parts).as_posix())


def _write_member(archive: tarfile.TarFile, member: tarfile.TarInfo, destination: Path) -> None:
    if not member.isfile():
        raise ReleaseError("A required Qt notice is not a regular file.")
    source = archive.extractfile(member)
    if source is None:
        raise ReleaseError("A required Qt notice could not be read.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read())


def _referenced_notice_files(item: dict[str, object]) -> list[tuple[str, str]]:
    """Return every license/copyright file named by a Qt attribution record."""
    references: list[tuple[str, str]] = []
    for field in ("LicenseFile", "LicenseFiles", "CopyrightFile", "CopyrightFiles"):
        values = item.get(field)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                reference = (field, value)
                if reference not in references:
                    references.append(reference)
    return references


def generate(cache: Path, output: Path) -> dict[str, object]:
    cache = cache.expanduser().resolve()
    output = output.expanduser().resolve()
    expected_parent = (PROJECT_ROOT / "licenses").resolve()
    try:
        output.relative_to(expected_parent)
    except ValueError as exc:
        raise ReleaseError("Qt attribution output must stay under the tracked licenses directory.") from exc
    fetch_sources(cache, offline=True)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    index_rows: list[dict[str, str]] = []
    archive_rows: list[dict[str, object]] = []
    license_rows: list[dict[str, str]] = []
    referenced_notice_rows: list[dict[str, str]] = []
    for module, filename in QT_ARCHIVES.items():
        archive_path = cache / filename
        archive_rows.append({
            "module": module,
            "filename": filename,
            "sha256": sha256_file(archive_path),
            "size": archive_path.stat().st_size,
        })
        with tarfile.open(archive_path, "r:*") as archive:
            members = {member.name: member for member in archive.getmembers()}
            attribution_names = sorted(
                name for name in members if name.endswith("qt_attribution.json")
            )
            if not attribution_names:
                raise ReleaseError(f"Qt source archive has no attributions: {filename}")
            root_name = _safe_member(next(iter(members))).parts[0]
            for name, member in sorted(members.items()):
                relative = _safe_member(name)
                if len(relative.parts) > 1 and relative.parts[1] == "LICENSES" and member.isfile():
                    local = PurePosixPath(
                        module,
                        "l",
                        hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
                        + Path(name).suffix[:16],
                    )
                    _write_member(archive, member, output / local)
                    license_rows.append({
                        "module": module,
                        "source_path": str(relative),
                        "packaged_path": local.as_posix(),
                    })
            for name in attribution_names:
                if "/examples/" in name:
                    continue
                member = members[name]
                raw = archive.extractfile(member).read()  # type: ignore[union-attr]
                try:
                    # Qt's source notices occasionally contain literal newlines
                    # inside JSON strings. Python's non-strict mode preserves
                    # those upstream bytes while still validating the structure.
                    payload = json.loads(raw.decode("utf-8"), strict=False)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReleaseError(f"Invalid Qt attribution JSON in {filename}: {name}") from exc
                relative = _safe_member(name)
                local_relative = PurePosixPath(
                    module,
                    "a",
                    hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] + ".json",
                )
                destination = output / local_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                payloads = payload if isinstance(payload, list) else [payload]
                for item in payloads:
                    if not isinstance(item, dict):
                        raise ReleaseError(f"Invalid Qt attribution record in {filename}: {name}")
                    for field, notice_name in _referenced_notice_files(item):
                        source_name = _resolve_member(
                            PurePosixPath(name).parent, notice_name
                        ).as_posix()
                        source_member = members.get(source_name)
                        if source_member is None:
                            raise ReleaseError(
                                "Qt attribution references a missing notice file: "
                                f"{filename}:{source_name}"
                            )
                        suffix = Path(source_name).suffix[:16]
                        local_notice = PurePosixPath(
                            module,
                            "r",
                            hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:16]
                            + suffix,
                        )
                        _write_member(archive, source_member, output / local_notice)
                        notice_row = {
                            "module": module,
                            "field": field,
                            "source_path": source_name,
                            "packaged_path": local_notice.as_posix(),
                        }
                        if notice_row not in referenced_notice_rows:
                            referenced_notice_rows.append(notice_row)
                    index_rows.append({
                        "module": module,
                        "name": str(item.get("Name") or item.get("Id") or "unnamed"),
                        "version": str(item.get("Version") or "not stated"),
                        "license": str(item.get("LicenseId") or item.get("License") or "not stated"),
                        "attribution": local_relative.as_posix(),
                        "source_path": str(relative),
                    })

    write_json(
        output / "SOURCE_ARCHIVES.json",
        {
            "schema_version": 1,
            "archives": archive_rows,
            "module_license_files": license_rows,
            "referenced_notice_files": referenced_notice_rows,
        },
    )
    lines = [
        "# Qt 6.11.1 third-party attributions",
        "",
        "Generated from the hash-pinned official PySide/Qt source archives listed in",
        "`SOURCE_ARCHIVES.json`. The original Qt attribution JSON, referenced copyright",
        "and license files, and each module's full `LICENSES` directory are preserved",
        "below this directory. This is a notice superset for the Qt modules shipped by",
        "Music Vault; including an extra notice does not change a component's license.",
        "",
        "| Module | Component | Version | License | Exact attribution | Upstream path |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(index_rows, key=lambda value: (value["module"], value["name"].casefold())):
        values = [row[key].replace("|", "\\|").replace("\n", " ") for key in ("module", "name", "version", "license", "attribution", "source_path")]
        lines.append("| " + " | ".join(values) + " |")
    (output / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"archive_count": len(archive_rows), "attribution_count": len(index_rows)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate exact Qt attribution materials.")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "release_artifacts" / ".source-cache")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "licenses" / "qt-attrib")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = generate(args.cache_dir, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError, ReleaseError) as exc:
        print(f"Qt attribution generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
