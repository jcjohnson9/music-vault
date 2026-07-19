[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("baseline", "create-backup", "clone-dry-run", "launch-preflight", "verify")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [switch]$AcknowledgeLiveLibrary,

    [string]$DataDirectory,
    [string]$Database,
    [string]$Baseline,
    [string]$DryRun,
    [string]$Backup,
    [string]$NetworkReport,
    [string]$Output
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\verify_batch10_3_live_migration.py"

foreach ($RequiredFile in @($Python, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        Write-Output '{"error_code":"batch10_3_live_gate_input_unavailable","ok":false}'
        exit 2
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    Write-Output '{"error_code":"music_vault_process_running","ok":false}'
    exit 2
}

$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
$Arguments = @(
    "-B",
    $Tool,
    $Mode,
    "--acknowledge-live-library",
    "batch10.3-live-schema-6-to-7",
    "--project-root",
    $ProjectRoot
)
if ($DataDirectory) { $Arguments += @("--data-dir", $DataDirectory) }
if ($Database) { $Arguments += @("--database", $Database) }
if ($Baseline) { $Arguments += @("--baseline", $Baseline) }
if ($DryRun) { $Arguments += @("--dry-run", $DryRun) }
if ($Backup) { $Arguments += @("--backup", $Backup) }
if ($NetworkReport) { $Arguments += @("--network-report", $NetworkReport) }
if ($Output) { $Arguments += @("--output", $Output) }

Set-Location -LiteralPath $ProjectRoot
& $Python @Arguments
exit $LASTEXITCODE
