[CmdletBinding()]
param([ValidateRange(15, 180)][int]$TimeoutSeconds = 60)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_4_packaged_quiescence_smoke.py"

foreach ($RequiredFile in @($Python, $Exe, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required Batch 10.4 packaged-smoke input is unavailable."
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before the packaged quiescence smoke."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The packaged network-observation command is unavailable."
}

if (-not ("Batch104OwnedWindow" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class Batch104OwnedWindow
{
    private delegate bool EnumWindowsProc(IntPtr window, IntPtr state);
    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr state);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

    public static int PostCloseOwned(int processId)
    {
        int count = 0;
        EnumWindows(delegate (IntPtr window, IntPtr state) {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner == (uint)processId &&
                PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero)) {
                count++;
            }
            return true;
        }, IntPtr.Zero);
        return count;
    }
}
'@
}

function Close-Batch104ProcessGracefully($OwnedProcess) {
    $OwnedProcess.Refresh()
    if ($OwnedProcess.HasExited) { return $true }
    $CloseRequested = $OwnedProcess.CloseMainWindow()
    if (-not $CloseRequested) {
        $CloseRequested = [Batch104OwnedWindow]::PostCloseOwned($OwnedProcess.Id) -gt 0
    }
    if (-not $CloseRequested) { return $false }
    return $OwnedProcess.WaitForExit(15000)
}

function Remove-SafeBatch104TempTree([string]$Path, [string]$ExpectedPrefix) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path.TrimEnd('\')
    $ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $Leaf = Split-Path -Leaf $ResolvedPath
    if (-not $ResolvedPath.StartsWith($ResolvedTemp + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove evidence outside TEMP."
    }
    if (-not $Leaf.StartsWith($ExpectedPrefix, [StringComparison]::Ordinal)) {
        throw "Refusing to remove evidence without the controlled prefix."
    }
    Remove-Item -LiteralPath $ResolvedPath -Recurse -Force
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$Runtime = Join-Path $env:TEMP "MusicVault_Batch10_4_PackagedQuiescence_$Timestamp"
$ReviewOutput = "${Runtime}_SecondLaunchReview"
$Manifest = Join-Path $Runtime "acceptance-manifest.json"
$NetworkReport = Join-Path $Runtime "network-report.json"
$ReviewPlan = Join-Path $Runtime "batch10_4-second-launch-review.json"
$ReviewManifest = Join-Path $ReviewOutput "manifest.json"
$FirstProcess = $null
$SecondProcess = $null
$Succeeded = $false
$FirstClosed = $false
$SecondClosed = $false
$FirstNetworkObserved = $false
$SecondNetworkObserved = $false

$PreviousRoot = $env:MUSIC_VAULT_PROJECT_ROOT
$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousNoNetwork = $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK
$PreviousNetworkReport = $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT
$PreviousReview = $env:MUSIC_VAULT_UI_REVIEW
$PreviousArtistProvider = $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER
$PreviousDisableNetwork = $env:MUSIC_VAULT_DISABLE_NETWORK

try {
    Set-Location -LiteralPath $ProjectRoot
    & $Python -B $Tool prepare --runtime $Runtime --project-root $ProjectRoot --manifest $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged quiescence preparation failed." }

    $env:MUSIC_VAULT_PROJECT_ROOT = $Runtime
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $NetworkReport
    Remove-Item Env:MUSIC_VAULT_UI_REVIEW -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_DISABLE_NETWORK -ErrorAction SilentlyContinue

    # A hidden Qt top-level window has no MainWindowHandle on Windows and
    # therefore cannot receive the wrapper's normal CloseMainWindow request.
    # Keep the automated smoke unobtrusive while preserving a real, gracefully
    # closeable main window.
    $FirstProcess = Start-Process -FilePath $Exe -WorkingDirectory $Runtime -WindowStyle Minimized -PassThru
    $FirstDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Status = Join-Path $Runtime "data\music_vault_status.json"
    while ((Get-Date) -lt $FirstDeadline -and -not $FirstProcess.HasExited) {
        Start-Sleep -Milliseconds 100
        $FirstProcess.Refresh()
        $Connections = @(Get-NetTCPConnection -OwningProcess $FirstProcess.Id -ErrorAction SilentlyContinue |
            Where-Object { $_.State -ne "Listen" })
        if ($Connections.Count -gt 0) { $FirstNetworkObserved = $true }
        if ((Test-Path -LiteralPath $Status -PathType Leaf) -and
            (Test-Path -LiteralPath $NetworkReport -PathType Leaf)) { break }
    }
    if ($FirstProcess.HasExited) { throw "First packaged launch exited before startup completed." }
    if (-not (Test-Path -LiteralPath $Status -PathType Leaf)) {
        throw "First packaged launch did not write App Status in time."
    }
    if ($FirstNetworkObserved) { throw "A network connection was observed during migration startup." }
    $FirstClosed = Close-Batch104ProcessGracefully $FirstProcess
    if (-not $FirstClosed) { throw "First packaged launch did not close gracefully." }
    if ($FirstProcess.ExitCode -ne 0) { throw "First packaged launch exited with a failure." }

    & $Python -B $Tool verify-first --runtime $Runtime --project-root $ProjectRoot `
        --manifest $Manifest --network-report $NetworkReport `
        --graceful-close-confirmed --observed-network-connection-count 0
    if ($LASTEXITCODE -ne 0) { throw "First packaged quiescence verification failed." }

    # The second process is an ordinary current-schema launch. It has no
    # acceptance policy blocks and uses only the isolated synthetic UI provider.
    Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_DISABLE_NETWORK -ErrorAction SilentlyContinue
    $env:MUSIC_VAULT_UI_REVIEW = $ReviewPlan
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = "synthetic"

    $SecondProcess = Start-Process -FilePath $Exe -WorkingDirectory $Runtime -PassThru
    $SecondDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $SecondDeadline -and -not $SecondProcess.HasExited) {
        Start-Sleep -Milliseconds 100
        $SecondProcess.Refresh()
        $Connections = @(Get-NetTCPConnection -OwningProcess $SecondProcess.Id -ErrorAction SilentlyContinue |
            Where-Object { $_.State -ne "Listen" })
        if ($Connections.Count -gt 0) { $SecondNetworkObserved = $true }
    }
    if (-not $SecondProcess.HasExited) {
        [void](Close-Batch104ProcessGracefully $SecondProcess)
        throw "Second packaged synthetic review did not finish before timeout."
    }
    if ($SecondProcess.ExitCode -ne 0) { throw "Second packaged launch exited with a failure." }
    if ($SecondNetworkObserved) { throw "A real network connection was observed on the second launch." }
    if (-not (Test-Path -LiteralPath $ReviewManifest -PathType Leaf)) {
        throw "Second packaged launch did not create its synthetic review evidence."
    }
    $ReviewEvidence = Get-Content -Raw -LiteralPath $ReviewManifest | ConvertFrom-Json
    if ($ReviewEvidence.status -ne "complete") {
        throw "Second packaged synthetic review did not close normally."
    }
    $SecondClosed = $true

    & $Python -B $Tool verify-second --runtime $Runtime --project-root $ProjectRoot `
        --manifest $Manifest --review-manifest $ReviewManifest `
        --graceful-close-confirmed --observed-network-connection-count 0
    if ($LASTEXITCODE -ne 0) { throw "Second packaged quiescence verification failed." }
    $Succeeded = $true
}
finally {
    $env:MUSIC_VAULT_PROJECT_ROOT = $PreviousRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = $PreviousNoNetwork
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $PreviousNetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $PreviousReview
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = $PreviousArtistProvider
    $env:MUSIC_VAULT_DISABLE_NETWORK = $PreviousDisableNetwork

    $OwnedProcessStillRunning = @($FirstProcess, $SecondProcess) | Where-Object {
        $null -ne $_ -and -not $_.HasExited
    }
    if ($OwnedProcessStillRunning.Count -gt 0) {
        Write-Warning "An owned packaged process is still running; TEMP evidence was retained."
    }
    elseif ($Succeeded -and $FirstClosed -and $SecondClosed) {
        Remove-SafeBatch104TempTree $ReviewOutput "MusicVault_Batch10_4_PackagedQuiescence_"
        Remove-SafeBatch104TempTree $Runtime "MusicVault_Batch10_4_PackagedQuiescence_"
    }
    elseif (Test-Path -LiteralPath $Runtime) {
        Write-Warning "Packaged quiescence evidence was retained because the smoke did not pass."
    }
}
