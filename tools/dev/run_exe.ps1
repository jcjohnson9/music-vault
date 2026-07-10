$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $projectRoot "dist\MusicVault\MusicVault.exe"

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Built EXE not found: $exe"
}

Start-Process -FilePath $exe -WorkingDirectory $projectRoot
Write-Output "Launched Music Vault with project root as working directory."
