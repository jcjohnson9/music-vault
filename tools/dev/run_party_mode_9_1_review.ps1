param(
    [ValidateSet(1.0, 1.25, 1.5)]
    [double]$Scale = 1.0,
    [Alias("Output")]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found: $python"
}

$arguments = @("-B", ".\tools\dev\run_party_mode_9_1_review.py")
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $arguments += @("--output", $OutputPath)
}
$arguments += @(
    "--scale",
    $Scale.ToString([Globalization.CultureInfo]::InvariantCulture)
)

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Batch 9.1 Party Mode review failed with exit code $LASTEXITCODE."
}
