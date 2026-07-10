$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

Set-Location $projectRoot
& $python -B .\tools\security\pre_public_commit_check.py
$scannerExitCode = $LASTEXITCODE
exit $scannerExitCode
