[CmdletBinding()]
param([string]$Output)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_3_source_migration_proof.py"

foreach ($RequiredFile in @($Python, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        Write-Output '{"error_code":"batch10_3_source_proof_input_unavailable","ok":false}'
        exit 2
    }
}

$env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
$Arguments = @("-B", $Tool)
if ($Output) { $Arguments += @("--output", $Output) }
Set-Location -LiteralPath $ProjectRoot
& $Python @Arguments
exit $LASTEXITCODE
