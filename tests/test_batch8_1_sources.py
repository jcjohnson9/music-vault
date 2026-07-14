from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.error
from pathlib import Path

import pytest

from tools.release import fetch_compliance_sources as sources
from tools.release.release_common import PROJECT_ROOT, ReleaseError


ZLIB_HASH = "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"
PYSIDE_FILENAME = "pyside-setup-everywhere-src-6.11.1.tar.xz"
PYSIDE_ROOT = "pyside-setup-everywhere-src-6.11.1"
PYSIDE_PRIMARY = (
    "https://download.qt.io/official_releases/QtForPython/pyside6/"
    "PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz"
)
PYSIDE_MIRROR = (
    "https://qt.mirror.constant.com/official_releases/QtForPython/pyside6/"
    "PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz"
)
PYSIDE_HASH = "6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2"
PYSIDE_SIZE = 17_963_432
PYSIDE_TEST_LICENSE = (
    b"GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
)
QT_SUBMODULE_ORIGIN = (
    "https://download.qt.io/official_releases/qt/6.11/6.11.1/submodules/"
)
QT_SUBMODULE_MIRROR = (
    "https://qt.mirror.constant.com/archive/qt/6.11/6.11.1/submodules/"
)
QT_TEST_LICENSES = {
    "GPL-2.0-only.txt": b"GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991\n",
    "GPL-3.0-only.txt": b"GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n",
    "LGPL-3.0-only.txt": (
        b"GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
    ),
}
QT_SUBMODULE_CASES = (
    {
        "filename": "qtbase-everywhere-src-6.11.1.tar.xz",
        "project": "QtBase",
        "sha256": "d9594a31228aa23ad6b531719a29b45f0f3989fe6c136d45767ea179f233c1ac",
        "size_bytes": 50_648_500,
    },
    {
        "filename": "qtmultimedia-everywhere-src-6.11.1.tar.xz",
        "project": "QtMultimedia",
        "sha256": "390f8e52ddee3aca5c4de7eead900c84c4fa61ff6d1f0ebea9c7543365c09b0a",
        "size_bytes": 10_243_896,
    },
    {
        "filename": "qtsvg-everywhere-src-6.11.1.tar.xz",
        "project": "QtSvg",
        "sha256": "7f3cf02f4824bf03c2c5859ea6db173bf1482a1daf24e6cdf7bc78cfa26a8a94",
        "size_bytes": 2_336_944,
    },
    {
        "filename": "qtimageformats-everywhere-src-6.11.1.tar.xz",
        "project": "QtImageFormats",
        "sha256": "b2bf6c6845ac175ed7f819145483ba4676f617aaa6a5012c8efee63c8bbac413",
        "size_bytes": 2_032_792,
    },
)
REAL_DNS_VALIDATOR = sources._validate_public_dns


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "_validate_public_dns", lambda _host: None)


def isolate_temp_roots(
    monkeypatch: pytest.MonkeyPatch,
    standard_root: Path,
) -> None:
    standard_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sources.tempfile, "gettempdir", lambda: str(standard_root))
    for name in ("TEMP", "TMP", "TMPDIR", "RUNNER_TEMP", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


def patch_canonical_alias(
    monkeypatch: pytest.MonkeyPatch,
    alias_root: Path,
    canonical_root: Path,
) -> None:
    original_realpath = sources.os.path.realpath
    alias_key = sources._comparison_path(alias_root)

    def alias_aware_realpath(value: str | bytes | os.PathLike[str]) -> str:
        absolute = sources._comparison_path(
            sources._absolute_release_path(os.fspath(value))
        )
        if absolute == alias_key or sources._path_is_within(
            Path(absolute), Path(alias_key)
        ):
            relative = os.path.relpath(absolute, alias_key)
            mapped = canonical_root if relative == os.curdir else canonical_root / relative
            return original_realpath(os.fspath(mapped))
        return original_realpath(value)

    monkeypatch.setattr(sources.os.path, "realpath", alias_aware_realpath)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://primary.example/source.tar.gz",
        content_type: str = "application/x-gzip",
        declared_size: int | None = None,
        status: int = 200,
    ) -> None:
        self._stream = io.BytesIO(body)
        self._url = url
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body) if declared_size is None else declared_size),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self._stream.close()

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def archive_bytes(
    *,
    top_level: str = "source-1.0",
    version: str = "1.0",
    license_text: str = "Permission granted\nRequired notice\n",
    extra_members: list[tuple[str, bytes]] | None = None,
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        files = {
            f"{top_level}/VERSION": f"VERSION={version}\n".encode(),
            f"{top_level}/LICENSE": license_text.encode(),
            **dict(extra_members or []),
        }
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def pyside_archive_bytes(
    *,
    pyside_version: tuple[str, str, str] = ("6", "11", "1"),
    shiboken_version: tuple[str, str, str] = ("6", "11", "1"),
    license_text: bytes = PYSIDE_TEST_LICENSE,
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as archive:
        files = {
            f"{PYSIDE_ROOT}/setup.py": b"# synthetic setup\n",
            f"{PYSIDE_ROOT}/sources/pyside6/CMakeLists.txt": b"# synthetic PySide\n",
            f"{PYSIDE_ROOT}/sources/pyside6/.cmake.conf": (
                f'set(pyside_MAJOR_VERSION "{pyside_version[0]}")\n'
                f'set(pyside_MINOR_VERSION "{pyside_version[1]}")\n'
                f'set(pyside_MICRO_VERSION "{pyside_version[2]}")\n'
            ).encode(),
            f"{PYSIDE_ROOT}/sources/shiboken6/CMakeLists.txt": b"# synthetic Shiboken\n",
            f"{PYSIDE_ROOT}/sources/shiboken6/.cmake.conf": (
                f'set(shiboken_MAJOR_VERSION "{shiboken_version[0]}")\n'
                f'set(shiboken_MINOR_VERSION "{shiboken_version[1]}")\n'
                f'set(shiboken_MICRO_VERSION "{shiboken_version[2]}")\n'
            ).encode(),
            f"{PYSIDE_ROOT}/LICENSES/LGPL-3.0-only.txt": license_text,
        }
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def write_pyside_inventory(path: Path, body: bytes, **overrides) -> Path:
    pyside_version = f"{PYSIDE_ROOT}/sources/pyside6/.cmake.conf"
    shiboken_version = f"{PYSIDE_ROOT}/sources/shiboken6/.cmake.conf"
    license_path = f"{PYSIDE_ROOT}/LICENSES/LGPL-3.0-only.txt"
    row = {
        "component": "Synthetic PySide6 source",
        "filename": PYSIDE_FILENAME,
        "url": PYSIDE_PRIMARY,
        "fallback_urls": [PYSIDE_MIRROR],
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
        "maximum_size_bytes": sources.MAX_SOURCE_ARCHIVE_BYTES,
        "archive_format": "tar.xz",
        "allowed_hosts": ["download.qt.io", "qt.mirror.constant.com"],
        "content_types": ["application/x-xz", "application/octet-stream"],
        "timeout_seconds": 7,
        "max_redirects": 3,
        "primary_attempts": 1,
        "fallback_attempts": 1,
        "top_level": PYSIDE_ROOT,
        "required_paths": [
            f"{PYSIDE_ROOT}/setup.py",
            f"{PYSIDE_ROOT}/sources/pyside6/CMakeLists.txt",
            pyside_version,
            f"{PYSIDE_ROOT}/sources/shiboken6/CMakeLists.txt",
            shiboken_version,
            license_path,
        ],
        "version_checks": [
            {
                "path": pyside_version,
                "contains": [
                    'set(pyside_MAJOR_VERSION "6")',
                    'set(pyside_MINOR_VERSION "11")',
                    'set(pyside_MICRO_VERSION "1")',
                ],
            },
            {
                "path": shiboken_version,
                "contains": [
                    'set(shiboken_MAJOR_VERSION "6")',
                    'set(shiboken_MINOR_VERSION "11")',
                    'set(shiboken_MICRO_VERSION "1")',
                ],
            },
        ],
        "license_checks": [
            {
                "path": license_path,
                "sha256": hashlib.sha256(PYSIDE_TEST_LICENSE).hexdigest(),
                "contains": [
                    "GNU LESSER GENERAL PUBLIC LICENSE",
                    "Version 3, 29 June 2007",
                ],
            }
        ],
    }
    row.update(overrides)
    path.write_text(
        json.dumps({"corresponding_source_archives": [row]}),
        encoding="utf-8",
    )
    return path


def qt_submodule_archive_bytes(
    case: dict[str, object],
    *,
    version: str = "6.11.1",
    omit_license: str | None = None,
    unsafe_member: str | None = None,
) -> bytes:
    filename = str(case["filename"])
    root = filename.removesuffix(".tar.xz")
    project = str(case["project"])
    files = {
        f"{root}/.cmake.conf": f'set(QT_REPO_MODULE_VERSION "{version}")\n'.encode(),
        f"{root}/CMakeLists.txt": (
            f"project({project}\n"
            '    VERSION "${QT_REPO_MODULE_VERSION}"\n'
            ")\n"
        ).encode(),
        **{
            f"{root}/LICENSES/{name}": content
            for name, content in QT_TEST_LICENSES.items()
            if name != omit_license
        },
    }
    if unsafe_member:
        files[unsafe_member] = b"unsafe\n"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def write_qt_submodule_inventory(
    path: Path,
    case: dict[str, object],
    body: bytes,
    **overrides,
) -> Path:
    filename = str(case["filename"])
    root = filename.removesuffix(".tar.xz")
    project = str(case["project"])
    license_paths = {
        name: f"{root}/LICENSES/{name}" for name in QT_TEST_LICENSES
    }
    row = {
        "component": f"Synthetic {project} source",
        "filename": filename,
        "url": QT_SUBMODULE_ORIGIN + filename,
        "fallback_urls": [QT_SUBMODULE_MIRROR + filename],
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
        "maximum_size_bytes": sources.MAX_SOURCE_ARCHIVE_BYTES,
        "archive_format": "tar.xz",
        "allowed_hosts": ["download.qt.io", "qt.mirror.constant.com"],
        "content_types": ["application/x-xz", "application/octet-stream"],
        "timeout_seconds": 7,
        "max_redirects": 3,
        "primary_attempts": 1,
        "fallback_attempts": 1,
        "top_level": root,
        "required_paths": [
            f"{root}/.cmake.conf",
            f"{root}/CMakeLists.txt",
            *license_paths.values(),
        ],
        "version_checks": [
            {
                "path": f"{root}/.cmake.conf",
                "contains": ['set(QT_REPO_MODULE_VERSION "6.11.1")'],
            },
            {
                "path": f"{root}/CMakeLists.txt",
                "contains": [
                    f"project({project}",
                    'VERSION "${QT_REPO_MODULE_VERSION}"',
                ],
            },
        ],
        "license_checks": [
            {
                "path": license_paths[name],
                "sha256": hashlib.sha256(content).hexdigest(),
                "contains": [
                    "GNU LESSER GENERAL PUBLIC LICENSE"
                    if name.startswith("LGPL")
                    else "GNU GENERAL PUBLIC LICENSE",
                    "Version 2, June 1991"
                    if name.startswith("GPL-2")
                    else "Version 3, 29 June 2007",
                ],
            }
            for name, content in QT_TEST_LICENSES.items()
        ],
    }
    row.update(overrides)
    path.write_text(
        json.dumps({"corresponding_source_archives": [row]}),
        encoding="utf-8",
    )
    return path


def write_inventory(
    path: Path,
    body: bytes,
    **overrides,
) -> Path:
    row = {
        "component": "Synthetic source",
        "filename": "source-1.0.tar.gz",
        "url": "https://primary.example/source.tar.gz",
        "fallback_urls": ["https://fallback.example/source.tar.gz"],
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
        "archive_format": "tar.gz",
        "allowed_hosts": ["primary.example", "fallback.example"],
        "content_types": ["application/x-gzip", "application/octet-stream"],
        "timeout_seconds": 7,
        "max_redirects": 3,
        "primary_attempts": 2,
        "fallback_attempts": 1,
        "top_level": "source-1.0",
        "required_paths": ["source-1.0/VERSION", "source-1.0/LICENSE"],
        "version_checks": [
            {"path": "source-1.0/VERSION", "contains": ["VERSION=1.0"]}
        ],
        "license_checks": [
            {
                "path": "source-1.0/LICENSE",
                "contains": ["Permission granted", "Required notice"],
            }
        ],
    }
    row.update(overrides)
    path.write_text(
        json.dumps({"corresponding_source_archives": [row]}),
        encoding="utf-8",
    )
    return path


def fake_open_for(body: bytes, calls: list[tuple[str, int]]):
    def fake_open(request, *, timeout, allowed_hosts, max_redirects):
        calls.append((request.full_url, timeout))
        assert max_redirects <= sources.MAX_REDIRECTS
        assert urllib_host(request.full_url) in allowed_hosts
        return FakeResponse(body, url=request.full_url)

    return fake_open


def urllib_host(url: str) -> str:
    from urllib.parse import urlsplit

    return str(urlsplit(url).hostname)


def test_01_zlib_record_uses_immutable_fossil_then_mutable_official_asset() -> None:
    inventory = json.loads(
        (PROJECT_ROOT / "tools/release/third_party_licenses.json").read_text(encoding="utf-8")
    )
    row = next(
        item
        for item in inventory["corresponding_source_archives"]
        if item["filename"] == "zlib-1.3.1.tar.gz"
    )
    assert row["url"] == "https://zlib.net/fossils/zlib-1.3.1.tar.gz"
    assert row["fallback_urls"] == [
        "https://github.com/madler/zlib/releases/download/v1.3.1/zlib-1.3.1.tar.gz"
    ]
    assert row["sha256"] == ZLIB_HASH
    assert row["size_bytes"] == 1_512_791
    assert row["source_provenance"]["primary"]["immutable"] is True
    assert row["source_provenance"]["fallback"]["immutable"] is False
    assert row["source_provenance"]["fallback"]["release_asset_id"] == 147136750
    assert "zlib-1.3.1/zutil.c" in row["required_paths"]


def test_02_primary_source_retries_once_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body)
    calls: list[str] = []

    def fake_open(request, **_kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError("synthetic transient failure")
        return FakeResponse(body, url=request.full_url)

    monkeypatch.setattr(sources, "_open_response", fake_open)
    rows = sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert rows[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert rows[0]["provenance"] == {
        "kind": "network",
        "source_role": "primary",
        "source_url": "https://primary.example/source.tar.gz",
    }
    assert calls == ["https://primary.example/source.tar.gz"] * 2


def test_03_declared_fallback_is_used_after_bounded_primary_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body)
    calls: list[str] = []

    def fake_open(request, **_kwargs):
        calls.append(request.full_url)
        if "primary.example" in request.full_url:
            raise TimeoutError("synthetic timeout")
        return FakeResponse(body, url=request.full_url, content_type="application/octet-stream")

    monkeypatch.setattr(sources, "_open_response", fake_open)
    sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert calls == [
        "https://primary.example/source.tar.gz",
        "https://primary.example/source.tar.gz",
        "https://fallback.example/source.tar.gz",
    ]


def test_04_interface_accepts_positional_offline_and_inventory_path(tmp_path: Path) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "source-1.0.tar.gz").write_bytes(body)
    rows = sources.fetch_sources(cache, True, inventory)
    row = rows[0]
    assert row["component"] == "Synthetic source"
    assert row["filename"] == "source-1.0.tar.gz"
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert row["size"] == len(body)
    assert row["authoritative_primary"] == {
        "url": "https://primary.example/source.tar.gz",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
        "archive_format": "tar.gz",
        "top_level": "source-1.0",
    }
    assert row["validation"]["cryptographic"]["status"] == "verified"
    assert row["validation"]["archive"]["member_safety"] == "verified"
    assert row["validation"]["semantics"] == {
        "version": "verified",
        "license": "verified",
    }
    assert row["provenance"] == {
        "kind": "cache",
        "source_role": "prevalidated_cache",
        "source_url": None,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://primary.example/source.tar.gz",
        "https://user:secret@primary.example/source.tar.gz",
        "https://primary.example/source.tar.gz?credential=ambiguous",
        "https://127.0.0.1/source.tar.gz",
        "https://[::1]/source.tar.gz",
        "https://localhost/source.tar.gz",
    ],
)
def test_05_inventory_rejects_insecure_ambiguous_or_local_urls(tmp_path: Path, url: str) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body, url=url)
    with pytest.raises(ReleaseError, match="source URL"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_06_unapproved_redirect_is_rejected_and_query_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(
        tmp_path / "inventory.json",
        body,
        content_types=None,
        fallback_urls=[],
        primary_attempts=1,
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda *_args, **_kwargs: FakeResponse(
            body, url="https://evil.example/payload?token=DO_NOT_PRINT"
        ),
    )
    with pytest.raises(ReleaseError) as raised:
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    message = str(raised.value)
    assert "unapproved host" in message
    assert "https://evil.example/payload" in message
    assert "DO_NOT_PRINT" not in message


def test_07_timeout_and_redirect_limits_are_bounded_and_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body)
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(sources, "_open_response", fake_open_for(body, calls))
    sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert calls == [("https://primary.example/source.tar.gz", 7)]

    bad = write_inventory(
        tmp_path / "bad.json", body, timeout_seconds=sources.MAX_TIMEOUT_SECONDS + 1
    )
    with pytest.raises(ReleaseError, match="source timeout"):
        sources.fetch_sources(tmp_path / "other-cache", inventory_path=bad)


def test_08_wrong_size_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = archive_bytes()
    inventory = write_inventory(
        tmp_path / "inventory.json", body, size_bytes=len(body) + 1, fallback_urls=[], primary_attempts=1
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda request, **_kwargs: FakeResponse(body, url=request.full_url),
    )
    with pytest.raises(ReleaseError, match="Content-Length"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_09_wrong_content_type_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(
        tmp_path / "inventory.json",
        body,
        content_types=None,
        fallback_urls=[],
        primary_attempts=1,
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda request, **_kwargs: FakeResponse(
            body, url=request.full_url, content_type="text/html"
        ),
    )
    with pytest.raises(ReleaseError, match="Content-Type"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_10_hash_mismatch_reports_sanitized_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = archive_bytes()
    received = archive_bytes(version="2.0")
    inventory = write_inventory(
        tmp_path / "inventory.json",
        expected,
        size_bytes=len(received),
        fallback_urls=[],
        primary_attempts=1,
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda _request, **_kwargs: FakeResponse(
            received,
            url="https://primary.example/source.tar.gz?signature=SECRET",
        ),
    )
    with pytest.raises(ReleaseError) as raised:
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    message = str(raised.value)
    assert "SHA-256 mismatch" in message
    assert f"actual_bytes={len(received)}" in message
    assert f"actual_sha256={hashlib.sha256(received).hexdigest()}" in message
    assert "content_type=application/x-gzip" in message
    assert "signature" not in message and "SECRET" not in message


@pytest.mark.parametrize(
    "unsafe_member",
    ["../escape.txt", "/absolute.txt"],
    ids=["parent-traversal", "absolute-path"],
)
def test_11_magic_and_archive_traversal_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_member: str
) -> None:
    body = b"not a gzip archive"
    inventory = write_inventory(
        tmp_path / "magic.json",
        body,
        required_paths=[],
        version_checks=[],
        license_checks=[],
        top_level=None,
        fallback_urls=[],
        primary_attempts=1,
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda request, **_kwargs: FakeResponse(body, url=request.full_url),
    )
    with pytest.raises(ReleaseError, match="magic"):
        sources.fetch_sources(tmp_path / "magic-cache", inventory_path=inventory)

    traversal = archive_bytes(extra_members=[(unsafe_member, b"bad")])
    traversal_inventory = write_inventory(
        tmp_path / "traversal.json",
        traversal,
        required_paths=[],
        version_checks=[],
        license_checks=[],
        top_level=None,
    )
    cache = tmp_path / "traversal-cache"
    cache.mkdir()
    (cache / "source-1.0.tar.gz").write_bytes(traversal)
    with pytest.raises(ReleaseError, match="archive member path"):
        sources.fetch_sources(cache, True, traversal_inventory)


def test_12_top_level_and_required_paths_are_enforced(tmp_path: Path) -> None:
    body = archive_bytes(top_level="wrong-root")
    inventory = write_inventory(tmp_path / "inventory.json", body)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "source-1.0.tar.gz").write_bytes(body)
    with pytest.raises(ReleaseError, match="top-level"):
        sources.fetch_sources(cache, True, inventory)

    correct = archive_bytes()
    missing_inventory = write_inventory(
        tmp_path / "missing.json",
        correct,
        required_paths=["source-1.0/VERSION", "source-1.0/LICENSE", "source-1.0/NOTICE"],
    )
    (cache / "source-1.0.tar.gz").write_bytes(correct)
    with pytest.raises(ReleaseError, match="required path"):
        sources.fetch_sources(cache, True, missing_inventory)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (archive_bytes(version="2.0"), "version marker"),
        (archive_bytes(license_text="Permission granted only\n"), "license marker"),
    ],
)
def test_13_offline_cache_runs_version_and_license_semantic_checks(
    tmp_path: Path, body: bytes, match: str
) -> None:
    inventory = write_inventory(tmp_path / "inventory.json", body)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "source-1.0.tar.gz").write_bytes(body)
    with pytest.raises(ReleaseError, match=match):
        sources.fetch_sources(cache, True, inventory)


def test_14_environment_proxies_are_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    captured: list[object] = []
    sentinel = object()

    class FakeOpener:
        def open(self, _request, *, timeout):
            assert timeout == 7
            return sentinel

    def fake_build_opener(*handlers):
        captured.extend(handlers)
        return FakeOpener()

    monkeypatch.setattr(sources.urllib.request, "build_opener", fake_build_opener)
    result = sources._open_response(
        sources.urllib.request.Request("https://example.com/source.tar.gz"),
        timeout=7,
        allowed_hosts={"example.com"},
        max_redirects=3,
    )
    assert result is sentinel
    proxy_handler = next(
        handler for handler in captured if isinstance(handler, sources.urllib.request.ProxyHandler)
    )
    assert proxy_handler.proxies == {}


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.7", "169.254.1.1", "::1"])
def test_15_private_or_loopback_dns_answers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(
        sources.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (sources.socket.AF_INET6 if ":" in address else sources.socket.AF_INET,
             sources.socket.SOCK_STREAM,
             6,
             "",
             (address, 443, 0, 0) if ":" in address else (address, 443))
        ],
    )
    with pytest.raises(ReleaseError, match="non-public"):
        REAL_DNS_VALIDATOR("public-looking.example")


def test_16_maximum_response_size_applies_without_exact_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(
        tmp_path / "inventory.json",
        body,
        size_bytes=None,
        maximum_size_bytes=len(body) - 1,
        fallback_urls=[],
        primary_attempts=1,
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda request, **_kwargs: FakeResponse(body, url=request.full_url),
    )
    with pytest.raises(ReleaseError, match="maximum allowed byte size"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_17_stream_growth_is_bounded_when_content_length_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(
        tmp_path / "inventory.json",
        body,
        size_bytes=None,
        maximum_size_bytes=len(body) - 1,
        fallback_urls=[],
        primary_attempts=1,
    )

    class NoLengthResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__(body)
            self.headers.pop("Content-Length")

    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda *_args, **_kwargs: NoLengthResponse(),
    )
    with pytest.raises(ReleaseError, match="maximum allowed byte size"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_18_monotonic_deadline_covers_the_whole_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(
        tmp_path / "inventory.json", body, fallback_urls=[], primary_attempts=1
    )
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda request, **_kwargs: FakeResponse(body, url=request.full_url),
    )
    moments = iter([0.0, 0.1, 0.2, 8.0])
    monkeypatch.setattr(sources.time, "monotonic", lambda: next(moments))
    with pytest.raises(ReleaseError, match="whole-attempt deadline"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_19_repository_live_or_tracked_locations_cannot_be_caches(tmp_path: Path) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body)
    unsafe = PROJECT_ROOT / "data" / "batch8_1_forbidden_cache"
    assert not unsafe.exists()
    with pytest.raises(ReleaseError, match="ignored release_artifacts"):
        sources.fetch_sources(unsafe, True, inventory)
    assert not unsafe.exists()


def test_20_symlink_or_reparse_cache_components_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive_bytes()
    inventory = write_inventory(tmp_path / "inventory.json", body)
    cache = tmp_path / "unsafe-cache"
    cache.mkdir()
    original = sources.is_reparse_or_link
    monkeypatch.setattr(
        sources,
        "is_reparse_or_link",
        lambda path: path == cache or original(path),
    )
    with pytest.raises(ReleaseError, match="symlink or reparse"):
        sources.fetch_sources(cache, True, inventory)


def test_21_redirect_dns_is_validated_before_urllib_follows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = sources._SafeRedirectHandler({"allowed.example"}, 3)

    def reject_before_follow(host: str) -> None:
        assert host == "allowed.example"
        raise ReleaseError("synthetic private redirect DNS")

    monkeypatch.setattr(sources, "_validate_public_dns", reject_before_follow)
    request = sources.urllib.request.Request("https://origin.example/source.tar.gz")
    with pytest.raises(ReleaseError, match="private redirect DNS"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://allowed.example/source.tar.gz?temporary=signature",
        )


def test_22_real_pytest_tmp_path_is_an_approved_cache_root(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    result = sources._prepare_cache(cache)
    assert sources._comparison_path(result) == sources._comparison_path(cache.resolve())


def test_23_cache_below_stdlib_temp_root_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard_root = tmp_path / "stdlib-temp"
    isolate_temp_roots(monkeypatch, standard_root)
    cache = standard_root / "cache"
    result = sources._prepare_cache(cache)
    assert sources._comparison_path(result) == sources._comparison_path(cache.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows path-case semantics")
def test_24_windows_case_differences_are_canonicalized(tmp_path: Path) -> None:
    cache = tmp_path / "Case-Different-Cache"
    alternate_case = Path(str(cache).swapcase())
    result = sources._prepare_cache(alternate_case)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(cache)


@pytest.mark.skipif(os.name != "nt", reason="Windows separator semantics")
def test_25_windows_separator_variants_are_equivalent(tmp_path: Path) -> None:
    cache = tmp_path / "separator-cache"
    forward_slashes = Path(str(cache).replace("\\", "/"))
    result = sources._prepare_cache(forward_slashes)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(cache)


def test_26_resolved_pytest_path_matches_unresolved_temp_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_root = tmp_path / "long-temp-root"
    long_root.mkdir()
    alias_root = tmp_path / "RUNNER~1"
    isolate_temp_roots(monkeypatch, long_root)
    monkeypatch.setattr(sources.tempfile, "gettempdir", lambda: str(alias_root))
    patch_canonical_alias(monkeypatch, alias_root, long_root)
    cache = long_root / "pytest-cache"
    result = sources._prepare_cache(cache)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(cache)


def test_26_inverse_temp_alias_is_accepted_without_trusting_reparse_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_root = tmp_path / "long-temp-root"
    long_root.mkdir()
    alias_root = tmp_path / "RUNNER~1"
    alias_root.mkdir()
    isolate_temp_roots(monkeypatch, long_root)
    patch_canonical_alias(monkeypatch, alias_root, long_root)
    cache = alias_root / "pytest-cache"
    result = sources._prepare_cache(cache)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(
        long_root / "pytest-cache"
    )


def test_26_reparse_alias_cannot_become_an_approved_temp_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_root = tmp_path / "long-temp-root"
    long_root.mkdir()
    alias_root = tmp_path / "linked-temp-root"
    alias_root.mkdir()
    isolate_temp_roots(monkeypatch, long_root)
    patch_canonical_alias(monkeypatch, alias_root, long_root)
    original = sources.is_reparse_or_link
    alias_key = sources._comparison_path(alias_root)
    monkeypatch.setattr(
        sources,
        "is_reparse_or_link",
        lambda path: sources._comparison_path(path) == alias_key or original(path),
    )
    cache = alias_root / "cache"
    with pytest.raises(ReleaseError, match="approved operating-system temp"):
        sources._prepare_cache(cache)
    assert not cache.exists()


@pytest.mark.parametrize("environment_name", ["TEMP", "TMP", "TMPDIR"])
def test_27_environment_temp_roots_are_accepted_after_canonicalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
) -> None:
    standard_root = tmp_path / "stdlib-temp"
    environment_root = tmp_path / f"{environment_name.casefold()}-root"
    environment_root.mkdir()
    isolate_temp_roots(monkeypatch, standard_root)
    monkeypatch.setenv(environment_name, str(environment_root))
    cache = environment_root / "cache"
    result = sources._prepare_cache(cache)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(cache)


def test_28_runner_temp_is_accepted_only_in_github_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard_root = tmp_path / "stdlib-temp"
    runner_root = tmp_path / "runner-temp"
    runner_root.mkdir()
    isolate_temp_roots(monkeypatch, standard_root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(runner_root))
    cache = runner_root / "cache"
    result = sources._prepare_cache(cache)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(cache)


def test_29_runner_temp_is_not_trusted_outside_github_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard_root = tmp_path / "stdlib-temp"
    runner_root = tmp_path / "runner-temp"
    runner_root.mkdir()
    isolate_temp_roots(monkeypatch, standard_root)
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.setenv("RUNNER_TEMP", str(runner_root))
    cache = runner_root / "cache"
    with pytest.raises(ReleaseError, match="approved operating-system temp"):
        sources._prepare_cache(cache)
    assert not cache.exists()


def test_30_duplicate_temp_aliases_are_deduplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_root = tmp_path / "long-temp-root"
    long_root.mkdir()
    alias_root = tmp_path / "RUNNER~1"
    isolate_temp_roots(monkeypatch, long_root)
    monkeypatch.setattr(sources.tempfile, "gettempdir", lambda: str(alias_root))
    monkeypatch.setenv("TEMP", str(long_root))
    patch_canonical_alias(monkeypatch, alias_root, long_root)
    records = sources._approved_temp_root_records()
    assert len(records) == 1
    assert len(records[0][1]) == 2


def test_31_most_specific_matching_temp_root_is_selected(tmp_path: Path) -> None:
    outer = sources._canonicalize_release_path(tmp_path)
    inner = sources._canonicalize_release_path(tmp_path / "nested")
    cache = sources._canonicalize_release_path(tmp_path / "nested" / "cache")
    selected = sources._select_cache_boundary(cache, [outer, inner])
    assert selected == inner


def test_32_component_containment_rejects_sibling_prefix(tmp_path: Path) -> None:
    approved = sources._canonicalize_release_path(tmp_path / "Temp")
    sibling = sources._canonicalize_release_path(tmp_path / "TempEvil" / "cache")
    assert not sources._path_is_within(sibling, approved)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive semantics")
def test_33_different_windows_drive_is_rejected_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard_root = tmp_path / "stdlib-temp"
    isolate_temp_roots(monkeypatch, standard_root)
    current_drive = tmp_path.drive.casefold()
    other_drive = "Z:" if current_drive != "z:" else "Y:"
    cache = Path(f"{other_drive}\\MusicVaultBatch84\\cache")
    with pytest.raises(ReleaseError, match="approved operating-system temp"):
        sources._prepare_cache(cache)
    assert not cache.exists()


def test_34_relative_parent_traversal_cannot_escape_release_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(PROJECT_ROOT)
    target = PROJECT_ROOT / "data" / "batch8_4_parent_escape_cache"
    with pytest.raises(ReleaseError, match="ignored release_artifacts"):
        sources._prepare_cache(
            Path("release_artifacts") / ".." / "data" / target.name
        )
    assert not target.exists()


@pytest.mark.parametrize(
    "cache",
    [
        PROJECT_ROOT / "tests" / "test_batch8_1_sources.py",
        PROJECT_ROOT / "batch8_4_unignored_cache",
    ],
    ids=["tracked", "unignored"],
)
def test_35_tracked_and_unignored_repository_caches_remain_rejected(cache: Path) -> None:
    with pytest.raises(ReleaseError, match="ignored release_artifacts"):
        sources._prepare_cache(cache)
    if cache.name == "batch8_4_unignored_cache":
        assert not cache.exists()


def test_36_release_artifacts_cache_must_be_ignored_and_untracked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_project = tmp_path / "fake-project"
    fake_project.mkdir()
    isolate_temp_roots(monkeypatch, tmp_path / "stdlib-temp")
    monkeypatch.setattr(sources, "PROJECT_ROOT", fake_project)
    monkeypatch.setattr(sources, "_git_cache_is_ignored_and_untracked", lambda _relative: False)
    cache = fake_project / "release_artifacts" / "cache"
    with pytest.raises(ReleaseError, match="ignored and untracked"):
        sources._prepare_cache(cache)
    assert not cache.exists()


def test_37_ignored_untracked_release_artifacts_cache_remains_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_project = tmp_path / "fake-project"
    fake_project.mkdir()
    isolate_temp_roots(monkeypatch, tmp_path / "stdlib-temp")
    monkeypatch.setattr(sources, "PROJECT_ROOT", fake_project)
    monkeypatch.setattr(sources, "_git_cache_is_ignored_and_untracked", lambda _relative: True)
    cache = fake_project / "release_artifacts" / "cache"
    result = sources._prepare_cache(cache)
    assert sources._canonicalize_release_path(result) == sources._canonicalize_release_path(cache)


def test_38_intermediate_reparse_cache_component_remains_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir()
    cache = unsafe_parent / "cache"
    original = sources.is_reparse_or_link
    unsafe_key = sources._comparison_path(unsafe_parent)
    monkeypatch.setattr(
        sources,
        "is_reparse_or_link",
        lambda path: sources._comparison_path(path) == unsafe_key or original(path),
    )
    with pytest.raises(ReleaseError, match="symlink or reparse"):
        sources._prepare_cache(cache)
    assert not cache.exists()


def test_39_post_creation_canonical_resolution_escape_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = tmp_path / "approved-temp"
    approved.mkdir()
    outside = tmp_path / "outside"
    isolate_temp_roots(monkeypatch, approved)
    cache = approved / "cache"
    escaped = outside / "cache"
    original_realpath = sources.os.path.realpath
    cache_key = sources._comparison_path(cache)

    def escaping_realpath(value: str | bytes | os.PathLike[str]) -> str:
        absolute = sources._comparison_path(sources._absolute_release_path(os.fspath(value)))
        if absolute == cache_key and cache.exists():
            return os.fspath(escaped)
        return original_realpath(value)

    monkeypatch.setattr(sources.os.path, "realpath", escaping_realpath)
    with pytest.raises(ReleaseError, match="escaped its approved boundary"):
        sources._prepare_cache(cache)
    assert cache.is_dir()
    assert not escaped.exists()


def test_40_repository_cache_cannot_resolve_outside_release_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_project = tmp_path / "fake-project"
    fake_project.mkdir()
    isolate_temp_roots(monkeypatch, tmp_path / "stdlib-temp")
    monkeypatch.setattr(sources, "PROJECT_ROOT", fake_project)
    monkeypatch.setattr(sources, "_git_cache_is_ignored_and_untracked", lambda _relative: True)
    cache = fake_project / "release_artifacts" / "cache"
    escaped = fake_project / "data" / "cache"
    original_realpath = sources.os.path.realpath
    cache_key = sources._comparison_path(cache)

    def escaping_realpath(value: str | bytes | os.PathLike[str]) -> str:
        absolute = sources._comparison_path(sources._absolute_release_path(os.fspath(value)))
        if absolute == cache_key and cache.exists():
            return os.fspath(escaped)
        return original_realpath(value)

    monkeypatch.setattr(sources.os.path, "realpath", escaping_realpath)
    with pytest.raises(ReleaseError, match="escaped its approved boundary"):
        sources._prepare_cache(cache)
    assert not escaped.exists()


def test_41_cache_entries_still_reject_links_or_reparse_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = tmp_path / "cache" / "source.tar.gz"
    entry.parent.mkdir()
    entry.write_bytes(b"synthetic")
    monkeypatch.setattr(sources, "is_reparse_or_link", lambda path: path == entry)
    with pytest.raises(ReleaseError, match="link or reparse point"):
        sources._validate_cache_entry(entry)


@pytest.mark.skipif(os.name == "nt", reason="POSIX containment semantics")
def test_42_posix_temp_containment_remains_component_aware(tmp_path: Path) -> None:
    approved = sources._canonicalize_release_path(tmp_path / "temp")
    child = sources._canonicalize_release_path(tmp_path / "temp" / "cache")
    sibling = sources._canonicalize_release_path(tmp_path / "temp-evil" / "cache")
    assert sources._path_is_within(child, approved)
    assert not sources._path_is_within(sibling, approved)


def test_43_relative_or_empty_environment_temp_roots_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standard_root = tmp_path / "stdlib-temp"
    isolate_temp_roots(monkeypatch, standard_root)
    monkeypatch.setenv("TEMP", "")
    monkeypatch.setenv("TMP", "relative-temp")
    roots = sources._approved_temp_roots()
    assert roots == (sources._canonicalize_release_path(standard_root),)


def test_44_current_working_directory_cannot_be_the_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ReleaseError, match="current working directory"):
        sources._prepare_cache(Path.cwd())


def test_45_pyside_record_scopes_the_exact_official_mirror_and_validation() -> None:
    inventory = json.loads(
        (PROJECT_ROOT / "tools/release/third_party_licenses.json").read_text(encoding="utf-8")
    )
    row = next(
        item
        for item in inventory["corresponding_source_archives"]
        if item["filename"] == PYSIDE_FILENAME
    )
    assert row["url"] == PYSIDE_PRIMARY
    assert row["fallback_urls"] == [PYSIDE_MIRROR]
    assert row["allowed_hosts"] == ["download.qt.io", "qt.mirror.constant.com"]
    assert row["sha256"] == PYSIDE_HASH
    assert row["size_bytes"] == PYSIDE_SIZE
    assert row["maximum_size_bytes"] == sources.MAX_SOURCE_ARCHIVE_BYTES
    assert row["archive_format"] == "tar.xz"
    assert row["timeout_seconds"] == 60
    assert row["max_redirects"] == 5
    assert row["primary_attempts"] == row["fallback_attempts"] == 1
    assert row["top_level"] == PYSIDE_ROOT
    assert len(row["version_checks"]) == 2
    assert len(row["license_checks"]) == 3
    assert row["source_provenance"]["primary"]["kind"] == "official Qt download origin"
    assert row["source_provenance"]["fallback"] == {
        "kind": "official Qt mirror",
        "operator": "Constant Hosting",
        "official_mirror_directory": "https://download.qt.io/static/mirrorlist/",
    }


def test_46_pyside_exact_mirror_redirect_is_accepted_and_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = pyside_archive_bytes()
    inventory = write_pyside_inventory(tmp_path / "pyside.json", body)
    handler = sources._SafeRedirectHandler(
        {"download.qt.io", "qt.mirror.constant.com"}, 3
    )
    redirected = handler.redirect_request(
        sources.urllib.request.Request(PYSIDE_PRIMARY),
        None,
        302,
        "Found",
        {},
        PYSIDE_MIRROR,
    )
    assert redirected.full_url == PYSIDE_MIRROR

    calls: list[str] = []

    def fake_open(request, **_kwargs):
        calls.append(request.full_url)
        return FakeResponse(body, url=PYSIDE_MIRROR, content_type="application/x-xz")

    monkeypatch.setattr(sources, "_open_response", fake_open)
    rows = sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert calls == [PYSIDE_PRIMARY]
    assert rows[0]["provenance"] == {
        "kind": "network",
        "source_role": "primary",
        "source_url": PYSIDE_PRIMARY,
        "redirect_url": PYSIDE_MIRROR,
    }


@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://ftp.fau.de/qtproject/official_releases/QtForPython/pyside6/"
        "PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz",
        "https://mirror.constant.com/official_releases/QtForPython/pyside6/"
        "PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz",
    ],
)
def test_47_pyside_redirect_rejects_undeclared_qt_and_constant_hosts(
    redirect_url: str,
) -> None:
    handler = sources._SafeRedirectHandler(
        {"download.qt.io", "qt.mirror.constant.com"}, 3
    )
    with pytest.raises(ReleaseError, match="unapproved host"):
        handler.redirect_request(
            sources.urllib.request.Request(PYSIDE_PRIMARY),
            None,
            302,
            "Found",
            {},
            redirect_url,
        )


def test_48_pyside_declared_fallback_survives_an_unusable_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = pyside_archive_bytes()
    inventory = write_pyside_inventory(tmp_path / "pyside.json", body)
    calls: list[str] = []

    def fake_open(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url == PYSIDE_PRIMARY:
            raise ReleaseError("Source download redirected to an unapproved host")
        return FakeResponse(body, url=request.full_url, content_type="application/x-xz")

    monkeypatch.setattr(sources, "_open_response", fake_open)
    rows = sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert calls == [PYSIDE_PRIMARY, PYSIDE_MIRROR]
    assert rows[0]["provenance"] == {
        "kind": "network",
        "source_role": "fallback",
        "source_url": PYSIDE_MIRROR,
    }
    assert rows[0]["validation"]["cryptographic"]["status"] == "verified"
    assert rows[0]["validation"]["size"]["status"] == "verified"
    assert rows[0]["validation"]["archive"]["format"] == "tar.xz"
    assert rows[0]["validation"]["semantics"] == {
        "version": "verified",
        "license": "verified",
    }


def test_49_pyside_altered_mirror_path_cannot_bypass_the_pinned_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = pyside_archive_bytes()
    received = bytearray(body)
    received[-1] ^= 1
    inventory = write_pyside_inventory(
        tmp_path / "pyside.json",
        body,
        fallback_urls=[],
    )
    altered = PYSIDE_MIRROR.replace(PYSIDE_FILENAME, "altered-source.tar.xz")
    altered += "?token=DO_NOT_PRINT"
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda *_args, **_kwargs: FakeResponse(
            bytes(received),
            url=altered,
            content_type="application/x-xz",
        ),
    )
    with pytest.raises(ReleaseError) as raised:
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    message = str(raised.value)
    assert "SHA-256 mismatch" in message
    assert "https://qt.mirror.constant.com/official_releases/QtForPython/pyside6/" in message
    assert "altered-source.tar.xz" in message
    assert "DO_NOT_PRINT" not in message


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("hash", "SHA-256 mismatch"),
        ("size", "Content-Length"),
        ("archive", "magic"),
        ("version", "version marker mismatch"),
        ("license", "license file hash mismatch"),
        ("html", "Content-Type"),
    ],
)
def test_50_pyside_direct_mirror_fallback_remains_fail_closed(
    case: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = pyside_archive_bytes()
    if case == "archive":
        body = b"not a tar.xz archive"
    elif case == "version":
        body = pyside_archive_bytes(pyside_version=("6", "11", "2"))
    elif case == "license":
        body = pyside_archive_bytes(license_text=b"unexpected license\n")
    overrides: dict[str, object] = {}
    if case == "hash":
        overrides["sha256"] = "0" * 64
    elif case == "size":
        overrides["size_bytes"] = len(body) + 1
    inventory = write_pyside_inventory(tmp_path / "pyside.json", body, **overrides)

    def fake_open(request, **_kwargs):
        if request.full_url == PYSIDE_PRIMARY:
            raise TimeoutError("synthetic primary failure")
        content_type = "text/html" if case == "html" else "application/x-xz"
        return FakeResponse(body, url=request.full_url, content_type=content_type)

    monkeypatch.setattr(sources, "_open_response", fake_open)
    with pytest.raises(ReleaseError, match=error):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


def test_51_pyside_http_mirror_fallback_is_rejected(tmp_path: Path) -> None:
    body = pyside_archive_bytes()
    inventory = write_pyside_inventory(
        tmp_path / "pyside.json",
        body,
        fallback_urls=[PYSIDE_MIRROR.replace("https://", "http://")],
    )
    with pytest.raises(ReleaseError, match="source fallback URL"):
        sources._source_rows(inventory)


def test_52_exactly_four_remaining_qt_submodule_records_are_handled() -> None:
    inventory = json.loads(
        (PROJECT_ROOT / "tools/release/third_party_licenses.json").read_text(encoding="utf-8")
    )
    rows = [
        row
        for row in inventory["corresponding_source_archives"]
        if str(row.get("url", "")).startswith(QT_SUBMODULE_ORIGIN)
    ]
    assert {row["filename"] for row in rows} == {
        str(case["filename"]) for case in QT_SUBMODULE_CASES
    }
    assert len(rows) == len(QT_SUBMODULE_CASES) == 4
    assert all(row.get("fallback_urls") for row in rows)


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
def test_53_qt_submodule_record_preserves_identity_and_scoped_policy(
    case: dict[str, object],
) -> None:
    inventory = json.loads(
        (PROJECT_ROOT / "tools/release/third_party_licenses.json").read_text(encoding="utf-8")
    )
    filename = str(case["filename"])
    root = filename.removesuffix(".tar.xz")
    row = next(
        item
        for item in inventory["corresponding_source_archives"]
        if item["filename"] == filename
    )
    assert row["url"] == QT_SUBMODULE_ORIGIN + filename
    assert row["fallback_urls"] == [QT_SUBMODULE_MIRROR + filename]
    assert row["sha256"] == case["sha256"]
    assert row["size_bytes"] == case["size_bytes"]
    assert row["maximum_size_bytes"] == sources.MAX_SOURCE_ARCHIVE_BYTES
    assert row["archive_format"] == "tar.xz"
    assert set(row["content_types"]) == {
        "application/x-xz",
        "application/x-tar",
        "application/octet-stream",
    }
    assert row["allowed_hosts"] == ["download.qt.io", "qt.mirror.constant.com"]
    assert all("*" not in host for host in row["allowed_hosts"])
    assert not any(
        "qt.mirror.constant.com" in hosts
        for hosts in sources.KNOWN_HTTPS_REDIRECT_HOSTS.values()
    )
    assert row["timeout_seconds"] == 60
    assert row["max_redirects"] == 5
    assert row["primary_attempts"] == row["fallback_attempts"] == 1
    assert row["top_level"] == root
    assert f"{root}/CMakeLists.txt" in row["required_paths"]
    assert f"{root}/.cmake.conf" in row["required_paths"]
    assert len(row["version_checks"]) == 2
    assert len(row["license_checks"]) == 3
    assert row["source_provenance"]["primary"] == {
        "kind": "official Qt download origin",
        "host": "download.qt.io",
    }
    assert row["source_provenance"]["fallback"] == {
        "kind": "official Qt mirror",
        "host": "qt.mirror.constant.com",
        "operator": "Constant Hosting",
        "official_mirror_directory": "https://download.qt.io/static/mirrorlist/",
    }


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
def test_54_each_qt_submodule_exact_redirect_is_accepted_and_recorded(
    case: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = str(case["filename"])
    primary = QT_SUBMODULE_ORIGIN + filename
    mirror = QT_SUBMODULE_MIRROR + filename
    body = qt_submodule_archive_bytes(case)
    inventory = write_qt_submodule_inventory(tmp_path / "qt.json", case, body)
    handler = sources._SafeRedirectHandler(
        {"download.qt.io", "qt.mirror.constant.com"}, 3
    )
    redirected = handler.redirect_request(
        sources.urllib.request.Request(primary),
        None,
        302,
        "Found",
        {},
        mirror,
    )
    assert redirected.full_url == mirror
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda *_args, **_kwargs: FakeResponse(
            body,
            url=mirror,
            content_type="application/x-xz",
        ),
    )
    rows = sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert rows[0]["provenance"] == {
        "kind": "network",
        "source_role": "primary",
        "source_url": primary,
        "redirect_url": mirror,
    }


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
def test_55_each_qt_submodule_direct_fallback_is_verified(
    case: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = str(case["filename"])
    primary = QT_SUBMODULE_ORIGIN + filename
    mirror = QT_SUBMODULE_MIRROR + filename
    body = qt_submodule_archive_bytes(case)
    inventory = write_qt_submodule_inventory(tmp_path / "qt.json", case, body)
    calls: list[str] = []

    def fake_open(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url == primary:
            raise ReleaseError("synthetic rejected primary redirect")
        return FakeResponse(body, url=mirror, content_type="application/x-xz")

    monkeypatch.setattr(sources, "_open_response", fake_open)
    rows = sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    assert calls == [primary, mirror]
    assert rows[0]["provenance"] == {
        "kind": "network",
        "source_role": "fallback",
        "source_url": mirror,
    }
    assert rows[0]["validation"]["cryptographic"]["status"] == "verified"
    assert rows[0]["validation"]["size"]["status"] == "verified"
    assert rows[0]["validation"]["archive"]["format"] == "tar.xz"
    assert rows[0]["validation"]["semantics"] == {
        "version": "verified",
        "license": "verified",
    }


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
@pytest.mark.parametrize(
    "bad_host",
    ["ftp.fau.de", "mirror.constant.com"],
    ids=["undeclared-qt-mirror", "constant-sibling"],
)
def test_56_qt_submodule_redirects_reject_every_undeclared_host(
    case: dict[str, object],
    bad_host: str,
) -> None:
    filename = str(case["filename"])
    handler = sources._SafeRedirectHandler(
        {"download.qt.io", "qt.mirror.constant.com"}, 3
    )
    with pytest.raises(ReleaseError, match="unapproved host"):
        handler.redirect_request(
            sources.urllib.request.Request(QT_SUBMODULE_ORIGIN + filename),
            None,
            302,
            "Found",
            {},
            f"https://{bad_host}/archive/qt/6.11/6.11.1/submodules/{filename}",
        )


@pytest.mark.parametrize(
    "case_index",
    range(len(QT_SUBMODULE_CASES)),
    ids=[str(case["project"]) for case in QT_SUBMODULE_CASES],
)
def test_57_wrong_qt_component_path_cannot_bypass_the_pinned_hash(
    case_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = QT_SUBMODULE_CASES[case_index]
    wrong_case = QT_SUBMODULE_CASES[(case_index + 1) % len(QT_SUBMODULE_CASES)]
    body = qt_submodule_archive_bytes(case)
    received = bytearray(body)
    received[-1] ^= 1
    inventory = write_qt_submodule_inventory(
        tmp_path / "qt.json",
        case,
        body,
        fallback_urls=[],
    )
    wrong_url = QT_SUBMODULE_MIRROR + str(wrong_case["filename"])
    monkeypatch.setattr(
        sources,
        "_open_response",
        lambda *_args, **_kwargs: FakeResponse(
            bytes(received),
            url=wrong_url,
            content_type="application/x-xz",
        ),
    )
    with pytest.raises(ReleaseError) as raised:
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
    message = str(raised.value)
    assert "SHA-256 mismatch" in message
    assert str(wrong_case["filename"]) in message


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("hash", "SHA-256 mismatch"),
        ("size", "Content-Length"),
        ("html", "Content-Type"),
        ("archive", "magic"),
        ("traversal", "archive member path"),
        ("version", "version marker mismatch"),
        ("license", "required path"),
    ],
)
def test_58_qt_submodule_mirror_delivery_remains_fail_closed(
    case: dict[str, object],
    failure: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = qt_submodule_archive_bytes(case)
    if failure == "archive":
        body = b"not a tar.xz archive"
    elif failure == "traversal":
        body = qt_submodule_archive_bytes(case, unsafe_member="../escape.txt")
    elif failure == "version":
        body = qt_submodule_archive_bytes(case, version="6.11.2")
    elif failure == "license":
        body = qt_submodule_archive_bytes(case, omit_license="LGPL-3.0-only.txt")
    overrides: dict[str, object] = {}
    if failure == "hash":
        overrides["sha256"] = "0" * 64
    elif failure == "size":
        overrides["size_bytes"] = len(body) + 1
    inventory = write_qt_submodule_inventory(
        tmp_path / "qt.json",
        case,
        body,
        **overrides,
    )
    primary = QT_SUBMODULE_ORIGIN + str(case["filename"])

    def fake_open(request, **_kwargs):
        if request.full_url == primary:
            raise TimeoutError("synthetic primary failure")
        content_type = "text/html" if failure == "html" else "application/x-xz"
        return FakeResponse(body, url=request.full_url, content_type=content_type)

    monkeypatch.setattr(sources, "_open_response", fake_open)
    with pytest.raises(ReleaseError, match=error):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
def test_59_qt_submodule_http_fallback_is_rejected(
    case: dict[str, object],
    tmp_path: Path,
) -> None:
    body = qt_submodule_archive_bytes(case)
    filename = str(case["filename"])
    inventory = write_qt_submodule_inventory(
        tmp_path / "qt.json",
        case,
        body,
        fallback_urls=[(QT_SUBMODULE_MIRROR + filename).replace("https://", "http://")],
    )
    with pytest.raises(ReleaseError, match="source fallback URL"):
        sources._source_rows(inventory)


@pytest.mark.parametrize(
    "case",
    QT_SUBMODULE_CASES,
    ids=lambda case: str(case["project"]),
)
def test_60_qt_submodule_private_dns_is_rejected_without_network(
    case: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = qt_submodule_archive_bytes(case)
    inventory = write_qt_submodule_inventory(tmp_path / "qt.json", case, body)
    monkeypatch.setattr(sources, "_validate_public_dns", REAL_DNS_VALIDATOR)
    monkeypatch.setattr(
        sources.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                sources.socket.AF_INET,
                sources.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            )
        ],
    )
    with pytest.raises(ReleaseError, match="non-public"):
        sources.fetch_sources(tmp_path / "cache", inventory_path=inventory)
