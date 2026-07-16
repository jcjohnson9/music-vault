$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual-environment Python was not found."
}

Set-Location $ProjectRoot
& $Python -B (Join-Path $ScriptDir "profile_multiple_sources.py") @args
if ($LASTEXITCODE -ne 0) {
    throw "Multiple-source profiling failed with exit code $LASTEXITCODE."
}
