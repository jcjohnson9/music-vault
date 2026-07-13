"""Non-admin Windows desktop-shortcut support with conflict protection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from music_vault.version import APP_NAME, DISPLAY_VERSION


_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ExecutablePath = [string]$env:MUSIC_VAULT_SHORTCUT_EXECUTABLE
$WorkingDirectory = [string]$env:MUSIC_VAULT_SHORTCUT_WORKING_DIRECTORY
$DesktopDirectory = [string]$env:MUSIC_VAULT_SHORTCUT_DESKTOP
$IconPath = [string]$env:MUSIC_VAULT_SHORTCUT_ICON
$Description = [string]$env:MUSIC_VAULT_SHORTCUT_DESCRIPTION
$ReplaceDifferentTarget = [string]$env:MUSIC_VAULT_SHORTCUT_REPLACE_DIFFERENT
if ([string]::IsNullOrWhiteSpace($DesktopDirectory)) {
    $DesktopDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
}
[System.IO.Directory]::CreateDirectory($DesktopDirectory) | Out-Null
$shortcutPath = Join-Path $DesktopDirectory 'Music Vault.lnk'
$shell = New-Object -ComObject WScript.Shell
$existed = Test-Path -LiteralPath $shortcutPath -PathType Leaf
$status = 'created'
if ($existed) {
    $current = $shell.CreateShortcut($shortcutPath)
    $currentTarget = [string]$current.TargetPath
    $sameTarget = -not [string]::IsNullOrWhiteSpace($currentTarget) -and [string]::Equals(
            [System.IO.Path]::GetFullPath($currentTarget),
            [System.IO.Path]::GetFullPath($ExecutablePath),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    if (-not $sameTarget -and $ReplaceDifferentTarget -ne 'true') {
        [pscustomobject]@{
            status = 'conflict'
            shortcut_path = $shortcutPath
            target_path = $ExecutablePath
            working_directory = $WorkingDirectory
            icon_path = $IconPath
        } | ConvertTo-Json -Compress
        exit 0
    }
    $status = 'updated'
    $expectedIcon = if ([string]::IsNullOrWhiteSpace($IconPath)) { "$ExecutablePath,0" } else { "$IconPath,0" }
    if (
        $sameTarget -and
        [string]::Equals([string]$current.WorkingDirectory, $WorkingDirectory, [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$current.IconLocation, $expectedIcon, [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$current.Description, $Description, [System.StringComparison]::Ordinal)
    ) {
        $status = 'unchanged'
    }
}
if ($status -ne 'unchanged') {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $ExecutablePath
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = $Description
    if ([string]::IsNullOrWhiteSpace($IconPath)) {
        $shortcut.IconLocation = "$ExecutablePath,0"
    } else {
        $shortcut.IconLocation = "$IconPath,0"
    }
    $shortcut.Save()
}
[pscustomobject]@{
    status = $status
    shortcut_path = $shortcutPath
    target_path = $ExecutablePath
    working_directory = $WorkingDirectory
    icon_path = $(if ([string]::IsNullOrWhiteSpace($IconPath)) { $ExecutablePath } else { $IconPath })
} | ConvertTo-Json -Compress
"""


@dataclass(frozen=True)
class ShortcutResult:
    status: str
    shortcut_path: Path | None
    target_path: Path
    working_directory: Path
    icon_path: Path | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {"created", "updated", "unchanged"}


def _powershell_executable() -> str | None:
    system_root = os.environ.get("SystemRoot", "")
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe") or shutil.which("powershell")


def create_or_update_desktop_shortcut(
    executable_path: str | Path,
    portable_root: str | Path,
    *,
    desktop_dir: str | Path | None = None,
    icon_path: str | Path | None = None,
    replace_existing_different_target: bool = False,
    runner: Callable[..., object] = subprocess.run,
    powershell_executable: str | Path | None = None,
) -> ShortcutResult:
    """Create/update ``Music Vault.lnk`` without silently retargeting another copy."""
    executable = Path(executable_path).expanduser().resolve()
    working_directory = Path(portable_root).expanduser().resolve()
    selected_icon = Path(icon_path).expanduser().resolve() if icon_path else None
    expected_shortcut = (
        Path(desktop_dir).expanduser().resolve() / f"{APP_NAME}.lnk"
        if desktop_dir is not None
        else None
    )
    if not executable.is_file():
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "MusicVault.exe was not found.",
        )
    if not working_directory.is_dir():
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "The portable application folder was not found.",
        )
    if selected_icon is not None and not selected_icon.is_file():
        selected_icon = None

    powershell = str(powershell_executable or _powershell_executable() or "")
    if not powershell:
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "Windows PowerShell is unavailable for shortcut creation.",
        )
    arguments = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _SHORTCUT_SCRIPT,
    ]
    environment = dict(os.environ)
    environment.update(
        {
            "MUSIC_VAULT_SHORTCUT_EXECUTABLE": str(executable),
            "MUSIC_VAULT_SHORTCUT_WORKING_DIRECTORY": str(working_directory),
            "MUSIC_VAULT_SHORTCUT_DESKTOP": (
                str(Path(desktop_dir).expanduser().resolve())
                if desktop_dir is not None
                else ""
            ),
            "MUSIC_VAULT_SHORTCUT_ICON": str(selected_icon) if selected_icon is not None else "",
            "MUSIC_VAULT_SHORTCUT_DESCRIPTION": f"{APP_NAME} {DISPLAY_VERSION}",
            "MUSIC_VAULT_SHORTCUT_REPLACE_DIFFERENT": (
                "true" if replace_existing_different_target else "false"
            ),
        }
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = runner(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
            creationflags=creationflags,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "Desktop shortcut creation timed out.",
        )
    except (OSError, ValueError):
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "Desktop shortcut creation could not start.",
        )
    if int(getattr(completed, "returncode", 1)) != 0:
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "Desktop shortcut creation failed.",
        )
    try:
        output_lines = [line for line in str(getattr(completed, "stdout", "")).splitlines() if line.strip()]
        payload = json.loads(output_lines[-1])
        status = str(payload["status"])
        if status not in {"created", "updated", "unchanged", "conflict"}:
            raise ValueError
        shortcut_path = Path(payload["shortcut_path"]).resolve()
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ShortcutResult(
            "failed",
            expected_shortcut,
            executable,
            working_directory,
            selected_icon,
            "Desktop shortcut creation returned an invalid result.",
        )
    return ShortcutResult(
        status,
        shortcut_path,
        executable,
        working_directory,
        selected_icon or executable,
        "An existing Music Vault shortcut points to another application location."
        if status == "conflict"
        else None,
    )
