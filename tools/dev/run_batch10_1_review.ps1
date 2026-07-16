$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual-environment Python was not found."
}

Set-Location $ProjectRoot
& $Python -B (Join-Path $ScriptDir "run_batch10_1_review.py") @args
if ($LASTEXITCODE -ne 0) {
    throw "Batch 10.1 synthetic UI review failed with exit code $LASTEXITCODE."
}
