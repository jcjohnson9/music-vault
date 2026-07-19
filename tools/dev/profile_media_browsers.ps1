$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual-environment Python was not found."
}

Set-Location $ProjectRoot
$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
$env:QT_QPA_PLATFORM = "offscreen"
& $Python -B (Join-Path $ScriptDir "profile_media_browsers.py") @args
if ($LASTEXITCODE -ne 0) {
    throw "Media-browser profiling failed with exit code $LASTEXITCODE."
}
