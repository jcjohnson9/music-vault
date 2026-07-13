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
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace MusicVaultShortcut
{
    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    [ClassInterface(ClassInterfaceType.None)]
    public class ShellLink { }

    [ComImport]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int size, IntPtr findData, uint flags);
        void GetIDList(out IntPtr itemIdList);
        void SetIDList(IntPtr itemIdList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description, int size);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory, int size);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments, int size);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCommand);
        void SetShowCmd(int showCommand);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder iconPath, int size, out int iconIndex);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string iconPath, int iconIndex);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string relativePath, uint reserved);
        void Resolve(IntPtr windowHandle, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }

    [ComImport]
    [Guid("0000010B-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPersistFile
    {
        [PreserveSig] int GetClassID(out Guid classId);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string fileName, uint mode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string fileName, bool remember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string fileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string fileName);
    }

    public sealed class ShellLinkData
    {
        public string TargetPath { get; set; }
        public string WorkingDirectory { get; set; }
        public string Description { get; set; }
        public string IconPath { get; set; }
        public int IconIndex { get; set; }
    }

    public static class ShellLinkFactory
    {
        public static ShellLinkData Read(string path)
        {
            IShellLinkW link = (IShellLinkW)new ShellLink();
            try
            {
                ((IPersistFile)link).Load(path, 0);
                StringBuilder target = new StringBuilder(32768);
                StringBuilder workingDirectory = new StringBuilder(32768);
                StringBuilder description = new StringBuilder(1024);
                StringBuilder icon = new StringBuilder(32768);
                int iconIndex;
                link.GetPath(target, target.Capacity, IntPtr.Zero, 0);
                link.GetWorkingDirectory(workingDirectory, workingDirectory.Capacity);
                link.GetDescription(description, description.Capacity);
                link.GetIconLocation(icon, icon.Capacity, out iconIndex);
                return new ShellLinkData {
                    TargetPath = target.ToString(),
                    WorkingDirectory = workingDirectory.ToString(),
                    Description = description.ToString(),
                    IconPath = icon.ToString(),
                    IconIndex = iconIndex
                };
            }
            finally
            {
                Marshal.FinalReleaseComObject(link);
            }
        }

        public static void Write(
            string path,
            string target,
            string workingDirectory,
            string description,
            string icon
        )
        {
            IShellLinkW link = (IShellLinkW)new ShellLink();
            try
            {
                link.SetPath(target);
                link.SetWorkingDirectory(workingDirectory);
                link.SetDescription(description);
                link.SetIconLocation(icon, 0);
                link.SetShowCmd(1);
                ((IPersistFile)link).Save(path, true);
            }
            finally
            {
                Marshal.FinalReleaseComObject(link);
            }
        }
    }
}
'@

function Read-MusicVaultShortcut([string]$Path) {
    return [MusicVaultShortcut.ShellLinkFactory]::Read($Path)
}

function Write-MusicVaultShortcut(
    [string]$Path,
    [string]$Target,
    [string]$WorkingDirectory,
    [string]$Description,
    [string]$Icon
) {
    [MusicVaultShortcut.ShellLinkFactory]::Write(
        $Path,
        $Target,
        $WorkingDirectory,
        $Description,
        $Icon
    )
}

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
$existed = Test-Path -LiteralPath $shortcutPath -PathType Leaf
$status = 'created'
if ($existed) {
    $current = Read-MusicVaultShortcut $shortcutPath
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
    $expectedIcon = if ([string]::IsNullOrWhiteSpace($IconPath)) { $ExecutablePath } else { $IconPath }
    if (
        $sameTarget -and
        [string]::Equals([string]$current.WorkingDirectory, $WorkingDirectory, [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$current.IconPath, $expectedIcon, [System.StringComparison]::OrdinalIgnoreCase) -and
        [int]$current.IconIndex -eq 0 -and
        [string]::Equals([string]$current.Description, $Description, [System.StringComparison]::Ordinal)
    ) {
        $status = 'unchanged'
    }
}
if ($status -ne 'unchanged') {
    $selectedIcon = if ([string]::IsNullOrWhiteSpace($IconPath)) { $ExecutablePath } else { $IconPath }
    Write-MusicVaultShortcut $shortcutPath $ExecutablePath $WorkingDirectory $Description $selectedIcon
    $written = Read-MusicVaultShortcut $shortcutPath
    if (
        -not [string]::Equals([string]$written.TargetPath, $ExecutablePath, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([string]$written.WorkingDirectory, $WorkingDirectory, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([string]$written.IconPath, $selectedIcon, [System.StringComparison]::OrdinalIgnoreCase) -or
        [int]$written.IconIndex -ne 0 -or
        -not [string]::Equals([string]$written.Description, $Description, [System.StringComparison]::Ordinal)
    ) {
        throw 'The desktop shortcut did not preserve its configured paths.'
    }
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
