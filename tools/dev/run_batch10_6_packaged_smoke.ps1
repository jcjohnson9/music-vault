[CmdletBinding()]
param([ValidateRange(15, 180)][int]$TimeoutSeconds = 90)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_6_packaged_smoke.py"

foreach ($RequiredFile in @($Python, $Exe, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required Batch 10.6 packaged-smoke input is unavailable."
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before the packaged smoke."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The packaged network-observation command is unavailable."
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$Runtime = Join-Path $env:TEMP "MusicVault_Batch10_6_PackagedSmoke_$Timestamp"
$Manifest = Join-Path $Runtime "acceptance-manifest.json"
$NetworkReport = Join-Path $Runtime "batch10_6-network-report.json"
$ReviewPlan = Join-Path $Runtime "batch10_6-ui-review-plan.json"
$ReviewOutput = "${Runtime}_Review"
$ReviewManifest = Join-Path $ReviewOutput "manifest.json"
$Process = $null
$Succeeded = $false
$GracefulCloseConfirmed = $false
$NetworkConnectionObserved = $false
$Previous = @{}
$EnvironmentNames = @(
    "MUSIC_VAULT_PROJECT_ROOT",
    "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS",
    "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK",
    "MUSIC_VAULT_DISABLE_NETWORK",
    "MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT",
    "MUSIC_VAULT_UI_REVIEW",
    "MUSIC_VAULT_ARTIST_IMAGE_PROVIDER"
)
foreach ($Name in $EnvironmentNames) {
    $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
}

try {
    Set-Location -LiteralPath $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    & $Python -B $Tool prepare --runtime $Runtime --project-root $ProjectRoot --manifest $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged schema-7 runtime preparation failed." }

    $env:MUSIC_VAULT_PROJECT_ROOT = $Runtime
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_DISABLE_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $NetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $ReviewPlan
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = "synthetic"

    $Process = Start-Process -FilePath $Exe -WorkingDirectory $Runtime -PassThru
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $Deadline -and -not $Process.HasExited) {
        Start-Sleep -Milliseconds 100
        $Process.Refresh()
        $Connections = @(
            Get-NetTCPConnection -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
                Where-Object { $_.State -ne "Listen" }
        )
        if ($Connections.Count -gt 0) { $NetworkConnectionObserved = $true }
    }
    if ($NetworkConnectionObserved) {
        throw "A network connection was observed from the packaged process."
    }
    if (-not $Process.HasExited) {
        [void]$Process.CloseMainWindow()
        [void]$Process.WaitForExit(15000)
        throw "The packaged UI review did not finish before the timeout."
    }
    if ($Process.ExitCode -ne 0) {
        throw "The packaged UI review process exited with a failure."
    }
    if (-not (Test-Path -LiteralPath $ReviewManifest -PathType Leaf)) {
        throw "The packaged production UI review manifest was not generated."
    }
    $GracefulCloseConfirmed = $true

    & $Python -B $Tool verify --runtime $Runtime --project-root $ProjectRoot `
        --manifest $Manifest --graceful-close-confirmed --network-report $NetworkReport `
        --review-manifest $ReviewManifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged Batch 10.6 verification failed." }
    $Succeeded = $true
}
finally {
    foreach ($Name in $EnvironmentNames) {
        [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], "Process")
    }
    if ($null -ne $Process -and -not $Process.HasExited) {
        Write-Warning "The owned packaged process is still running; TEMP evidence was retained."
    }
    elseif ($Succeeded -and $GracefulCloseConfirmed -and (Test-Path -LiteralPath $Runtime)) {
        $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
        $ResolvedRuntime = (Resolve-Path -LiteralPath $Runtime).Path
        $Leaf = Split-Path -Leaf $ResolvedRuntime
        if (-not $ResolvedRuntime.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a runtime outside TEMP."
        }
        if (-not $Leaf.StartsWith("MusicVault_Batch10_6_PackagedSmoke_", [StringComparison]::Ordinal)) {
            throw "Refusing to remove a runtime without the acceptance prefix."
        }
        Remove-Item -LiteralPath $ResolvedRuntime -Recurse -Force
        if (Test-Path -LiteralPath $ReviewOutput) {
            $ResolvedReview = (Resolve-Path -LiteralPath $ReviewOutput).Path
            if (-not $ResolvedReview.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to remove review evidence outside TEMP."
            }
            if (-not (Split-Path -Leaf $ResolvedReview).StartsWith("MusicVault_Batch10_6_PackagedSmoke_", [StringComparison]::Ordinal)) {
                throw "Refusing to remove review evidence without the acceptance prefix."
            }
            Remove-Item -LiteralPath $ResolvedReview -Recurse -Force
        }
    }
    elseif (Test-Path -LiteralPath $Runtime) {
        Write-Warning "Packaged smoke evidence was retained because the smoke did not pass."
    }
}
