$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "verify.ps1")
if ($LASTEXITCODE -ne 0) { throw "Verification script failed." }

& (Join-Path $PSScriptRoot "build_exe.ps1")
if ($LASTEXITCODE -ne 0) { throw "Build script failed." }

& (Join-Path $PSScriptRoot "run_exe_from_temp.ps1")
if ($LASTEXITCODE -ne 0) { throw "Alternate-working-directory launch script failed." }

Start-Sleep -Seconds 3
& (Join-Path $PSScriptRoot "check_status.ps1")
if ($LASTEXITCODE -ne 0) { throw "App Status check failed." }
