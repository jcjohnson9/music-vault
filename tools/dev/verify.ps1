$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

Set-Location $projectRoot

& $python -B .\tools\verify_music_vault.py
if ($LASTEXITCODE -ne 0) { throw "Music Vault verification failed." }

& $python -B -m py_compile .\music_vault\app.py
if ($LASTEXITCODE -ne 0) { throw "Music Vault syntax check failed." }

& $python -B -c 'import music_vault.app as app; print(app.__file__)'
if ($LASTEXITCODE -ne 0) { throw "Music Vault import check failed." }
