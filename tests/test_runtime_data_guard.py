from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _runtime_data_guard import RuntimeDataAccessBlocked, RuntimeDataGuard


@pytest.mark.parametrize(
    ("event", "arguments"),
    [
        ("open", ("data/private.txt", "r", os.O_RDONLY)),
        ("open", ("data/private.txt", "w", os.O_WRONLY | os.O_CREAT)),
        ("os.mkdir", ("data", 0o777, -1)),
        ("os.listdir", ("data",)),
        ("os.scandir", ("data/covers",)),
        ("os.remove", ("data/private.txt", -1)),
        ("os.rename", ("temporary.txt", "data/private.txt", -1, -1)),
        ("sqlite3.connect", ("data/private.sqlite3",)),
    ],
)
def test_runtime_guard_denies_access_before_any_io(tmp_path, monkeypatch, event, arguments):
    monkeypatch.chdir(tmp_path)
    guard = RuntimeDataGuard(tmp_path)
    with pytest.raises(RuntimeDataAccessBlocked, match="synthetic temporary root"):
        guard.audit(event, arguments)
    assert len(guard.violations) == 1
    assert not (tmp_path / "data").exists()
    assert "private" not in json.dumps(guard.violations)


@pytest.mark.parametrize("query", ["mode=ro&immutable=1", "mode=rw", "mode=rwc"])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_runtime_guard_denies_sqlite_uri_even_if_readonly(tmp_path, query, as_bytes):
    guard = RuntimeDataGuard(tmp_path)
    uri = (tmp_path / "data" / "a synthetic.sqlite3").as_uri() + "?" + query
    if as_bytes:
        uri = os.fsencode(uri)
    with pytest.raises(RuntimeDataAccessBlocked):
        guard.audit("sqlite3.connect", (uri,))


def test_runtime_guard_keeps_evidence_after_swallowed_exception(tmp_path):
    guard = RuntimeDataGuard(tmp_path)
    try:
        guard.audit("open", (tmp_path / "data" / "status.json", "w", os.O_WRONLY))
    except OSError:
        pass
    assert guard.violations == [{"event": "open", "operation": "write"}]


def test_runtime_guard_allows_only_public_reads_evidence_and_synthetic_roots(tmp_path):
    guard = RuntimeDataGuard(tmp_path / "project")
    for name in ("README.md", ".gitkeep"):
        target = tmp_path / "project" / "data" / name
        guard.audit("open", (target, "r", os.O_RDONLY))
        with pytest.raises(RuntimeDataAccessBlocked):
            guard.audit("open", (target, "w", os.O_WRONLY))
    for target in (
        tmp_path / "project" / "data" / "astra_reports" / "evidence.json",
        tmp_path / "synthetic" / "data" / "youtube_api_key.txt",
        tmp_path / "project" / "data-other" / "fixture.txt",
    ):
        guard.audit("open", (target, "w", os.O_WRONLY))
    assert len(guard.violations) == 2


def test_runtime_guard_normalizes_parent_components(tmp_path):
    guard = RuntimeDataGuard(tmp_path)
    target = tmp_path / "fixture" / ".." / "data" / "private.json"
    with pytest.raises(RuntimeDataAccessBlocked):
        guard.audit("open", (target, "r", os.O_RDONLY))


def test_runtime_guard_resolves_symbolic_link_alias(tmp_path):
    root = tmp_path / "project"
    data = root / "data"
    data.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(data, target_is_directory=True)
    except OSError:
        pytest.skip("This host does not permit creating symbolic links")
    guard = RuntimeDataGuard(root)
    with pytest.raises(RuntimeDataAccessBlocked):
        guard.audit("open", (alias / "private.json", "r", os.O_RDONLY))
