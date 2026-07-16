[CmdletBinding()]
param([ValidateRange(10, 120)][int]$TimeoutSeconds = 40)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_1_packaged_smoke.py"
$ReviewTool = Join-Path $ProjectRoot "tools\dev\capture_ui_review.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Project Python was not found." }
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw "Official MusicVault.exe was not found." }
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before the packaged smoke test."
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$Runtime = Join-Path $env:TEMP "MusicVault_Batch10_1_PackagedSmoke_$Timestamp"
$Manifest = Join-Path $Runtime "acceptance-manifest.json"
$ReviewOutput = Join-Path $env:TEMP "MusicVault_UI_Review_Output_Batch10_1_$Timestamp"
$Process = $null
$PreviousRoot = $env:MUSIC_VAULT_PROJECT_ROOT
$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS

try {
    Set-Location $ProjectRoot
    & $Python -B $Tool prepare --runtime $Runtime --project-root $ProjectRoot --manifest $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Synthetic runtime preparation failed." }

    $env:MUSIC_VAULT_PROJECT_ROOT = $Runtime
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $Process = Start-Process -FilePath $Exe -WorkingDirectory $Runtime -PassThru
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Status = Join-Path $Runtime "data\music_vault_status.json"
    while ((Get-Date) -lt $Deadline -and -not $Process.HasExited) {
        Start-Sleep -Milliseconds 200
        $Process.Refresh()
        if ((Test-Path -LiteralPath $Status) -and $Process.MainWindowHandle -ne 0) { break }
    }
    if ($Process.HasExited) { throw "Packaged Music Vault exited before startup completed." }
    if (-not (Test-Path -LiteralPath $Status -PathType Leaf)) { throw "Packaged App Status was not written." }
    if ($Process.MainWindowHandle -eq 0) { throw "Packaged Music Vault did not publish a closable main window." }
    if (-not $Process.CloseMainWindow()) { throw "Packaged Music Vault did not accept a graceful close request." }
    if (-not $Process.WaitForExit(15000)) { throw "Packaged Music Vault did not close gracefully." }

    # The existing explicit review hook runs inside the frozen EXE, blocks
    # networking, uses an owned synthetic runtime, validates metadata behavior,
    # and exercises its bounded synthetic provider.  No production fake is
    # enabled and no tool module is bundled.
    & $Python -B $ReviewTool --exe $Exe --output $ReviewOutput --size "1280x720" `
        --page "metadata_intelligence_smoke" --page "metadata_provenance_locks" --offscreen
    if ($LASTEXITCODE -ne 0) { throw "Strict packaged UI review failed." }
    $ReviewManifest = Join-Path $ReviewOutput "manifest.json"
    if (-not (Test-Path -LiteralPath $ReviewManifest -PathType Leaf)) {
        throw "Strict packaged UI review did not produce evidence."
    }

    & $Python -B $Tool verify --runtime $Runtime --project-root $ProjectRoot `
        --manifest $Manifest --review-manifest $ReviewManifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged synthetic verification failed." }
}
finally {
    if ($null -ne $Process -and -not $Process.HasExited) {
        Write-Warning "The owned packaged-smoke process is still running; its runtime was retained."
    }
    else {
        $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
        if ((Test-Path -LiteralPath $Runtime) -and $Runtime.StartsWith($ResolvedTemp) -and (Split-Path $Runtime -Leaf).StartsWith("MusicVault_Batch10_1_PackagedSmoke_")) {
            Remove-Item -LiteralPath $Runtime -Recurse -Force
        }
    }
    if (Test-Path -LiteralPath $ReviewOutput) {
        $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
        if ($ReviewOutput.StartsWith($ResolvedTemp) -and (Split-Path $ReviewOutput -Leaf).StartsWith("MusicVault_UI_Review_Output_Batch10_1_")) {
            Remove-Item -LiteralPath $ReviewOutput -Recurse -Force
        }
    }
    $env:MUSIC_VAULT_PROJECT_ROOT = $PreviousRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
}
