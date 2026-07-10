$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

& (Join-Path $PSScriptRoot "verify.ps1")
if ($LASTEXITCODE -ne 0) { throw "Verification script failed." }

& (Join-Path $PSScriptRoot "build_exe.ps1")
if ($LASTEXITCODE -ne 0) { throw "Build script failed." }

& (Join-Path $PSScriptRoot "run_exe.ps1")
if ($LASTEXITCODE -ne 0) { throw "EXE launch script failed." }
