[CmdletBinding()]
param([ValidateRange(15, 180)][int]$TimeoutSeconds = 60)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch10_2_packaged_migration_smoke.py"
$Schema5Backup = Join-Path $ProjectRoot "data\backups\music_vault_batch10_1_explicit_rollback_20260716_003442_649.sqlite3"

foreach ($RequiredFile in @($Python, $Exe, $Tool, $Schema5Backup)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required packaged-migration input is unavailable."
    }
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before the packaged migration smoke."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The packaged network-observation command is unavailable."
}

if (-not ("Batch102OwnedWindow" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class Batch102OwnedWindow
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
                ShowWindowAsync(window, 0); // SW_HIDE
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
            if (owner == (uint)processId) {
                if (PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero)) { // WM_CLOSE
                    count++;
                }
            }
            return true;
        }, IntPtr.Zero);
        return count;
    }
}
'@
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$Runtime = Join-Path $env:TEMP "MusicVault_Batch10_2_PackagedMigration_$Timestamp"
$Manifest = Join-Path $Runtime "acceptance-manifest.json"
$Process = $null
$Succeeded = $false
$GracefulCloseConfirmed = $false
$NetworkConnectionObserved = $false
$PreviousRoot = $env:MUSIC_VAULT_PROJECT_ROOT
$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousReviewRequest = $env:MUSIC_VAULT_UI_REVIEW_REQUEST

try {
    Set-Location -LiteralPath $ProjectRoot
    & $Python -B $Tool prepare --runtime $Runtime --project-root $ProjectRoot --manifest $Manifest
    if ($LASTEXITCODE -ne 0) { throw "Packaged migration runtime preparation failed." }

    $env:MUSIC_VAULT_PROJECT_ROOT = $Runtime
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    Remove-Item Env:MUSIC_VAULT_UI_REVIEW_REQUEST -ErrorAction SilentlyContinue

    # This is the official frozen executable.  It receives an isolated TEMP
    # root, no credentials, no opted-in provider config, no available media,
    # and a hidden native window owned by this wrapper.
    $Process = Start-Process -FilePath $Exe -WorkingDirectory $Runtime `
        -WindowStyle Hidden -PassThru
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Status = Join-Path $Runtime "data\music_vault_status.json"
    while ((Get-Date) -lt $Deadline -and -not $Process.HasExited) {
        Start-Sleep -Milliseconds 100
        $Process.Refresh()
        [void][Batch102OwnedWindow]::HideOwned($Process.Id)
        $Connections = @(
            Get-NetTCPConnection -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
                Where-Object { $_.State -ne "Listen" }
        )
        if ($Connections.Count -gt 0) { $NetworkConnectionObserved = $true }
        if (Test-Path -LiteralPath $Status -PathType Leaf) { break }
    }
    if ($Process.HasExited) {
        throw "Packaged Music Vault exited before migration startup completed."
    }
    if (-not (Test-Path -LiteralPath $Status -PathType Leaf)) {
        throw "Packaged Music Vault did not finish migration startup in time."
    }
    if ($NetworkConnectionObserved) {
        throw "A network connection was observed from the packaged process."
    }

    # Prefer Qt's normal main-window close path.  A hidden window does not
    # always populate Process.MainWindowHandle, so fall back only to WM_CLOSE
    # posted to windows whose owning PID is exactly this smoke-test process.
    $CloseRequested = $Process.CloseMainWindow()
    if (-not $CloseRequested) {
        $CloseRequested = [Batch102OwnedWindow]::PostCloseOwned($Process.Id) -gt 0
    }
    if (-not $CloseRequested) {
        throw "The owned packaged process did not accept a graceful close request."
    }
    if (-not $Process.WaitForExit(15000)) {
        throw "The owned packaged process did not close gracefully."
    }
    $GracefulCloseConfirmed = $true

    & $Python -B $Tool verify --runtime $Runtime --project-root $ProjectRoot `
        --manifest $Manifest --graceful-close-confirmed --network-attempt-count 0
    if ($LASTEXITCODE -ne 0) { throw "Packaged schema-migration verification failed." }
    $Succeeded = $true
}
finally {
    $env:MUSIC_VAULT_PROJECT_ROOT = $PreviousRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
    $env:MUSIC_VAULT_UI_REVIEW_REQUEST = $PreviousReviewRequest

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
        if (-not $Leaf.StartsWith("MusicVault_Batch10_2_PackagedMigration_", [StringComparison]::Ordinal)) {
            throw "Refusing to remove a runtime without the acceptance prefix."
        }
        Remove-Item -LiteralPath $ResolvedRuntime -Recurse -Force
    }
    elseif (Test-Path -LiteralPath $Runtime) {
        Write-Warning "Packaged migration evidence was retained because the smoke did not pass."
    }
}
