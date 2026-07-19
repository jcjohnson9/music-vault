[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("batch10.4-live-schema7-quiescence")]
    [string]$AcknowledgeLiveLibrary,
    [ValidateRange(10, 180)]
    [int]$StartupTimeoutSeconds = 60,
    [ValidateRange(5, 60)]
    [int]$CloseTimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\batch10_4_acceptance.py"
$Status = Join-Path $ProjectRoot "data\music_vault_status.json"

foreach ($RequiredFile in @($Python, $Exe, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required Batch 10.4 controlled-startup input is unavailable."
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before controlled live acceptance."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The required network-observation command is unavailable."
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

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$EvidenceRoot = Join-Path $env:TEMP "MusicVault_Batch10_4_LiveAcceptance_$Timestamp"
$Manifest = Join-Path $EvidenceRoot "baseline.json"
$NetworkReport = Join-Path $EvidenceRoot "network-report.json"
$Process = $null
$GracefulCloseConfirmed = $false
$NetworkConnectionObserved = $false
$StatusBefore = if (Test-Path -LiteralPath $Status -PathType Leaf) {
    (Get-Item -LiteralPath $Status).LastWriteTimeUtc
} else {
    [DateTime]::MinValue
}

$PreviousRoot = $env:MUSIC_VAULT_PROJECT_ROOT
$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousNoNetwork = $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK
$PreviousNetworkReport = $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT
$PreviousReview = $env:MUSIC_VAULT_UI_REVIEW
$PreviousArtistProvider = $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER

try {
    Set-Location -LiteralPath $ProjectRoot
    $env:MUSIC_VAULT_PROJECT_ROOT = $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $NetworkReport
    Remove-Item Env:MUSIC_VAULT_UI_REVIEW -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER -ErrorAction SilentlyContinue

    $ExpectedCounts = @(
        "tracks=304",
        "playlists=2",
        "playlist_tracks=7",
        "playlist_track_origins=7",
        "sync_sources=1",
        "sync_source_items=328",
        "source_track_identities=304",
        "canonical_albums=167",
        "track_album_memberships=178",
        "artists=427",
        "artist_aliases=3",
        "artist_relationships=0",
        "track_artist_credits=309",
        "track_metadata_fields=2736",
        "track_metadata_history=1303",
        "track_metadata_observations=3418",
        "metadata_provider_cache=303",
        "metadata_intelligence_jobs=1",
        "metadata_intelligence_items=304",
        "metadata_remediation_jobs=1",
        "metadata_remediation_items=303"
    )
    $PrepareArgs = @(
        "-B", $Tool, "prepare-live",
        "--project-root", $ProjectRoot,
        "--evidence-dir", $EvidenceRoot,
        "--manifest", $Manifest,
        "--acknowledge-live-library", $AcknowledgeLiveLibrary,
        "--expected-cache-file-count", "226",
        "--expected-cache-total-bytes", "30791281"
    )
    foreach ($ExpectedCount in $ExpectedCounts) {
        $PrepareArgs += @("--expected-count", $ExpectedCount)
    }
    & $Python @PrepareArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Batch 10.4 baseline, cache audit, or schema-7 backup failed."
    }

    $Process = Start-Process -FilePath $Exe -WorkingDirectory $ProjectRoot -PassThru
    $Deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    $WindowReady = $false
    $StatusUpdated = $false
    while ((Get-Date) -lt $Deadline -and -not $Process.HasExited) {
        Start-Sleep -Milliseconds 100
        $Process.Refresh()
        $Connections = @(
            Get-NetTCPConnection -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
                Where-Object { $_.State -ne "Listen" }
        )
        if ($Connections.Count -gt 0) { $NetworkConnectionObserved = $true }
        if ($Process.MainWindowHandle -ne 0) {
            $WindowReady = $true
            if ($Process.MainWindowTitle -notlike "Music Vault v1.1.0 Development*") {
                throw "The controlled window title did not identify v1.1.0 Development."
            }
        }
        if (Test-Path -LiteralPath $Status -PathType Leaf) {
            $StatusUpdated = (Get-Item -LiteralPath $Status).LastWriteTimeUtc -gt $StatusBefore
        }
        if ($WindowReady -and $StatusUpdated) { break }
    }
    if ($Process.HasExited) {
        throw "Music Vault exited before controlled acceptance reached the main window."
    }
    if (-not $WindowReady -or -not $StatusUpdated) {
        throw "Music Vault did not reach the controlled acceptance ready state."
    }
    if ($NetworkConnectionObserved) {
        throw "A network connection was observed from the controlled process."
    }

    $CloseRequested = $Process.CloseMainWindow()
    if (-not $CloseRequested) {
        $CloseRequested = [Batch104OwnedWindow]::PostCloseOwned($Process.Id) -gt 0
    }
    if (-not $CloseRequested -or -not $Process.WaitForExit($CloseTimeoutSeconds * 1000)) {
        throw "The controlled process did not close gracefully."
    }
    if ($Process.ExitCode -ne 0) {
        throw "The controlled process exited with a failure."
    }
    $GracefulCloseConfirmed = $true

    & $Python -B $Tool verify-live --project-root $ProjectRoot --manifest $Manifest `
        --network-report $NetworkReport --graceful-close-confirmed
    if ($LASTEXITCODE -ne 0) {
        throw "Batch 10.4 live quiescence verification failed."
    }
    Write-Host "Batch 10.4 controlled live acceptance passed."
    Write-Host "Private aggregate evidence was retained under TEMP."
}
finally {
    $env:MUSIC_VAULT_PROJECT_ROOT = $PreviousRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = $PreviousNoNetwork
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $PreviousNetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $PreviousReview
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = $PreviousArtistProvider

    if ($null -ne $Process -and -not $Process.HasExited) {
        Write-Warning "The owned Music Vault process is still running; it was not force-terminated."
    }
    if (-not $GracefulCloseConfirmed) {
        Write-Warning "Batch 10.4 evidence was retained because acceptance did not finish."
    }
}
