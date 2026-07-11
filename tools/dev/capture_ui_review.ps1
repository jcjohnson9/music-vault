param(
    [string]$Output,
    [string]$Executable,
    [string[]]$Size,
    [string[]]$Page,
    [ValidateSet("1", "1.25", "1.5")]
    [string]$Scale,
    [switch]$Offscreen
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$captureTool = Join-Path $projectRoot "tools\dev\capture_ui_review.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found: $python"
}

$captureArgs = @("-B", $captureTool)

if ($Output) {
    $captureArgs += @("--output", $Output)
}

if ($Executable) {
    $captureArgs += @("--exe", $Executable)
}

foreach ($item in $Size) {
    $captureArgs += @("--size", $item)
}

foreach ($item in $Page) {
    $captureArgs += @("--page", $item)
}

if ($Scale) {
    $captureArgs += @("--scale", $Scale)
}

if ($Offscreen) {
    $captureArgs += "--offscreen"
}

Push-Location $projectRoot
try {
    & $python @captureArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
