from __future__ import annotations

"""Acceptance-only outbound-network denial for controlled startup gates.

The guard is inert during ordinary Music Vault use.  It activates only when
both acceptance environment switches are set, writes aggregate evidence below
the operating-system temporary directory, and never records a host, address,
URL, request, credential, or stack trace.
"""

import atexit
import json
import os
import socket
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from .runtime_policy import NO_NETWORK_ENVIRONMENT, NO_SECRETS_ENVIRONMENT


NETWORK_REPORT_ENVIRONMENT = "MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT"
NETWORK_REPORT_SCHEMA_VERSION = 2
_ACCEPTANCE_PREFIXES = (
    "MusicVault_Batch10_3_",
    "MusicVault_Batch10_4_",
    "MusicVault_Batch10_5_",
    "MusicVault_Batch10_6_",
)
_ACTIVE_GUARD_LOCK = threading.Lock()
_ACTIVE_GUARD: "AcceptanceNetworkGuard | None" = None


class AcceptanceNetworkBlocked(OSError):
    """Raised instead of attempting outbound network access."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _validated_report_path(value: object) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError("Acceptance network report path is required.")
    path = Path(raw).expanduser().resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if not _is_within(path, temporary) or path == temporary or path.exists() and path.is_dir():
        raise RuntimeError("Acceptance network report must be a file below TEMP.")
    relative = path.relative_to(temporary)
    if not any(
        part.startswith(prefix)
        for part in relative.parts
        for prefix in _ACCEPTANCE_PREFIXES
    ):
        raise RuntimeError("Acceptance network report lacks the controlled prefix.")
    return path


class AcceptanceNetworkGuard:
    """Process-local, fail-closed network guard with aggregate-only evidence."""

    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path
        self._attempt_count = 0
        self._provider_factory_invocation_count = 0
        self._provider_task_dispatch_count = 0
        self._lock = threading.Lock()
        self._installed = False
        self._originals: list[tuple[object, str, Callable[..., object]]] = []

    @property
    def attempt_count(self) -> int:
        with self._lock:
            return self._attempt_count

    @property
    def provider_factory_invocation_count(self) -> int:
        with self._lock:
            return self._provider_factory_invocation_count

    @property
    def provider_task_dispatch_count(self) -> int:
        with self._lock:
            return self._provider_task_dispatch_count

    def _write_report(self, *, finalized: bool) -> None:
        payload = {
            "schema_version": NETWORK_REPORT_SCHEMA_VERSION,
            "guard_installed": self._installed,
            "outbound_blocked": True,
            "attempt_count": self._attempt_count,
            "provider_factory_invocation_count": self._provider_factory_invocation_count,
            "provider_task_dispatch_count": self._provider_task_dispatch_count,
            "finalized": bool(finalized),
            "request_details_recorded": False,
            "credential_contents_read": False,
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(self.report_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.report_path)

    def _blocked(self, *_args, **_kwargs):
        with self._lock:
            self._attempt_count += 1
            self._write_report(finalized=False)
        raise AcceptanceNetworkBlocked("Outbound network is disabled for acceptance startup.")

    def record_provider_factory_invocation(self) -> None:
        """Record one actual optional-provider construction attempt."""

        with self._lock:
            self._provider_factory_invocation_count += 1
            self._write_report(finalized=False)

    def record_provider_task_dispatch(self) -> None:
        """Record one provider-capable background task dispatch."""

        with self._lock:
            self._provider_task_dispatch_count += 1
            self._write_report(finalized=False)

    def _patch(self, owner: object, name: str) -> None:
        original = getattr(owner, name, None)
        if original is None or not callable(original):
            return
        self._originals.append((owner, name, original))
        setattr(owner, name, self._blocked)

    def install(self) -> "AcceptanceNetworkGuard":
        if self._installed:
            return self
        global _ACTIVE_GUARD
        with _ACTIVE_GUARD_LOCK:
            if _ACTIVE_GUARD is not None and _ACTIVE_GUARD is not self:
                raise RuntimeError("An acceptance network guard is already active.")
            _ACTIVE_GUARD = self
        self._installed = True
        for owner, name in (
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket, "gethostbyname"),
            (socket, "gethostbyname_ex"),
            (socket, "gethostbyaddr"),
            (socket, "getnameinfo"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket.socket, "send"),
            (socket.socket, "sendall"),
            (socket.socket, "sendto"),
            (socket.socket, "sendmsg"),
            (urllib.request, "urlopen"),
        ):
            self._patch(owner, name)
        self._write_report(finalized=False)
        atexit.register(self.finalize)
        return self

    def finalize(self) -> None:
        with self._lock:
            self._write_report(finalized=True)

    def restore(self) -> None:
        """Restore patched callables after a bounded in-process proof."""

        atexit.unregister(self.finalize)
        while self._originals:
            owner, name, original = self._originals.pop()
            setattr(owner, name, original)
        self._installed = False
        global _ACTIVE_GUARD
        with _ACTIVE_GUARD_LOCK:
            if _ACTIVE_GUARD is self:
                _ACTIVE_GUARD = None

    def restore_for_test(self) -> None:
        """Compatibility alias for isolated unit tests."""

        self.restore()


def install_acceptance_network_guard() -> AcceptanceNetworkGuard | None:
    """Install the guard when explicitly requested by a controlled gate."""

    if os.environ.get(NO_NETWORK_ENVIRONMENT, "").strip() != "1":
        return None
    if os.environ.get(NO_SECRETS_ENVIRONMENT, "").strip() != "1":
        raise RuntimeError("Acceptance network guard also requires no-secret mode.")
    report = _validated_report_path(os.environ.get(NETWORK_REPORT_ENVIRONMENT))
    return AcceptanceNetworkGuard(report).install()


def record_provider_factory_invocation() -> None:
    """Record an aggregate acceptance event; remain inert in normal use."""

    with _ACTIVE_GUARD_LOCK:
        guard = _ACTIVE_GUARD
    if guard is not None:
        guard.record_provider_factory_invocation()


def record_provider_task_dispatch() -> None:
    """Record an aggregate acceptance event; remain inert in normal use."""

    with _ACTIVE_GUARD_LOCK:
        guard = _ACTIVE_GUARD
    if guard is not None:
        guard.record_provider_task_dispatch()


__all__ = [
    "AcceptanceNetworkBlocked",
    "AcceptanceNetworkGuard",
    "NETWORK_REPORT_ENVIRONMENT",
    "NETWORK_REPORT_SCHEMA_VERSION",
    "NO_NETWORK_ENVIRONMENT",
    "NO_SECRETS_ENVIRONMENT",
    "install_acceptance_network_guard",
    "record_provider_factory_invocation",
    "record_provider_task_dispatch",
]
