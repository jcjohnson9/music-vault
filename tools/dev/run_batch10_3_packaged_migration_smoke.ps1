[CmdletBinding()]
param([ValidateRange(15, 180)][int]$TimeoutSeconds = 60)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_3_packaged_migration_smoke.py"

foreach ($RequiredFile in @($Python, $Exe, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required Batch 10.3 packaged-smoke input is unavailable."
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before the packaged migration smoke."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The packaged network-observation command is unavailable."
}

if (-not ("Batch103OwnedWindow" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class Batch103OwnedWindow
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
$Runtime = Join-Path $env:TEMP "MusicVault_Batch10_3_PackagedMigration_$Timestamp"
$Manifest = Join-Path $Runtime "acceptance-manifest.json"
$NetworkReport = Join-Path $Runtime "batch10_3-network-report.json"
$ReviewPlan = Join-Path $Runtime "batch10_3-ui-review-plan.json"
$ReviewOutput = "${Runtime}_Review"
$ReviewManifest = Join-Path $ReviewOutput "manifest.json"
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

try {
    Set-Location -LiteralPath $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    & $Python -B $Tool prepare --runtime $Runtime --project-root $ProjectRoot --manifest $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged schema-6 runtime preparation failed." }

    $env:MUSIC_VAULT_PROJECT_ROOT = $Runtime
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $NetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $ReviewPlan
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = "synthetic"

    $Process = Start-Process -FilePath $Exe -WorkingDirectory $Runtime -PassThru
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Status = Join-Path $Runtime "data\music_vault_status.json"
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
        $CloseRequested = $Process.CloseMainWindow()
        if (-not $CloseRequested) {
            $CloseRequested = [Batch103OwnedWindow]::PostCloseOwned($Process.Id) -gt 0
        }
        if ($CloseRequested) { [void]$Process.WaitForExit(15000) }
        throw "The packaged UI review did not finish before the timeout."
    }
    if ($Process.ExitCode -ne 0) {
        throw "The packaged UI review process exited with a failure."
    }
    if (-not (Test-Path -LiteralPath $Status -PathType Leaf)) {
        throw "Packaged Music Vault did not finish migration startup."
    }
    if (-not (Test-Path -LiteralPath $ReviewManifest -PathType Leaf)) {
        throw "The packaged production UI review manifest was not generated."
    }
    $ReviewEvidence = Get-Content -Raw -LiteralPath $ReviewManifest | ConvertFrom-Json
    if ($ReviewEvidence.status -ne "complete") {
        throw "The packaged production UI review did not complete."
    }
    $GracefulCloseConfirmed = $true

    & $Python -B $Tool verify --runtime $Runtime --project-root $ProjectRoot `
        --manifest $Manifest --graceful-close-confirmed --network-report $NetworkReport `
        --review-manifest $ReviewManifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged schema-7 verification failed." }
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
        Write-Warning "The owned packaged process is still running; TEMP evidence was retained."
    }
    elseif ($Succeeded -and $GracefulCloseConfirmed -and (Test-Path -LiteralPath $Runtime)) {
        $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
        $ResolvedRuntime = (Resolve-Path -LiteralPath $Runtime).Path
        $Leaf = Split-Path -Leaf $ResolvedRuntime
        if (-not $ResolvedRuntime.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a runtime outside TEMP."
        }
        if (-not $Leaf.StartsWith("MusicVault_Batch10_3_PackagedMigration_", [StringComparison]::Ordinal)) {
            throw "Refusing to remove a runtime without the acceptance prefix."
        }
        Remove-Item -LiteralPath $ResolvedRuntime -Recurse -Force
        if (Test-Path -LiteralPath $ReviewOutput) {
            $ResolvedReview = (Resolve-Path -LiteralPath $ReviewOutput).Path
            $ReviewLeaf = Split-Path -Leaf $ResolvedReview
            if (-not $ResolvedReview.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to remove review evidence outside TEMP."
            }
            if (-not $ReviewLeaf.StartsWith("MusicVault_Batch10_3_PackagedMigration_", [StringComparison]::Ordinal)) {
                throw "Refusing to remove review evidence without the acceptance prefix."
            }
            Remove-Item -LiteralPath $ResolvedReview -Recurse -Force
        }
    }
    elseif (Test-Path -LiteralPath $Runtime) {
        Write-Warning "Packaged migration evidence was retained because the smoke did not pass."
        if (Test-Path -LiteralPath $ReviewOutput) {
            Write-Warning "Packaged UI review evidence was retained because the smoke did not pass."
        }
    }
}
