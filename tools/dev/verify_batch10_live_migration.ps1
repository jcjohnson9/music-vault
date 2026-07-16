[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("baseline", "create-backup", "verify")]
    [string]$Mode,

    [string]$DataDirectory,
    [string]$Database,
    [string]$Baseline,
    [string]$Backup,
    [string]$Output
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$tool = Join-Path $projectRoot "tools\dev\verify_batch10_live_migration.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Output '{"error_code":"project_python_unavailable","ok":false}'
    exit 2
}

$arguments = @("-B", $tool, $Mode, "--project-root", $projectRoot)
if ($DataDirectory) { $arguments += @("--data-dir", $DataDirectory) }
if ($Database) { $arguments += @("--database", $Database) }
if ($Baseline) { $arguments += @("--baseline", $Baseline) }
if ($Backup) { $arguments += @("--backup", $Backup) }
if ($Output) { $arguments += @("--output", $Output) }

Set-Location $projectRoot
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
