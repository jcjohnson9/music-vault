[CmdletBinding()]
param(
    [ValidateSet("DryRun", "Apply")]
    [string]$Mode = "DryRun",
    [string]$AcknowledgeTargetedLookup = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\batch10_6_acceptance.py"
$Database = Join-Path $ProjectRoot "data\music_vault.sqlite3"
$CacheRoot = Join-Path $ProjectRoot "data\artist_images"
$RequiredAcknowledgement = "batch10.6-live-one-track-orientation-repair"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "The project-local Python interpreter is unavailable."
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw "The Music Vault database is unavailable."
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before Batch 10.6 acceptance."
}
if ($Mode -eq "Apply" -and $AcknowledgeTargetedLookup -ne $RequiredAcknowledgement) {
    throw "The exact Batch 10.6 live-repair acknowledgement is required."
}

$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousNoNetwork = $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK
try {
    Set-Location $ProjectRoot
    if ($Mode -eq "DryRun") {
        $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
        $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    }
    else {
        # The explicitly acknowledged one-target lookup is the only mode in
        # which this wrapper permits the normal private token store/network.
        Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS -ErrorAction SilentlyContinue
        Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK -ErrorAction SilentlyContinue
    }
    $Arguments = @(
        "-B", $Tool,
        $(if ($Mode -eq "Apply") { "apply-live" } else { "dry-run" }),
        "--project-root", $ProjectRoot,
        "--database", $Database,
        "--cache-root", $CacheRoot
    )
    if ($Mode -eq "Apply") {
        $Arguments += @(
            "--acknowledge-targeted-lookup",
            $AcknowledgeTargetedLookup
        )
    }
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Batch 10.6 acceptance failed. No automatic restore was attempted."
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
