[CmdletBinding()]
param(
    [ValidateSet("offline", "network")]
    [string]$Mode = "offline",
    [switch]$Source,
    [ValidateRange(180, 300)]
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Runtime = Join-Path ([System.IO.Path]::GetTempPath()) ("MusicVault_Acquisition_" + [Guid]::NewGuid().ToString("N"))
$Report = Join-Path $Runtime "acceptance.json"
if ($Source) {
    $Executable = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $Tool = Join-Path $PSScriptRoot "verify_acquisition.py"
    $Arguments = @("-B", ('"' + $Tool + '"'), "--mode", $Mode, "--runtime-root", ('"' + $Runtime + '"'))
}
else {
    $Executable = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
    $Arguments = @("--verify-acquisition", "--mode", $Mode, "--runtime-root", ('"' + $Runtime + '"'))
}
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Required project executable is missing. Build the official EXE or select -Source."
}
if ($Mode -eq "network") {
    Write-Host "One bounded anonymous Blender CC-BY open-film sample; no playlist sync or credentials."
}
$Process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
if (-not $Process.WaitForExit($TimeoutSeconds * 1000)) {
    # This process is a headless, disposable acceptance worker, never the
    # user's GUI. Do not wait forever on an unresponsive remote transport.
    # Terminate only this still-running owned worker's process tree so a
    # child Deno/FFmpeg cannot continue after the acceptance watchdog exits.
    & (Join-Path $env:SystemRoot "System32\taskkill.exe") /PID $Process.Id /T /F *> $null
    throw "Owned acquisition acceptance timed out; its temporary evidence was retained at $Runtime"
}
if (-not (Test-Path -LiteralPath $Report -PathType Leaf)) {
    throw "Acquisition acceptance did not produce evidence. Check that the official EXE is current."
}
$Evidence = Get-Content -LiteralPath $Report -Raw | ConvertFrom-Json
Write-Host (Get-Content -LiteralPath $Report -Raw)
Write-Host "Aggregate evidence: $Report"
if ($Process.ExitCode -ne 0 -or -not $Evidence.passed) {
    throw "Acquisition acceptance failed; no personal runtime was used."
}
if ($Evidence.mode -ne $Mode -or -not $Evidence.runtime_payload_removed) {
    throw "Acceptance evidence has the wrong mode or incomplete disposable cleanup."
}
if (-not $Source -and -not $Evidence.frozen_executable) {
    throw "Expected acceptance evidence from the actual frozen executable."
}
Write-Host "Acquisition acceptance passed."
