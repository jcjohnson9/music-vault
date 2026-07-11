$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$statusFile = Join-Path $projectRoot "data\music_vault_status.json"

if (Test-Path -LiteralPath $statusFile -PathType Leaf) {
    Get-Content -Raw -LiteralPath $statusFile
} else {
    Write-Output "App Status file has not been generated yet: $statusFile"
}
