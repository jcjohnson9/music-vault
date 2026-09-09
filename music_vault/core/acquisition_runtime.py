"""Network-free acquisition capability checks, independent of desktop PATH.

No credentials, provider calls, runtime installs or remote solver downloads.
The EXE carries a reviewed Deno binary and matching EJS resources. Local
playback remains usable when this optional acquisition capability is missing.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


ACQUISITION_PINS = {
    "yt-dlp": "2026.8.19",
    "yt-dlp-ejs": "0.8.0",
    "deno": "2.9.5",
    "brotli": "1.2.0",
    "pycryptodomex": "3.23.0",
    "websockets": "17.1",
}
DENO_WINDOWS_SHA256 = "98f8c2a2d470e4ccb04c935c86ff8050817d877762aec5eaeeb9e409ccb3b9fd"
_ERROR_CODES = frozenset({
    "runtime_integrity_mismatch", "runtime_probe_failed", "runtime_probe_timeout",
    "runtime_version_mismatch", "dependency_version_mismatch", "solver_version_mismatch",
    "solver_integrity_mismatch", "dependencies_incomplete", "runtime_missing",
})


def _public_version(value: str | None) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"\d{1,4}(?:\.\d{1,4}){1,3}", value) else None


class AcquisitionRuntimeError(RuntimeError):
    """A safe capability failure, with no underlying paths or exception text."""


@dataclass(frozen=True)
class AcquisitionReadiness:
    ready: bool
    extractor_version: str | None = None
    ejs_version: str | None = None
    runtime_version: str | None = None
    runtime_path: Path | None = None
    error_code: str | None = None

    def public_summary(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "extractor": "yt-dlp",
            "extractor_version": _public_version(self.extractor_version),
            "solver": "yt-dlp-ejs",
            "solver_version": _public_version(self.ejs_version),
            "runtime": "deno",
            "runtime_version": _public_version(self.runtime_version),
            "runtime_source": "bundled" if getattr(sys, "frozen", False) else "project_environment",
            "error_code": self.error_code if self.error_code in _ERROR_CODES else (None if self.error_code is None else "dependencies_incomplete"),
            "authentication": "anonymous",
            "remote_components_enabled": False,
            "network_verified": False,
        }


def _runtime_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "acquisition" / "deno.exe"
    # Only this interpreter's environment. Do not search cwd, PATH, user
    # directories or user-controlled downloader configuration for executables.
    return Path(sys.executable).resolve().parent / ("deno.exe" if sys.platform == "win32" else "deno")


@lru_cache(maxsize=4)
def _probe_runtime(path: str, size: int, modified_ns: int) -> tuple[str | None, str | None]:
    del size, modified_ns  # cache invalidation keys, not claims of integrity
    try:
        if sys.platform == "win32":
            with Path(path).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != DENO_WINDOWS_SHA256:
                return None, "runtime_integrity_mismatch"
        result = subprocess.run(
            [path, "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=5,
            check=False, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        match = re.match(r"deno (\d+\.\d+\.\d+)(?:\s|$)", result.stdout)
        if result.returncode or not match:
            return None, "runtime_probe_failed"
        version = match[1]
        if version != ACQUISITION_PINS["deno"]:
            return version, "runtime_version_mismatch"
        return version, None
    except subprocess.TimeoutExpired:
        return None, "runtime_probe_timeout"
    except (OSError, ValueError):
        return None, "runtime_probe_failed"


@lru_cache(maxsize=1)
def _check_packages() -> tuple[str | None, str | None, str | None]:
    extractor = solver = None
    try:
        extractor = importlib.metadata.version("yt-dlp")
        solver = importlib.metadata.version("yt-dlp-ejs")
        for name, expected in ACQUISITION_PINS.items():
            if importlib.metadata.version(name) != expected:
                return extractor, solver, "dependency_version_mismatch"
        from yt_dlp.extractor.youtube.jsc._builtin.vendor import HASHES, VERSION

        if solver != VERSION:
            return extractor, solver, "solver_version_mismatch"
        resources = importlib.resources.files("yt_dlp_ejs.yt.solver")
        for part in ("core", "lib"):
            code = resources.joinpath(f"{part}.min.js").read_text(encoding="utf-8")
            if hashlib.sha3_512(code.encode()).hexdigest() != HASHES[f"yt.solver.{part}.min.js"]:
                return extractor, solver, "solver_integrity_mismatch"
        return extractor, solver, None
    except (ImportError, importlib.metadata.PackageNotFoundError, OSError, KeyError, ValueError):
        return extractor, solver, "dependencies_incomplete"


def acquisition_readiness(*, verify_integrity: bool = False) -> AcquisitionReadiness:
    # Reuse UI probes, but never treat size/mtime or a previous display check
    # as integrity evidence when starting an actual media acquisition.
    package_check = _check_packages.__wrapped__ if verify_integrity else _check_packages
    extractor, solver, error = package_check()
    path = _runtime_path()
    if error:
        return AcquisitionReadiness(False, extractor, solver, error_code=error)
    try:
        stat = path.stat()
        if not path.is_file():
            raise OSError("Not a runtime file")
    except OSError:
        return AcquisitionReadiness(False, extractor, solver, error_code="runtime_missing")
    probe = _probe_runtime.__wrapped__ if verify_integrity else _probe_runtime
    version, error = probe(str(path), stat.st_size, stat.st_mtime_ns)
    return AcquisitionReadiness(not error, extractor, solver, version, path, error)


def acquisition_ydl_options() -> dict[str, object]:
    capability = acquisition_readiness(verify_integrity=True)
    if not capability.ready:
        raise AcquisitionRuntimeError(
            "Acquisition components are not ready ("
            + str(capability.public_summary()["error_code"])
            + "). Repair the application environment; local playback is unaffected."
        )
    return {
        "js_runtimes": {"deno": {"path": str(capability.runtime_path)}},
        "remote_components": set(),
        "cachedir": False,
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "usenetrc": False,
        "noplaylist": True,
    }
