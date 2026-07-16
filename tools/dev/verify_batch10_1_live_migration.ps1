[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("baseline", "create-backup", "verify")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [switch]$AcknowledgeLiveLibrary,

    [string]$DataDirectory,
    [string]$Database,
    [string]$Baseline,
    [string]$Backup,
    [string]$Output
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\verify_batch10_1_live_migration.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Output '{"error_code":"project_python_unavailable","ok":false}'
    exit 2
}

$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
$Arguments = @(
    "-B",
    $Tool,
    $Mode,
    "--acknowledge-live-library",
    "batch10.1-live-schema-5-to-6",
    "--project-root",
    $ProjectRoot
)
if ($DataDirectory) { $Arguments += @("--data-dir", $DataDirectory) }
if ($Database) { $Arguments += @("--database", $Database) }
if ($Baseline) { $Arguments += @("--baseline", $Baseline) }
if ($Backup) { $Arguments += @("--backup", $Backup) }
if ($Output) { $Arguments += @("--output", $Output) }

Set-Location $ProjectRoot
& $Python @Arguments
exit $LASTEXITCODE
