$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

Set-Location $projectRoot
& $python -B .\tools\release\fetch_compliance_sources.py
if ($LASTEXITCODE -ne 0) { throw "Corresponding-source preparation failed." }
& $python -B .\tools\release\build_portable_release.py @args
if ($LASTEXITCODE -ne 0) { throw "Portable release build failed." }
