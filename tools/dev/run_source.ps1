$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

Set-Location $projectRoot
& $python .\run.py
if ($LASTEXITCODE -ne 0) { throw "Music Vault source app exited with code $LASTEXITCODE." }
