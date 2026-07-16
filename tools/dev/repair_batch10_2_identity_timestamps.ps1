[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("compare", "clone-proof", "repair")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$TargetDatabase,

    [Parameter(Mandatory = $true)]
    [string]$ReferenceBackup,

    [Parameter(Mandatory = $true)]
    [string]$ReferenceSha256,

    [Parameter(Mandatory = $true)]
    [int]$ExpectedIdentityCount,

    [Parameter(Mandatory = $true)]
    [int]$ExpectedRepairCount,

    [string]$BackupDirectory,
    [string]$Output,
    [switch]$AcknowledgeLiveRepair
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\repair_batch10_2_identity_timestamps.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Output '{"error_code":"project_python_unavailable","ok":false}'
    exit 2
}

$Arguments = @(
    "-B",
    $Tool,
    $Mode,
    "--target-database", $TargetDatabase,
    "--reference-backup", $ReferenceBackup,
    "--reference-sha256", $ReferenceSha256,
    "--expected-identity-count", $ExpectedIdentityCount,
    "--expected-repair-count", $ExpectedRepairCount
)
if ($Output) { $Arguments += @("--output", $Output) }

if ($Mode -eq "repair") {
    if (-not $AcknowledgeLiveRepair) {
        Write-Output '{"error_code":"live_acknowledgement_missing","ok":false}'
        exit 2
    }
    if (-not $BackupDirectory) {
        Write-Output '{"error_code":"backup_directory_missing","ok":false}'
        exit 2
    }
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $Arguments += @(
        "--backup-directory", $BackupDirectory,
        "--acknowledge-live-repair", "batch10.2-live-identity-timestamp-repair"
    )
}

Set-Location $ProjectRoot
& $Python @Arguments
exit $LASTEXITCODE
