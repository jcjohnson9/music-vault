"""Network-free acquisition capability, executable trust, and privacy checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from music_vault.core import acquisition_runtime as runtime


@pytest.fixture(autouse=True)
def clear_readiness_caches():
    runtime._check_packages.cache_clear()
    runtime._probe_runtime.cache_clear()
    yield
    runtime._check_packages.cache_clear()
    runtime._probe_runtime.cache_clear()


@pytest.fixture
def packages(monkeypatch, tmp_path):
    """Synthetic local resources; no provider, installer, or network involved."""
    from yt_dlp.extractor.youtube.jsc._builtin import vendor

    monkeypatch.setattr(runtime.importlib.metadata, "version", runtime.ACQUISITION_PINS.__getitem__)
    monkeypatch.setattr(vendor, "VERSION", runtime.ACQUISITION_PINS["yt-dlp-ejs"])
    resources = tmp_path / "solver"
    resources.mkdir()
    for part in ("core", "lib"):
        code = f"const {part} = 'synthetic';\n"
        (resources / f"{part}.min.js").write_text(code, encoding="utf-8")
        monkeypatch.setitem(
            vendor.HASHES, f"yt.solver.{part}.min.js",
            hashlib.sha3_512(code.encode()).hexdigest(),
        )
    monkeypatch.setattr(runtime.importlib.resources, "files", lambda name: resources)
    return resources


@pytest.fixture
def executable(monkeypatch, tmp_path):
    path = tmp_path / "environment" / "deno.exe"
    path.parent.mkdir()
    path.write_bytes(b"synthetic trusted runtime")
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "DENO_WINDOWS_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(runtime, "_runtime_path", lambda: path)
    monkeypatch.setattr(
        runtime.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="deno 2.9.5\nv8 synthetic\n"),
    )
    return path


def test_declared_pins_match_development_and_release_requirements():
    root = Path(__file__).resolve().parents[1]
    for filename in ("requirements.txt", "requirements-release.txt"):
        declared = {}
        for line in (root / filename).read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith("#"):
                name, version = line.split("==", 1)
                declared[name.replace("[default]", "")] = version
        assert all(declared[name] == version for name, version in runtime.ACQUISITION_PINS.items())
    assert len(runtime.DENO_WINDOWS_SHA256) == 64
    assert int(runtime.DENO_WINDOWS_SHA256, 16) > 0


def test_all_package_versions_and_both_solver_resources_are_checked(packages):
    assert runtime._check_packages() == ("2026.8.19", "0.8.0", None)


@pytest.mark.parametrize("dependency", tuple(runtime.ACQUISITION_PINS))
def test_each_wrong_dependency_pin_fails_closed(packages, monkeypatch, dependency):
    monkeypatch.setattr(
        runtime.importlib.metadata, "version",
        lambda name: "0.0.0" if name == dependency else runtime.ACQUISITION_PINS[name],
    )
    assert runtime._check_packages()[2] == "dependency_version_mismatch"


def test_missing_package_has_fixed_error_without_exception_text(monkeypatch):
    def missing(name):
        raise runtime.importlib.metadata.PackageNotFoundError("synthetic-private-package-name")

    monkeypatch.setattr(runtime.importlib.metadata, "version", missing)
    assert runtime._check_packages() == (None, None, "dependencies_incomplete")


def test_solver_vendor_version_must_match_package(packages, monkeypatch):
    from yt_dlp.extractor.youtube.jsc._builtin import vendor

    monkeypatch.setattr(vendor, "VERSION", "0.0.0")
    assert runtime._check_packages()[2] == "solver_version_mismatch"


@pytest.mark.parametrize("part", ("core", "lib"))
def test_modified_solver_resource_fails_hash_validation(packages, part):
    (packages / f"{part}.min.js").write_text("modified resource", encoding="utf-8")
    assert runtime._check_packages()[2] == "solver_integrity_mismatch"


def test_missing_solver_resource_fails_closed(packages):
    (packages / "lib.min.js").unlink()
    assert runtime._check_packages()[2] == "dependencies_incomplete"


def test_local_runtime_resolves_only_beside_project_interpreter(monkeypatch, tmp_path):
    environment = tmp_path / "venv" / "Scripts"
    environment.mkdir(parents=True)
    interpreter = environment / "python.exe"
    interpreter.write_bytes(b"not executed")
    attacker = tmp_path / "cwd"
    attacker.mkdir()
    (attacker / "deno.exe").write_bytes(b"never execute")
    monkeypatch.chdir(attacker)
    monkeypatch.setenv("PATH", str(attacker))
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "executable", str(interpreter))
    assert runtime._runtime_path() == environment / "deno.exe"


def test_frozen_runtime_ignores_interpreter_directory_cwd_and_path(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(tmp_path / "elsewhere" / "MusicVault.exe"))
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted"))
    assert runtime._runtime_path() == bundle / "acquisition" / "deno.exe"


def test_missing_local_runtime_never_falls_back_to_path(packages, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_runtime_path", lambda: tmp_path / "missing" / "deno.exe")
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "deno.exe").write_bytes(b"must not run")

    def prohibited(*args, **kwargs):
        pytest.fail("An absent trusted runtime must not trigger process execution")

    monkeypatch.setattr(runtime.subprocess, "run", prohibited)
    readiness = runtime.acquisition_readiness()
    assert not readiness.ready
    assert readiness.error_code == "runtime_missing"


def test_windows_runtime_hash_is_checked_before_any_execution(executable, monkeypatch):
    executable.write_bytes(b"untrusted replacement")

    def prohibited(*args, **kwargs):
        pytest.fail("An untrusted executable must not be launched, even for --version")

    monkeypatch.setattr(runtime.subprocess, "run", prohibited)
    assert runtime._probe_runtime(str(executable), 0, 0) == (None, "runtime_integrity_mismatch")


def test_runtime_probe_is_bounded_noninteractive_and_shell_free(executable, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="deno 2.9.5\nv8 synthetic\n")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime._probe_runtime(str(executable), 0, 0) == ("2.9.5", None)
    command, options = calls[0]
    assert command == [str(executable), "--version"]
    assert options["shell"] is False
    assert options["timeout"] == 5
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == options["stderr"] == subprocess.PIPE
    assert options["check"] is False
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (1, "deno 2.9.5\n", (None, "runtime_probe_failed")),
        (0, "not deno", (None, "runtime_probe_failed")),
        (0, "deno 2.9.5-injected\n", (None, "runtime_probe_failed")),
        (0, "deno 2.9.4\n", ("2.9.4", "runtime_version_mismatch")),
    ],
)
def test_runtime_probe_rejects_failed_or_unexpected_version(executable, monkeypatch, returncode, stdout, expected):
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    assert runtime._probe_runtime(str(executable), 0, 0) == expected


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (subprocess.TimeoutExpired("synthetic-private-command", 5), "runtime_probe_timeout"),
        (OSError("synthetic-private-path"), "runtime_probe_failed"),
        (ValueError("synthetic-private-value"), "runtime_probe_failed"),
    ],
)
def test_runtime_probe_exception_details_do_not_escape(executable, monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runtime.subprocess, "run", fail)
    assert runtime._probe_runtime(str(executable), 0, 0) == (None, expected)


def test_display_cache_does_not_authorize_same_stat_runtime_replacement(packages, executable):
    assert runtime.acquisition_readiness().ready
    stat = executable.stat()
    executable.write_bytes(b"x" * stat.st_size)
    os.utime(executable, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    # UI caching is acceptable, but actual acquisition must rehash the file.
    assert runtime.acquisition_readiness().ready
    checked = runtime.acquisition_readiness(verify_integrity=True)
    assert not checked.ready
    assert checked.error_code == "runtime_integrity_mismatch"
    with pytest.raises(runtime.AcquisitionRuntimeError, match="runtime_integrity_mismatch"):
        runtime.acquisition_ydl_options()


def test_actual_acquisition_rehashes_solver_resources_despite_ui_cache(packages, executable):
    assert runtime.acquisition_readiness().ready
    (packages / "lib.min.js").write_text("substituted solver", encoding="utf-8")
    assert runtime.acquisition_readiness().ready
    assert runtime.acquisition_readiness(verify_integrity=True).error_code == "solver_integrity_mismatch"
    with pytest.raises(runtime.AcquisitionRuntimeError, match="solver_integrity_mismatch"):
        runtime.acquisition_ydl_options()


def test_options_require_fresh_integrity_and_disable_remote_code_and_credentials(monkeypatch, tmp_path):
    checks = []
    path = tmp_path / "trusted" / "deno.exe"

    def readiness(*, verify_integrity=False):
        checks.append(verify_integrity)
        return runtime.AcquisitionReadiness(True, "2026.8.19", "0.8.0", "2.9.5", path)

    monkeypatch.setattr(runtime, "acquisition_readiness", readiness)
    options = runtime.acquisition_ydl_options()
    assert checks == [True]
    assert options == {
        "js_runtimes": {"deno": {"path": str(path)}},
        "remote_components": set(),
        "cachedir": False,
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "usenetrc": False,
        "noplaylist": True,
    }


def test_public_summary_never_exposes_runtime_path_or_claims_network_success(monkeypatch):
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    readiness = runtime.AcquisitionReadiness(
        True, "2026.8.19", "0.8.0", "2.9.5", Path("synthetic-private-folder/deno.exe"),
    )
    summary = readiness.public_summary()
    assert summary["runtime_source"] == "project_environment"
    assert summary["authentication"] == "anonymous"
    assert summary["remote_components_enabled"] is False
    assert summary["network_verified"] is False
    assert "synthetic-private-folder" not in json.dumps(summary)
    assert "runtime_path" not in summary
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    assert readiness.public_summary()["runtime_source"] == "bundled"


@pytest.mark.parametrize("untrusted", ["synthetic-secret", "file:///private", "2.9.5\nprivate", "2026.8.19+private"])
def test_public_summary_rejects_identifying_version_or_error_strings(untrusted):
    readiness = runtime.AcquisitionReadiness(False, untrusted, untrusted, untrusted, error_code=untrusted)
    summary = readiness.public_summary()
    assert summary["extractor_version"] is None
    assert summary["solver_version"] is None
    assert summary["runtime_version"] is None
    assert summary["error_code"] == "dependencies_incomplete"
    assert untrusted not in json.dumps(summary)


def test_spec_collects_reviewed_runtime_solver_resources_and_license_metadata():
    source = (Path(__file__).resolve().parents[1] / "MusicVault.spec").read_text(encoding="utf-8")
    assert "binaries=[(str(acquisition.runtime_path), 'acquisition')]" in source
    assert "collect_data_files('yt_dlp_ejs', includes=['**/*.js'])" in source
    assert "collect_submodules('yt_dlp_ejs')" in source
    assert "for dependency in ACQUISITION_PINS:" in source
    assert "copy_metadata(dependency)" in source
    assert "upx_exclude=['deno.exe']" in source
