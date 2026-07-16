[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Schema5Backup,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9A-Fa-f]{64}$")]
    [string]$ExpectedSha256,

    [int]$ExpectedTrackCount = 304,
    [int]$ExpectedIdentityCount = 304,
    [int]$ExpectedOldFieldCount = 1824,
    [int]$ExpectedNewFieldCount = 912
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_2_source_migration_proof.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Error "The project-local Python interpreter is unavailable."
}
if (-not (Test-Path -LiteralPath $Schema5Backup -PathType Leaf)) {
    Write-Error "The requested schema-5 backup is unavailable."
}

$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
Set-Location $ProjectRoot
& $Python -B $Tool `
    --schema5-backup $Schema5Backup `
    --expected-sha256 $ExpectedSha256 `
    --expected-track-count $ExpectedTrackCount `
    --expected-identity-count $ExpectedIdentityCount `
    --expected-old-field-count $ExpectedOldFieldCount `
    --expected-new-field-count $ExpectedNewFieldCount
exit $LASTEXITCODE
