"""Fail closed if a Python test accesses this checkout's private runtime data.

This is a test-isolation tripwire, not an operating-system sandbox. Native
applications and subprocesses still need their own isolated acceptance roots.
No denied filename or file contents are retained in the diagnostic records.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit


class RuntimeDataAccessBlocked(PermissionError):
    pass


class RuntimeDataGuard:
    def __init__(self, project_root: Path) -> None:
        self.data_root = self._canonical(project_root / "data")
        self.evidence_root = self._canonical(project_root / "data" / "astra_reports")
        self.public_files = {
            self._canonical(project_root / "data" / "README.md"),
            self._canonical(project_root / "data" / ".gitkeep"),
        }
        self.violations: list[dict[str, str]] = []

    @staticmethod
    def _canonical(value: str | bytes | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.realpath(os.fsdecode(value)))

    @staticmethod
    def _under(path: str, root: str) -> bool:
        return path == root or path.startswith(root + os.sep)

    def _check(self, value: object, event: str, *, write: bool) -> None:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        path = self._canonical(value)
        if not self._under(path, self.data_root):
            return
        if self._under(path, self.evidence_root):
            return
        if not write and path in self.public_files:
            return
        self.violations.append({"event": event, "operation": "write" if write else "read"})
        raise RuntimeDataAccessBlocked(
            "Test isolation blocked access to personal project runtime data "
            f"({event}); use a validated synthetic temporary root."
        )

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            write = (
                isinstance(mode, str) and any(character in mode for character in "wax+")
            ) or (
                isinstance(flags, int)
                and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            )
            self._check(args[0], event, write=write)
        elif event == "sqlite3.connect":
            target = args[0]
            if isinstance(target, (str, bytes, os.PathLike)):
                target = os.fsdecode(target)
            if isinstance(target, str) and target.startswith("file:"):
                parsed = urlsplit(target)
                target = unquote(parsed.path)
                if parsed.netloc:
                    target = "//" + parsed.netloc + target
                elif os.name == "nt" and len(target) > 2 and target[0] == "/" and target[2] == ":":
                    target = target[1:]
            # A database connection is always denied, including immutable/mode=ro
            # URIs: private rows must never be read by the unit-test suite.
            self._check(target, event, write=True)
        elif event in {"os.listdir", "os.scandir", "os.chdir"}:
            self._check(args[0], event, write=False)
        elif event in {
            "os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown",
            "os.utime", "os.truncate", "os.startfile", "os.startfile/2",
        }:
            self._check(args[0], event, write=True)
        elif event in {"os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copymode", "shutil.copystat"}:
            self._check(args[0], event, write=event in {"os.rename", "os.link", "os.symlink"})
            self._check(args[1], event, write=True)
