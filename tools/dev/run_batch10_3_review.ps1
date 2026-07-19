$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found."
}

Set-Location $ProjectRoot
$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
& $Python -B (Join-Path $ScriptDir "run_batch10_3_review.py") --offscreen @args
if ($LASTEXITCODE -ne 0) {
    throw "Batch 10.3 synthetic UI review failed with exit code $LASTEXITCODE."
}
