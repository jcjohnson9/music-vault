$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$scanner = Join-Path $projectRoot "tools\security\pre_public_history_check.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

Set-Location $projectRoot
& $python -B $scanner @args
$scannerExitCode = $LASTEXITCODE
exit $scannerExitCode
