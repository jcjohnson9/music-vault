from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.error
from pathlib import Path

import pytest

from tools.release import fetch_compliance_sources as sources
from tools.release.release_common import PROJECT_ROOT, ReleaseError


ZLIB_HASH = "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"
REAL_DNS_VALIDATOR = sources._validate_public_dns


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "_validate_public_dns", lambda _host: None)


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
