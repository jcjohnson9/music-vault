param(
    [switch]$ReplaceDifferentTarget
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $projectRoot "dist\MusicVault\MusicVault.exe"
$icon = Join-Path $projectRoot "assets\icons\music_vault.ico"
$desktop = if ($env:MUSIC_VAULT_DESKTOP_DIR) {
    [IO.Path]::GetFullPath($env:MUSIC_VAULT_DESKTOP_DIR)
} else {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
}
$shortcutPath = Join-Path $desktop "Music Vault.lnk"

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Built EXE not found: $exe"
}

$shell = New-Object -ComObject WScript.Shell
$existing = Test-Path -LiteralPath $shortcutPath -PathType Leaf
if ($existing) {
    $current = $shell.CreateShortcut($shortcutPath)
    $sameTarget = -not [string]::IsNullOrWhiteSpace($current.TargetPath) -and
        [string]::Equals(
            [IO.Path]::GetFullPath($current.TargetPath),
            [IO.Path]::GetFullPath($exe),
            [StringComparison]::OrdinalIgnoreCase
        )
    if (-not $sameTarget -and -not $ReplaceDifferentTarget) {
        throw "The existing Music Vault shortcut points elsewhere. Re-run with -ReplaceDifferentTarget only after confirming the replacement."
    }
}

[IO.Directory]::CreateDirectory($desktop) | Out-Null
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "Music Vault v1.0.0"

if (Test-Path -LiteralPath $icon -PathType Leaf) {
    $shortcut.IconLocation = "$icon,0"
}

$shortcut.Save()
Write-Output "Desktop shortcut created or updated: $shortcutPath"
