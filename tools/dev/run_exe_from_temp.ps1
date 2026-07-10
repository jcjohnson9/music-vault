$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$exe = Join-Path $projectRoot "dist\MusicVault\MusicVault.exe"

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Built EXE not found: $exe"
}

Start-Process -FilePath $exe -WorkingDirectory $env:TEMP
Write-Output "Launched Music Vault from TEMP. Runtime data should resolve under: $projectRoot\data"
