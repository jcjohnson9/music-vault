$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $projectRoot "dist\MusicVault\MusicVault.exe"
$icon = Join-Path $projectRoot "assets\icons\music_vault.ico"
$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
$shortcutPath = Join-Path $desktop "Music Vault.lnk"

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Built EXE not found: $exe"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "Music Vault v1.0.0 Release Candidate"

if (Test-Path -LiteralPath $icon -PathType Leaf) {
    $shortcut.IconLocation = "$icon,0"
}

$shortcut.Save()
Write-Output "Desktop shortcut created: $shortcutPath"
