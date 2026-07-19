[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\batch10_4_acceptance.py"
$CacheRoot = Join-Path $ProjectRoot "data\artist_images"

foreach ($RequiredFile in @($Python, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required Batch 10.4 cache-audit input is unavailable."
    }
}
if (-not (Test-Path -LiteralPath $CacheRoot -PathType Container)) {
    throw "The private artist-image cache is unavailable."
}

Set-Location -LiteralPath $ProjectRoot
& $Python -B $Tool audit-cache --cache-root $CacheRoot `
    --expected-file-count 226 --expected-total-bytes 30791281
if ($LASTEXITCODE -ne 0) {
    throw "Batch 10.4 aggregate artist-cache audit failed."
}
