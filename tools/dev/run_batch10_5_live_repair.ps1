[CmdletBinding()]
param(
    [ValidateSet("DryRun", "Apply")]
    [string]$Mode = "DryRun",
    [string]$AcknowledgeLiveRepair = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\batch10_5_acceptance.py"
$Database = Join-Path $ProjectRoot "data\music_vault.sqlite3"
$CacheRoot = Join-Path $ProjectRoot "data\artist_images"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "The project-local Python interpreter is unavailable."
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw "The Music Vault database is unavailable."
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before Batch 10.5 acceptance."
}
if ($Mode -eq "Apply" -and
    $AcknowledgeLiveRepair -ne "batch10.5-live-metadata-acceptance-repair") {
    throw "The exact Batch 10.5 live-repair acknowledgement is required."
}

$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousNoNetwork = $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK
try {
    Set-Location $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $Arguments = @(
        "-B", $Tool,
        $(if ($Mode -eq "Apply") { "apply-live" } else { "dry-run-clone" }),
        "--project-root", $ProjectRoot,
        "--database", $Database,
        "--cache-root", $CacheRoot
    )
    if ($Mode -eq "Apply") {
        $Arguments += @("--acknowledge-live-repair", $AcknowledgeLiveRepair)
    }
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Batch 10.5 acceptance failed. No automatic restore was attempted."
    }
}
finally {
    if ($null -eq $PreviousNoSecrets) {
        Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS -ErrorAction SilentlyContinue
    }
    else {
        $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
    }
    if ($null -eq $PreviousNoNetwork) {
        Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK -ErrorAction SilentlyContinue
    }
    else {
        $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = $PreviousNoNetwork
    }
}
