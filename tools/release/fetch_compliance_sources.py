from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import posixpath
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

try:
    from .release_common import (
        PROJECT_ROOT,
        ReleaseError,
        is_reparse_or_link,
        load_license_inventory,
    )
except ImportError:  # Direct script execution.
    from release_common import PROJECT_ROOT, ReleaseError, is_reparse_or_link, load_license_inventory


DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 60
DEFAULT_MAX_REDIRECTS = 5
MAX_REDIRECTS = 8
MAX_DOWNLOAD_ATTEMPTS = 3
MAX_SOURCE_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 250_000
MAX_SEMANTIC_FILE_BYTES = 2 * 1024 * 1024
USER_AGENT = "MusicVault-release-source-fetch/1.0"

ARCHIVE_MAGIC = {
    "tar.gz": (b"\x1f\x8b",),
    "tar.xz": (b"\xfd7zXZ\x00",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}

DEFAULT_CONTENT_TYPES = {
    "tar.gz": {
        "application/gzip",
        "application/octet-stream",
        "application/x-gzip",
        "application/x-tar",
    },
    "tar.xz": {
        "application/octet-stream",
        "application/x-tar",
        "application/x-xz",
    },
    "zip": {
        "application/octet-stream",
        "application/x-zip-compressed",
        "application/zip",
    },
}

# GitHub's public release and tag endpoints use these fixed HTTPS delivery
# hosts. Other redirects remain confined to the explicitly declared hosts.
KNOWN_HTTPS_REDIRECT_HOSTS = {
    "api.github.com": {"release-assets.githubusercontent.com"},
    "github.com": {"codeload.github.com", "release-assets.githubusercontent.com"},
}

SAFE_REQUEST_HEADERS = {"accept", "x-github-api-version"}


def _sanitize_url(value: str) -> str:
    """Return an origin/path-only URL suitable for release logs."""

    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or "invalid-host"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))


def _validated_https_url(
    value: object,
    *,
    label: str,
    allow_query: bool = False,
) -> tuple[str, str]:
    url = str(value or "").strip()
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.query and not allow_query)
        or parsed.fragment
    ):
        raise ReleaseError(f"Invalid HTTPS {label}: {_sanitize_url(url)}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ReleaseError(f"Literal-IP {label} is not allowed: {_sanitize_url(url)}")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ReleaseError(f"Private or loopback {label} is not allowed: {_sanitize_url(url)}")
    return url, host


def _validate_public_dns(host: str) -> None:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ReleaseError(f"Source host could not be resolved: {host}") from exc
    addresses = {str(answer[4][0]).split("%", 1)[0] for answer in answers}
    if not addresses:
        raise ReleaseError(f"Source host resolved to no addresses: {host}")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ReleaseError(f"Source host returned an invalid address: {host}") from exc
        if not parsed.is_global:
            raise ReleaseError(f"Source host resolved to a non-public address: {host}")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_reparse_components(path: Path, boundary: Path) -> None:
    relative = path.relative_to(boundary)
    current = boundary
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and is_reparse_or_link(current):
            raise ReleaseError("Source cache may not contain symlink or reparse-point components.")


def _git_cache_is_ignored_and_untracked(relative: Path) -> bool:
    common = ["git", "-c", f"safe.directory={PROJECT_ROOT.as_posix()}"]
    ignored = subprocess.run(
        [*common, "check-ignore", "--quiet", "--no-index", "--", relative.as_posix()],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        timeout=15,
    )
    tracked = subprocess.run(
        [*common, "ls-files", "--", relative.as_posix()],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        timeout=15,
    )
    return ignored.returncode == 0 and not tracked.stdout.strip()


def _prepare_cache(cache: Path) -> Path:
    raw = Path(os.path.abspath(cache.expanduser()))
    project_root = Path(os.path.abspath(PROJECT_ROOT))
    temp_root = Path(os.path.abspath(Path(tempfile.gettempdir())))
    if _is_within(raw, project_root):
        relative = raw.relative_to(project_root)
        if not relative.parts or relative.parts[0].casefold() != "release_artifacts":
            raise ReleaseError(
                "A repository-local source cache must stay under ignored release_artifacts."
            )
        if not _git_cache_is_ignored_and_untracked(relative):
            raise ReleaseError("A repository-local source cache must be ignored and untracked.")
        boundary = project_root
    elif _is_within(raw, temp_root):
        boundary = temp_root
    else:
        raise ReleaseError(
            "A source cache outside the repository must stay under the operating-system temp directory."
        )
    _reject_reparse_components(raw, boundary)
    raw.mkdir(parents=True, exist_ok=True)
    _reject_reparse_components(raw, boundary)
    resolved = raw.resolve()
    if not _is_within(resolved, boundary.resolve()):
        raise ReleaseError("Source cache resolution escaped its approved boundary.")
    return resolved


def _validate_cache_entry(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if is_reparse_or_link(path):
            raise ReleaseError(f"Source cache entry may not be a link or reparse point: {path.name}")
        if not path.is_file():
            raise ReleaseError(f"Source cache entry is not a regular file: {path.name}")


def _remove_partial(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    _validate_cache_entry(path)
    path.unlink()


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ReleaseError("Source download exceeded its whole-attempt deadline.")


def _positive_int(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise ReleaseError(f"Invalid {label} in corresponding-source inventory.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseError(f"Invalid {label} in corresponding-source inventory.") from exc
    if result <= 0 or result > maximum:
        raise ReleaseError(f"Invalid {label} in corresponding-source inventory.")
    return result


def _archive_format(filename: str, declared: object = None) -> str:
    value = str(declared or "").strip().casefold()
    if not value:
        folded = filename.casefold()
        if folded.endswith((".tar.gz", ".tgz")):
            value = "tar.gz"
        elif folded.endswith((".tar.xz", ".txz")):
            value = "tar.xz"
        elif folded.endswith(".zip"):
            value = "zip"
    if value not in ARCHIVE_MAGIC:
        raise ReleaseError(f"Unsupported corresponding-source archive format: {filename}")
    return value


def _validated_relative_path(value: object, *, label: str) -> str:
    path = str(value or "").strip()
    if not path or "\\" in path or "\x00" in path:
        raise ReleaseError(f"Invalid {label} in corresponding-source inventory.")
    trimmed = path.rstrip("/")
    parts = trimmed.split("/")
    if (
        not trimmed
        or trimmed.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
        or PurePosixPath(trimmed).is_absolute()
    ):
        raise ReleaseError(f"Invalid {label} in corresponding-source inventory.")
    return PurePosixPath(trimmed).as_posix()


def _validated_checks(value: object, *, label: str) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReleaseError(f"Invalid {label} in corresponding-source inventory.")
    result: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ReleaseError(f"Invalid {label} in corresponding-source inventory.")
        path = _validated_relative_path(raw.get("path"), label=f"{label} path")
        contains_raw = raw.get("contains", [])
        if isinstance(contains_raw, str):
            contains_raw = [contains_raw]
        if not isinstance(contains_raw, list) or any(
            not isinstance(marker, str) or not marker for marker in contains_raw
        ):
            raise ReleaseError(f"Invalid {label} markers in corresponding-source inventory.")
        digest = str(raw.get("sha256") or "").strip().casefold()
        if digest and (
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ReleaseError(f"Invalid {label} hash in corresponding-source inventory.")
        if not contains_raw and not digest:
            raise ReleaseError(f"Empty {label} in corresponding-source inventory.")
        result.append({"path": path, "contains": list(contains_raw), "sha256": digest})
    return result


def _read_inventory(inventory_path: Path | None) -> dict[str, object]:
    if inventory_path is None:
        return load_license_inventory()
    source = inventory_path.expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReleaseError("The corresponding-source inventory is invalid.")
    return payload


def _source_rows(inventory_path: Path | None = None) -> list[dict[str, object]]:
    rows = _read_inventory(inventory_path).get("corresponding_source_archives")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("The corresponding-source archive inventory is empty.")

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ReleaseError("The corresponding-source archive inventory is invalid.")
        component = str(raw.get("component") or "").strip()
        filename = str(raw.get("filename") or "").strip()
        digest = str(raw.get("sha256") or "").strip().casefold()
        if (
            not component
            or not filename
            or Path(filename).name != filename
            or filename.casefold() in seen
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ReleaseError("The corresponding-source archive inventory is invalid.")

        primary_url, primary_host = _validated_https_url(raw.get("url"), label="source URL")
        fallback_raw = raw.get("fallback_urls", [])
        if not isinstance(fallback_raw, list):
            raise ReleaseError("Invalid source fallback URLs in corresponding-source inventory.")
        fallback_urls: list[str] = []
        source_hosts = {primary_host}
        for fallback in fallback_raw:
            url, host = _validated_https_url(fallback, label="source fallback URL")
            if url in [primary_url, *fallback_urls]:
                raise ReleaseError("Duplicate source URL in corresponding-source inventory.")
            fallback_urls.append(url)
            source_hosts.add(host)

        allowed_hosts_raw = raw.get("allowed_hosts")
        if allowed_hosts_raw is None:
            allowed_hosts = set(source_hosts)
            for host in source_hosts:
                allowed_hosts.update(KNOWN_HTTPS_REDIRECT_HOSTS.get(host, set()))
        else:
            if not isinstance(allowed_hosts_raw, list) or not allowed_hosts_raw:
                raise ReleaseError("Invalid allowed source hosts in corresponding-source inventory.")
            allowed_hosts = {
                str(host or "").strip().casefold().rstrip(".") for host in allowed_hosts_raw
            }
            if "" in allowed_hosts or not source_hosts.issubset(allowed_hosts):
                raise ReleaseError("Invalid allowed source hosts in corresponding-source inventory.")

        archive_format = _archive_format(filename, raw.get("archive_format"))
        content_types_raw = raw.get("content_types")
        if content_types_raw is None:
            content_types = set(DEFAULT_CONTENT_TYPES[archive_format])
        else:
            if not isinstance(content_types_raw, list) or not content_types_raw:
                raise ReleaseError("Invalid source content types in corresponding-source inventory.")
            content_types = {
                str(value or "").split(";", 1)[0].strip().casefold()
                for value in content_types_raw
            }
            if "" in content_types or not content_types.issubset(DEFAULT_CONTENT_TYPES[archive_format]):
                raise ReleaseError("Invalid source content types in corresponding-source inventory.")

        headers_raw = raw.get("request_headers", {})
        if not isinstance(headers_raw, dict):
            raise ReleaseError("Invalid source request headers in corresponding-source inventory.")
        request_headers: dict[str, str] = {}
        for name, value in headers_raw.items():
            header = str(name or "").strip()
            header_value = str(value or "").strip()
            if (
                header.casefold() not in SAFE_REQUEST_HEADERS
                or not header_value
                or "\r" in header_value
                or "\n" in header_value
            ):
                raise ReleaseError("Invalid source request headers in corresponding-source inventory.")
            request_headers[header] = header_value

        expected_size = raw.get("size_bytes")
        if expected_size is not None:
            expected_size = _positive_int(
                expected_size,
                label="source archive size",
                maximum=MAX_SOURCE_ARCHIVE_BYTES,
            )
        maximum_size = raw.get("maximum_size_bytes", expected_size or MAX_SOURCE_ARCHIVE_BYTES)
        maximum_size = _positive_int(
            maximum_size,
            label="maximum source archive size",
            maximum=MAX_SOURCE_ARCHIVE_BYTES,
        )
        if expected_size is not None and maximum_size < expected_size:
            raise ReleaseError("Maximum source archive size is below its exact expected size.")
        timeout_seconds = _positive_int(
            raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            label="source timeout",
            maximum=MAX_TIMEOUT_SECONDS,
        )
        max_redirects = _positive_int(
            raw.get("max_redirects", DEFAULT_MAX_REDIRECTS),
            label="source redirect limit",
            maximum=MAX_REDIRECTS,
        )
        primary_attempts = _positive_int(
            raw.get("primary_attempts", 1),
            label="primary source attempt count",
            maximum=MAX_DOWNLOAD_ATTEMPTS,
        )
        fallback_attempts = _positive_int(
            raw.get("fallback_attempts", 1),
            label="fallback source attempt count",
            maximum=MAX_DOWNLOAD_ATTEMPTS,
        )

        top_level_raw = raw.get("top_level")
        top_level = ""
        if top_level_raw is not None:
            top_level = _validated_relative_path(top_level_raw, label="source top level")
            if "/" in top_level:
                raise ReleaseError("Source archive top level must be one path component.")
        required_raw = raw.get("required_paths", [])
        if not isinstance(required_raw, list):
            raise ReleaseError("Invalid required source paths in corresponding-source inventory.")
        required_paths = [
            _validated_relative_path(path, label="required source path") for path in required_raw
        ]
        if len(set(required_paths)) != len(required_paths):
            raise ReleaseError("Duplicate required source path in corresponding-source inventory.")

        version_checks = _validated_checks(raw.get("version_checks"), label="source version check")
        license_checks = _validated_checks(raw.get("license_checks"), label="source license check")
        semantic_paths = {
            str(check["path"]) for check in [*version_checks, *license_checks]
        }
        if not semantic_paths.issubset(set(required_paths)):
            raise ReleaseError("Semantic source checks must also be required source paths.")

        seen.add(filename.casefold())
        result.append(
            {
                "component": component,
                "filename": filename,
                "urls": [primary_url, *fallback_urls],
                "sha256": digest,
                "size_bytes": expected_size,
                "maximum_size_bytes": maximum_size,
                "allowed_hosts": allowed_hosts,
                "content_types": content_types,
                "request_headers": request_headers,
                "archive_format": archive_format,
                "top_level": top_level,
                "required_paths": required_paths,
                "version_checks": version_checks,
                "license_checks": license_checks,
                "timeout_seconds": timeout_seconds,
                "max_redirects": max_redirects,
                "primary_attempts": primary_attempts,
                "fallback_attempts": fallback_attempts,
            }
        )
    return result


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str], maximum: int) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.maximum = maximum
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.count += 1
        if self.count > self.maximum:
            raise ReleaseError("Source download exceeded its redirect limit.")
        _, host = _validated_https_url(newurl, label="redirect URL", allow_query=True)
        if host not in self.allowed_hosts:
            raise ReleaseError(
                f"Source download redirected to an unapproved host: {_sanitize_url(newurl)}"
            )
        _validate_public_dns(host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_response(
    request: urllib.request.Request,
    *,
    timeout: int,
    allowed_hosts: set[str],
    max_redirects: int,
):
    # Release inputs must not be affected by HTTP(S)_PROXY environment state.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SafeRedirectHandler(allowed_hosts, max_redirects),
    )
    return opener.open(request, timeout=timeout)


def _safe_member_path(name: str) -> str:
    return _validated_relative_path(name.rstrip("/"), label="archive member path")


def _resolved_link_target(member_name: str, target: str, *, symbolic: bool) -> str:
    if not target or "\\" in target or "\x00" in target or target.startswith("/"):
        raise ReleaseError(f"Unsafe archive link target: {member_name}")
    base = posixpath.dirname(member_name) if symbolic else ""
    resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise ReleaseError(f"Unsafe archive link target: {member_name}")
    return _validated_relative_path(resolved, label="archive link target")


def _validate_member_set(names: set[str], row: dict[str, object]) -> None:
    top_level = str(row["top_level"])
    if top_level and any(PurePosixPath(name).parts[0] != top_level for name in names):
        raise ReleaseError(f"Source archive has the wrong top-level directory: {row['filename']}")
    missing = sorted(set(row["required_paths"]) - names)
    if missing:
        raise ReleaseError(
            f"Source archive lacks a required path: {row['filename']} ({missing[0]})"
        )


def _validate_semantics(
    row: dict[str, object],
    read_member: Callable[[str], bytes],
) -> None:
    for label, checks in (
        ("version", row["version_checks"]),
        ("license", row["license_checks"]),
    ):
        for check in checks:
            path = str(check["path"])
            content = read_member(path)
            digest = str(check["sha256"])
            if digest and hashlib.sha256(content).hexdigest() != digest:
                raise ReleaseError(
                    f"Source archive {label} file hash mismatch: {row['filename']} ({path})"
                )
            text = content.decode("utf-8", errors="replace")
            for marker in check["contains"]:
                if marker not in text:
                    raise ReleaseError(
                        f"Source archive {label} marker mismatch: {row['filename']} ({path})"
                    )


def _validate_tar_archive(path: Path, row: dict[str, object]) -> None:
    with tarfile.open(path, mode="r:*") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise ReleaseError(f"Source archive has an invalid member count: {row['filename']}")
        names: set[str] = set()
        links: list[tuple[str, str, bool]] = []
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            name = _safe_member_path(member.name)
            if name in names:
                raise ReleaseError(f"Source archive has a duplicate path: {row['filename']}")
            names.add(name)
            by_name[name] = member
            if member.issym() or member.islnk():
                links.append((name, member.linkname, member.issym()))
            elif not (member.isfile() or member.isdir()):
                raise ReleaseError(f"Source archive has an unsafe member type: {row['filename']}")
        for name, target, symbolic in links:
            resolved = _resolved_link_target(name, target, symbolic=symbolic)
            if resolved not in names:
                raise ReleaseError(f"Source archive link target is absent: {row['filename']}")
        _validate_member_set(names, row)

        def read_member(name: str) -> bytes:
            member = by_name[name]
            if not member.isfile() or member.size > MAX_SEMANTIC_FILE_BYTES:
                raise ReleaseError(f"Source semantic file is invalid: {row['filename']} ({name})")
            stream = archive.extractfile(member)
            if stream is None:
                raise ReleaseError(f"Source semantic file is unreadable: {row['filename']} ({name})")
            content = stream.read(MAX_SEMANTIC_FILE_BYTES + 1)
            if len(content) > MAX_SEMANTIC_FILE_BYTES:
                raise ReleaseError(f"Source semantic file is too large: {row['filename']} ({name})")
            return content

        _validate_semantics(row, read_member)


def _validate_zip_archive(path: Path, row: dict[str, object]) -> None:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ReleaseError(f"Source archive has an invalid member count: {row['filename']}")
        names: set[str] = set()
        by_name: dict[str, zipfile.ZipInfo] = {}
        links: list[tuple[str, str]] = []
        for info in infos:
            name = _safe_member_path(info.filename)
            if name in names:
                raise ReleaseError(f"Source archive has a duplicate path: {row['filename']}")
            if info.flag_bits & 0x1:
                raise ReleaseError(f"Source archive contains encrypted content: {row['filename']}")
            names.add(name)
            by_name[name] = info
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                if info.file_size > 4096:
                    raise ReleaseError(f"Source archive has an unsafe link: {row['filename']}")
                target = archive.read(info).decode("utf-8", errors="strict")
                links.append((name, target))
            elif file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
                raise ReleaseError(f"Source archive has an unsafe member type: {row['filename']}")
        for name, target in links:
            resolved = _resolved_link_target(name, target, symbolic=True)
            if resolved not in names:
                raise ReleaseError(f"Source archive link target is absent: {row['filename']}")
        _validate_member_set(names, row)

        def read_member(name: str) -> bytes:
            info = by_name[name]
            if info.is_dir() or info.file_size > MAX_SEMANTIC_FILE_BYTES:
                raise ReleaseError(f"Source semantic file is invalid: {row['filename']} ({name})")
            content = archive.read(info)
            if len(content) > MAX_SEMANTIC_FILE_BYTES:
                raise ReleaseError(f"Source semantic file is too large: {row['filename']} ({name})")
            return content

        _validate_semantics(row, read_member)


def _validate_archive(path: Path, row: dict[str, object]) -> None:
    with path.open("rb") as source:
        prefix = source.read(8)
    if not any(prefix.startswith(magic) for magic in ARCHIVE_MAGIC[str(row["archive_format"])]):
        raise ReleaseError(f"Source archive magic does not match its format: {row['filename']}")
    try:
        if row["archive_format"] == "zip":
            _validate_zip_archive(path, row)
        else:
            _validate_tar_archive(path, row)
    except (tarfile.TarError, zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise ReleaseError(f"Source archive is unreadable or unsafe: {row['filename']}") from exc


def _validated_result(
    path: Path,
    row: dict[str, object],
    *,
    provenance_kind: str,
    source_role: str,
    source_url: str | None,
) -> dict[str, object]:
    actual_size = path.stat().st_size
    expected_size = row["size_bytes"]
    maximum_size = int(row["maximum_size_bytes"])
    if actual_size > maximum_size:
        raise ReleaseError(
            f"maximum-size violation; maximum_bytes={maximum_size}; actual_bytes={actual_size}"
        )
    if expected_size is not None and actual_size != expected_size:
        raise ReleaseError(
            f"size mismatch; expected_bytes={expected_size}; actual_bytes={actual_size}"
        )
    actual_digest = _sha256_file(path)
    if actual_digest != row["sha256"]:
        raise ReleaseError(
            f"SHA-256 mismatch; expected_sha256={row['sha256']}; actual_sha256={actual_digest}; "
            f"actual_bytes={actual_size}"
        )
    _validate_archive(path, row)
    return {
        "component": row["component"],
        "filename": path.name,
        "sha256": row["sha256"],
        "size": actual_size,
        "authoritative_primary": {
            "url": _sanitize_url(str(row["urls"][0])),
            "sha256": row["sha256"],
            "size_bytes": expected_size,
            "archive_format": row["archive_format"],
            "top_level": row["top_level"] or None,
        },
        "validation": {
            "profile_version": 1,
            "cryptographic": {
                "algorithm": "SHA-256",
                "status": "verified",
                "digest": row["sha256"],
            },
            "size": {
                "status": "verified",
                "expected_bytes": expected_size,
                "maximum_bytes": maximum_size,
                "actual_bytes": actual_size,
            },
            "archive": {
                "status": "verified",
                "format": row["archive_format"],
                "magic": "verified",
                "member_safety": "verified",
                "top_level": "verified" if row["top_level"] else "not_declared",
                "required_paths": "verified" if row["required_paths"] else "not_declared",
            },
            "semantics": {
                "version": "verified" if row["version_checks"] else "not_declared",
                "license": "verified" if row["license_checks"] else "not_declared",
            },
        },
        "provenance": {
            "kind": provenance_kind,
            "source_role": source_role,
            "source_url": _sanitize_url(source_url) if source_url else None,
        },
    }


def _validate_cached_source(path: Path, row: dict[str, object]) -> dict[str, object]:
    return _validated_result(
        path,
        row,
        provenance_kind="cache",
        source_role="prevalidated_cache",
        source_url=None,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _response_content_type(response) -> str:  # noqa: ANN001
    value = str(response.headers.get("Content-Type") or "")
    return value.split(";", 1)[0].strip().casefold()


def _response_content_length(response) -> int | None:  # noqa: ANN001
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ReleaseError("Source response has an invalid Content-Length.") from exc
    if value < 0:
        raise ReleaseError("Source response has an invalid Content-Length.")
    return value


def _download_attempt(
    url: str,
    target: Path,
    row: dict[str, object],
) -> None:
    _, requested_host = _validated_https_url(url, label="source URL")
    if requested_host not in row["allowed_hosts"]:
        raise ReleaseError(f"Source URL host is not approved: {_sanitize_url(url)}")
    _validate_public_dns(requested_host)

    headers = {"User-Agent": USER_AGENT, **row["request_headers"]}
    request = urllib.request.Request(url, headers=headers)
    digest = hashlib.sha256()
    actual_size = 0
    final_url = url
    status: int | None = None
    content_type = ""
    declared_size: int | None = None
    expected_size = row["size_bytes"]
    maximum_size = int(row["maximum_size_bytes"])
    expected_digest = str(row["sha256"])
    deadline = time.monotonic() + int(row["timeout_seconds"])
    try:
        with _open_response(
            request,
            timeout=int(row["timeout_seconds"]),
            allowed_hosts=set(row["allowed_hosts"]),
            max_redirects=int(row["max_redirects"]),
        ) as response, target.open("xb") as output:
            _check_deadline(deadline)
            final_url = str(response.geturl() or url)
            _, final_host = _validated_https_url(
                final_url,
                label="final source URL",
                allow_query=True,
            )
            if final_host not in row["allowed_hosts"]:
                raise ReleaseError(
                    f"Source download ended at an unapproved host: {_sanitize_url(final_url)}"
                )
            _validate_public_dns(final_host)
            status_value = getattr(response, "status", None)
            status = int(status_value) if status_value is not None else None
            if status is not None and not 200 <= status < 300:
                raise ReleaseError(f"Source response has an invalid HTTP status: {status}")
            content_type = _response_content_type(response)
            allowed_content_types = row["content_types"]
            if content_type not in allowed_content_types:
                raise ReleaseError("Source response has an unexpected Content-Type.")
            declared_size = _response_content_length(response)
            if declared_size is not None and declared_size > maximum_size:
                raise ReleaseError("Source response exceeds the maximum allowed byte size.")
            if expected_size is not None and declared_size is not None and declared_size != expected_size:
                raise ReleaseError("Source response has an unexpected Content-Length.")
            while True:
                _check_deadline(deadline)
                block = response.read(1024 * 1024)
                _check_deadline(deadline)
                if not block:
                    break
                actual_size += len(block)
                if actual_size > maximum_size:
                    raise ReleaseError("Source response exceeded the maximum allowed byte size.")
                digest.update(block)
                output.write(block)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    actual_digest = digest.hexdigest()
    reason = ""
    if declared_size is not None and actual_size != declared_size:
        reason = "Content-Length mismatch"
    elif expected_size is not None and actual_size != expected_size:
        reason = "byte-size mismatch"
    elif actual_digest != expected_digest:
        reason = "SHA-256 mismatch"
    if reason:
        target.unlink(missing_ok=True)
        raise ReleaseError(
            f"{reason}; requested={_sanitize_url(url)}; final={_sanitize_url(final_url)}; "
            f"status={status}; content_type={content_type or 'missing'}; "
            f"declared_bytes={declared_size}; expected_bytes={expected_size}; "
            f"actual_bytes={actual_size}; expected_sha256={expected_digest}; "
            f"actual_sha256={actual_digest}"
        )
    try:
        _validate_archive(target, row)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _safe_attempt_error(exc: BaseException) -> str:
    if isinstance(exc, ReleaseError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP error status={exc.code}"
    if isinstance(exc, (TimeoutError, urllib.error.URLError, OSError)):
        return f"transport error={type(exc).__name__}"
    return f"download error={type(exc).__name__}"


def fetch_sources(
    cache: Path,
    offline: bool = False,
    inventory_path: Path | None = None,
) -> list[dict[str, object]]:
    cache = _prepare_cache(cache)
    results: list[dict[str, object]] = []
    for row in _source_rows(inventory_path):
        target = cache / str(row["filename"])
        _validate_cache_entry(target)
        if target.is_file():
            try:
                results.append(_validate_cached_source(target, row))
            except (OSError, ReleaseError) as exc:
                raise ReleaseError(
                    f"Cached source archive validation failed: {target.name}; {_safe_attempt_error(exc)}"
                ) from exc
            continue
        if offline:
            raise ReleaseError(f"Required source archive is not cached: {target.name}")

        failures: list[str] = []
        fetched = False
        selected_url: str | None = None
        selected_role = ""
        for url_index, url in enumerate(row["urls"]):
            attempts = int(row["primary_attempts"] if url_index == 0 else row["fallback_attempts"])
            for attempt in range(1, attempts + 1):
                temporary = cache / (
                    f".{target.name}.partial-{os.getpid()}-{url_index + 1}-{attempt}"
                )
                _remove_partial(temporary)
                try:
                    _download_attempt(str(url), temporary, row)
                    _validate_cache_entry(target)
                    if target.exists():
                        raise ReleaseError(f"Source cache target appeared during download: {target.name}")
                    os.replace(temporary, target)
                    fetched = True
                    selected_url = str(url)
                    selected_role = "primary" if url_index == 0 else "fallback"
                    break
                except Exception as exc:
                    _remove_partial(temporary)
                    failures.append(
                        f"source={_sanitize_url(str(url))}; attempt={attempt}/{attempts}; "
                        f"{_safe_attempt_error(exc)}"
                    )
            if fetched:
                break
        if not fetched:
            raise ReleaseError(
                f"Could not fetch required source archive: {target.name}; " + " | ".join(failures)
            )
        try:
            results.append(
                _validated_result(
                    target,
                    row,
                    provenance_kind="network",
                    source_role=selected_role,
                    source_url=selected_url,
                )
            )
        except (OSError, ReleaseError) as exc:
            target.unlink(missing_ok=True)
            raise ReleaseError(
                f"Downloaded source archive validation failed: {target.name}; "
                f"{_safe_attempt_error(exc)}"
            ) from exc
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch exact hash-pinned release source archives.")
    parser.add_argument(
        "--cache-dir", type=Path, default=PROJECT_ROOT / "release_artifacts" / ".source-cache"
    )
    parser.add_argument("--offline", action="store_true", help="Validate the cache without network access.")
    parser.add_argument("--inventory-path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = fetch_sources(
            args.cache_dir,
            offline=args.offline,
            inventory_path=args.inventory_path,
        )
    except (OSError, ValueError, json.JSONDecodeError, ReleaseError) as exc:
        print(f"Compliance-source preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
