$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

$toolExitCode = 1
Push-Location $projectRoot
try {
    & $python -B .\tools\dev\remediate_library_metadata.py @args
    $toolExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $toolExitCode
