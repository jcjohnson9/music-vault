from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .release_common import PROJECT_ROOT, ReleaseError, load_license_inventory, sha256_file
except ImportError:  # Direct script execution.
    from release_common import PROJECT_ROOT, ReleaseError, load_license_inventory, sha256_file


def _source_rows() -> list[dict[str, str]]:
    rows = load_license_inventory().get("corresponding_source_archives")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("The corresponding-source archive inventory is empty.")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        row = {key: str(raw.get(key) or "").strip() for key in ("component", "filename", "url", "sha256")}
        filename = row["filename"]
        if (
            not all(row.values())
            or Path(filename).name != filename
            or len(row["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in row["sha256"].casefold())
            or filename.casefold() in seen
        ):
            raise ReleaseError("The corresponding-source archive inventory is invalid.")
        seen.add(filename.casefold())
        result.append(row)
    return result


def fetch_sources(cache: Path, *, offline: bool = False) -> list[dict[str, object]]:
    cache = cache.expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for row in _source_rows():
        target = cache / row["filename"]
        expected = row["sha256"].casefold()
        if target.is_file() and sha256_file(target) != expected:
            raise ReleaseError(f"Cached source archive hash mismatch: {target.name}")
        if not target.is_file():
            if offline:
                raise ReleaseError(f"Required source archive is not cached: {target.name}")
            temporary = cache / f".{target.name}.partial-{os.getpid()}"
            request = urllib.request.Request(
                row["url"], headers={"User-Agent": "MusicVault-release-source-fetch/1.0"}
            )
            digest = hashlib.sha256()
            try:
                with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as output:
                    while block := response.read(1024 * 1024):
                        digest.update(block)
                        output.write(block)
            except (OSError, urllib.error.URLError) as exc:
                temporary.unlink(missing_ok=True)
                raise ReleaseError(f"Could not fetch required source archive: {target.name}") from exc
            if digest.hexdigest() != expected:
                temporary.unlink(missing_ok=True)
                raise ReleaseError(f"Downloaded source archive hash mismatch: {target.name}")
            os.replace(temporary, target)
        results.append({
            "component": row["component"],
            "filename": target.name,
            "sha256": expected,
            "size": target.stat().st_size,
        })
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch exact hash-pinned release source archives.")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "release_artifacts" / ".source-cache")
    parser.add_argument("--offline", action="store_true", help="Validate the cache without network access.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = fetch_sources(args.cache_dir, offline=args.offline)
    except (OSError, ValueError, json.JSONDecodeError, ReleaseError) as exc:
        print(f"Compliance-source preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
