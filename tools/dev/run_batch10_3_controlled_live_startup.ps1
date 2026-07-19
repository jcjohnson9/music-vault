[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [switch]$AcknowledgeLiveLibrary,

    [Parameter(Mandatory = $true)]
    [string]$Baseline,

    [Parameter(Mandatory = $true)]
    [string]$DryRun,

    [Parameter(Mandatory = $true)]
    [string]$Backup,

    [string]$Output,
    [ValidateRange(15, 180)][int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\verify_batch10_3_live_migration.py"
$Database = Join-Path $ProjectRoot "data\music_vault.sqlite3"
$Status = Join-Path $ProjectRoot "data\music_vault_status.json"

if (-not $AcknowledgeLiveLibrary) {
    throw "The controlled live-library acknowledgement is required."
}
foreach ($RequiredFile in @($Python, $Exe, $Tool, $Database, $Baseline, $DryRun, $Backup)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required controlled-startup input is unavailable."
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before controlled startup."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The packaged network-observation command is unavailable."
}

if (-not ("Batch103LiveOwnedWindow" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class Batch103LiveOwnedWindow
{
    private delegate bool EnumWindowsProc(IntPtr window, IntPtr state);
    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr state);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr window, int command);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

    public static int HideOwned(int processId)
    {
        int count = 0;
        EnumWindows(delegate (IntPtr window, IntPtr state) {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner == (uint)processId) {
                ShowWindowAsync(window, 0);
                count++;
            }
            return true;
        }, IntPtr.Zero);
        return count;
    }

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
$EvidenceRoot = Join-Path $env:TEMP "MusicVault_Batch10_3_LiveStartup_$Timestamp"
$NetworkReport = Join-Path $EvidenceRoot "batch10_3-network-report.json"
$Process = $null
$Succeeded = $false
$GracefulCloseConfirmed = $false
$NetworkConnectionObserved = $false
$PreviousRoot = $env:MUSIC_VAULT_PROJECT_ROOT
$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousNoNetwork = $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK
$PreviousNetworkReport = $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT
$PreviousReview = $env:MUSIC_VAULT_UI_REVIEW
$PreviousArtistProvider = $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER
$PreviousStatusWrite = if (Test-Path -LiteralPath $Status -PathType Leaf) {
    (Get-Item -LiteralPath $Status).LastWriteTimeUtc
} else {
    [DateTime]::MinValue
}

try {
    New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null
    Set-Location -LiteralPath $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    & $Python -B $Tool launch-preflight --acknowledge-live-library `
        batch10.3-live-schema-6-to-7 --project-root $ProjectRoot `
        --baseline $Baseline --dry-run $DryRun --backup $Backup
    if ($LASTEXITCODE -ne 0) { throw "Controlled startup preflight failed." }

    $env:MUSIC_VAULT_PROJECT_ROOT = $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $NetworkReport
    Remove-Item Env:MUSIC_VAULT_UI_REVIEW -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER -ErrorAction SilentlyContinue

    $Process = Start-Process -FilePath $Exe -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden -PassThru
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $StartupReady = $false
    while ((Get-Date) -lt $Deadline -and -not $Process.HasExited) {
        Start-Sleep -Milliseconds 100
        $Process.Refresh()
        [void][Batch103LiveOwnedWindow]::HideOwned($Process.Id)
        $Connections = @(
            Get-NetTCPConnection -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
                Where-Object { $_.State -ne "Listen" }
        )
        if ($Connections.Count -gt 0) { $NetworkConnectionObserved = $true }
        if (
            (Test-Path -LiteralPath $NetworkReport -PathType Leaf) -and
            (Test-Path -LiteralPath $Status -PathType Leaf) -and
            (Get-Item -LiteralPath $Status).LastWriteTimeUtc -gt $PreviousStatusWrite
        ) {
            $StartupReady = $true
            break
        }
    }
    if ($Process.HasExited) { throw "Music Vault exited before controlled startup completed." }
    if (-not $StartupReady) { throw "Controlled startup did not complete in time." }
    if ($NetworkConnectionObserved) {
        throw "A network connection was observed during controlled startup."
    }

    $CloseRequested = $Process.CloseMainWindow()
    if (-not $CloseRequested) {
        $CloseRequested = [Batch103LiveOwnedWindow]::PostCloseOwned($Process.Id) -gt 0
    }
    if (-not $CloseRequested -or -not $Process.WaitForExit(15000)) {
        throw "Music Vault did not close gracefully after controlled startup."
    }
    $GracefulCloseConfirmed = $true

    $VerifyArguments = @(
        "-B", $Tool, "verify", "--acknowledge-live-library",
        "batch10.3-live-schema-6-to-7", "--project-root", $ProjectRoot,
        "--baseline", $Baseline, "--dry-run", $DryRun, "--backup", $Backup,
        "--network-report", $NetworkReport
    )
    if ($Output) { $VerifyArguments += @("--output", $Output) }
    & $Python @VerifyArguments
    if ($LASTEXITCODE -ne 0) { throw "Controlled post-migration verification failed." }
    $Succeeded = $true
}
finally {
    $env:MUSIC_VAULT_PROJECT_ROOT = $PreviousRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = $PreviousNoNetwork
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $PreviousNetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $PreviousReview
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = $PreviousArtistProvider

    if ($null -ne $Process -and -not $Process.HasExited) {
        Write-Warning "The owned Music Vault process is still running; TEMP evidence was retained."
    }
    elseif ($Succeeded -and $GracefulCloseConfirmed -and (Test-Path -LiteralPath $EvidenceRoot)) {
        $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
        $ResolvedEvidence = (Resolve-Path -LiteralPath $EvidenceRoot).Path
        $Leaf = Split-Path -Leaf $ResolvedEvidence
        if (-not $ResolvedEvidence.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove evidence outside TEMP."
        }
        if (-not $Leaf.StartsWith("MusicVault_Batch10_3_LiveStartup_", [StringComparison]::Ordinal)) {
            throw "Refusing to remove evidence without the controlled prefix."
        }
        Remove-Item -LiteralPath $ResolvedEvidence -Recurse -Force
    }
    elseif (Test-Path -LiteralPath $EvidenceRoot) {
        Write-Warning "Controlled-startup evidence was retained because the gate did not pass."
    }
}
