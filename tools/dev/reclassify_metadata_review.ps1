$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $projectRoot

& ".\.venv\Scripts\python.exe" -B ".\tools\dev\reclassify_metadata_review.py" @args
if ($LASTEXITCODE -ne 0) {
    throw "Metadata review reclassification failed with exit code $LASTEXITCODE."
}
