[CmdletBinding()]
param(
    [switch]$RunLiveMigration,

    [ValidateSet("batch11-live-schema7-to-8")]
    [string]$AcknowledgeLiveLibrary,

    [ValidateRange(30, 240)]
    [int]$PackagedTimeoutSeconds = 150,

    [ValidateRange(15, 180)]
    [int]$LiveStartupTimeoutSeconds = 90,

    [ValidateRange(5, 60)]
    [int]$CloseTimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Exe = Join-Path $ProjectRoot "dist\MusicVault\MusicVault.exe"
$Tool = Join-Path $ProjectRoot "tools\dev\run_batch11_quality_e2e.py"
$WrongData = Join-Path $ProjectRoot "dist\MusicVault\data"

function Test-MusicVaultSourceProcess {
    $RootPattern = [regex]::Escape($ProjectRoot)
    $Processes = @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -match $RootPattern -and
                $_.CommandLine -match '([\\/])run\.py(?:\s|"|$)|music_vault\.app'
            }
    )
    return $Processes.Count -gt 0
}

foreach ($RequiredFile in @($Python, $Exe, $Tool)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required Batch 11 E2E input is unavailable. Build the official EXE first."
    }
}
if (Test-Path -LiteralPath $WrongData) {
    throw "dist\MusicVault\data already exists; the Batch 11 gate will not continue."
}
if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "MusicVault.exe must be closed before the Batch 11 essential E2E gate."
}
if (Test-MusicVaultSourceProcess) {
    throw "A source-run Music Vault process must be closed before the Batch 11 gate."
}
if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    throw "The required external network-observation command is unavailable."
}
if ($RunLiveMigration -and $AcknowledgeLiveLibrary -ne "batch11-live-schema7-to-8") {
    throw "The explicit Batch 11 live schema-migration acknowledgement is required."
}
if (-not $RunLiveMigration -and $AcknowledgeLiveLibrary) {
    throw "The live-library acknowledgement is accepted only with -RunLiveMigration."
}

if (-not ("Batch11OwnedWindow" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class Batch11OwnedWindow
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

function Wait-Batch11Process {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [switch]$WaitForNaturalExit,
        [string]$ReadyFile,
        [DateTime]$ReadyFileAfter = [DateTime]::MinValue
    )

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Ready = -not $ReadyFile
    $NetworkObserved = $false
    while ((Get-Date) -lt $Deadline) {
        Start-Sleep -Milliseconds 100
        $Process.Refresh()
        $Connections = @(
            Get-NetTCPConnection -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
                Where-Object { $_.State -ne "Listen" }
        )
        if ($Connections.Count -gt 0) {
            $NetworkObserved = $true
        }
        if ($ReadyFile -and (Test-Path -LiteralPath $ReadyFile -PathType Leaf)) {
            $Ready = (Get-Item -LiteralPath $ReadyFile).LastWriteTimeUtc -gt $ReadyFileAfter
        }
        if ($WaitForNaturalExit) {
            if ($Process.HasExited) { break }
        }
        elseif ($Ready) {
            break
        }
        if ($Process.HasExited) { break }
    }
    return [PSCustomObject]@{
        Ready = $Ready
        HasExited = $Process.HasExited
        NetworkObserved = $NetworkObserved
    }
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$Runtime = Join-Path $env:TEMP "MusicVault_Batch11_QualityE2E_$Timestamp"
$ReviewOutput = "$Runtime`_Review"
$ReviewPlan = Join-Path $Runtime "batch11-review-plan.json"
$StageAManifest = Join-Path $Runtime "stage-a-manifest.json"
$StageASummary = Join-Path $Runtime "stage-a-summary.json"
$NetworkDirectory = Join-Path $Runtime "MusicVault_Batch10_6_NetworkGuard"
$StageAPreparationNetworkReport = Join-Path $NetworkDirectory "batch11-preparation-network-report.json"
$StageANetworkReport = Join-Path $NetworkDirectory "batch11-network-report.json"
$FinalSummary = Join-Path $Runtime "batch11-e2e-summary.json"
$StageBRoot = $null
$StageBSummary = $null
$OwnedProcess = $null
$OwnedProcessIsLive = $false
$StageAClosed = $false
$StageBClosed = $false

$PreviousRoot = $env:MUSIC_VAULT_PROJECT_ROOT
$PreviousNoSecrets = $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS
$PreviousNoNetwork = $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK
$PreviousNetworkReport = $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT
$PreviousReview = $env:MUSIC_VAULT_UI_REVIEW
$PreviousArtistProvider = $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER
$PreviousLocalAppData = $env:LOCALAPPDATA

try {
    Set-Location -LiteralPath $ProjectRoot

    # The source-side synthetic preparation has its own process audit hook.
    # Force no-secret/no-network policy and remove inherited reporting/review
    # state so a stale developer environment cannot redirect or broaden it.
    $env:MUSIC_VAULT_PROJECT_ROOT = $ProjectRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $StageAPreparationNetworkReport
    Remove-Item Env:MUSIC_VAULT_UI_REVIEW -ErrorAction SilentlyContinue
    Remove-Item Env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER -ErrorAction SilentlyContinue

    & $Python -B $Tool prepare-stage-a --project-root $ProjectRoot `
        --runtime $Runtime --review-output $ReviewOutput
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $StageAManifest -PathType Leaf)) {
        throw "Batch 11 Stage A preparation failed."
    }

    $env:MUSIC_VAULT_PROJECT_ROOT = $Runtime
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $StageANetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $ReviewPlan
    $IsolatedLocalAppData = Join-Path $Runtime "local-app-data"
    New-Item -ItemType Directory -Path $IsolatedLocalAppData -Force | Out-Null
    $env:LOCALAPPDATA = $IsolatedLocalAppData
    Remove-Item Env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER -ErrorAction SilentlyContinue

    $OwnedProcess = Start-Process -FilePath $Exe -WorkingDirectory $Runtime -PassThru
    $OwnedProcessIsLive = $true
    $StageAWait = Wait-Batch11Process -Process $OwnedProcess `
        -TimeoutSeconds $PackagedTimeoutSeconds -WaitForNaturalExit
    if ($StageAWait.HasExited) {
        $OwnedProcessIsLive = $false
    }
    if (-not $StageAWait.HasExited) {
        throw "The isolated packaged review did not close itself in time."
    }
    if ($OwnedProcess.ExitCode -ne 0) {
        throw "The isolated packaged review exited with a failure."
    }
    $StageAClosed = $true

    $StageAVerify = @(
        "-B", $Tool, "verify-stage-a",
        "--project-root", $ProjectRoot,
        "--runtime", $Runtime,
        "--review-output", $ReviewOutput,
        "--graceful-close-confirmed"
    )
    if ($StageAWait.NetworkObserved) {
        $StageAVerify += "--external-network-connection-observed"
    }
    & $Python @StageAVerify
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $StageASummary -PathType Leaf)) {
        throw "Batch 11 isolated packaged Stage A verification failed."
    }

    if ($RunLiveMigration) {
        if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
            throw "MusicVault.exe must be closed before controlled live migration."
        }
        if (Test-MusicVaultSourceProcess) {
            throw "A source-run Music Vault process must be closed before live migration."
        }
        $StageBRoot = Join-Path $env:TEMP "MusicVault_Batch11_QualityE2E_${Timestamp}_Live"
        New-Item -ItemType Directory -Path $StageBRoot | Out-Null
        $StageBSummary = Join-Path $StageBRoot "stage-b-summary.json"
        $StageBNetworkDirectory = Join-Path $StageBRoot "MusicVault_Batch10_6_NetworkGuard"
        $StageBNetworkReport = Join-Path $StageBNetworkDirectory "batch11-network-report.json"
        New-Item -ItemType Directory -Path $StageBNetworkDirectory | Out-Null

        $env:MUSIC_VAULT_PROJECT_ROOT = $ProjectRoot
        $env:LOCALAPPDATA = $PreviousLocalAppData
        $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"
        $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = "1"
        $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $StageBNetworkReport
        Remove-Item Env:MUSIC_VAULT_UI_REVIEW -ErrorAction SilentlyContinue
        Remove-Item Env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER -ErrorAction SilentlyContinue

        & $Python -B $Tool prepare-live --project-root $ProjectRoot `
            --evidence-root $StageBRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Batch 11 controlled live baseline or rollback-backup verification failed."
        }

        $LiveStatus = Join-Path $ProjectRoot "data\music_vault_status.json"
        $StatusBefore = if (Test-Path -LiteralPath $LiveStatus -PathType Leaf) {
            (Get-Item -LiteralPath $LiveStatus).LastWriteTimeUtc
        }
        else {
            [DateTime]::MinValue
        }

        $OwnedProcess = Start-Process -FilePath $Exe -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden -PassThru
        $OwnedProcessIsLive = $true
        $StageBWait = Wait-Batch11Process -Process $OwnedProcess `
            -TimeoutSeconds $LiveStartupTimeoutSeconds -ReadyFile $LiveStatus `
            -ReadyFileAfter $StatusBefore
        if ($StageBWait.HasExited) {
            throw "Music Vault exited before controlled live startup completed."
        }
        if (-not $StageBWait.Ready) {
            throw "Controlled live startup did not reach its safe ready state."
        }
        if ($StageBWait.NetworkObserved) {
            throw "A network connection was observed during controlled live startup."
        }

        $CloseRequested = $OwnedProcess.CloseMainWindow()
        if (-not $CloseRequested) {
            $CloseRequested = [Batch11OwnedWindow]::PostCloseOwned($OwnedProcess.Id) -gt 0
        }
        if (-not $CloseRequested -or -not $OwnedProcess.WaitForExit($CloseTimeoutSeconds * 1000)) {
            throw "The controlled live process did not close gracefully."
        }
        if ($OwnedProcess.ExitCode -ne 0) {
            throw "The controlled live process exited with a failure."
        }
        $StageBClosed = $true
        $OwnedProcessIsLive = $false

        $StageBVerify = @(
            "-B", $Tool, "verify-live",
            "--project-root", $ProjectRoot,
            "--evidence-root", $StageBRoot,
            "--network-report", $StageBNetworkReport,
            "--graceful-close-confirmed"
        )
        if ($StageBWait.NetworkObserved) {
            $StageBVerify += "--external-network-connection-observed"
        }
        & $Python @StageBVerify
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $StageBSummary -PathType Leaf)) {
            throw "Batch 11 controlled live preservation verification failed."
        }
    }

    $Combine = @(
        "-B", $Tool, "combine",
        "--stage-a", $StageASummary,
        "--output", $FinalSummary
    )
    if ($StageBSummary) {
        $Combine += @("--stage-b", $StageBSummary)
    }
    & $Python @Combine
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $FinalSummary -PathType Leaf)) {
        throw "Batch 11 aggregate E2E report creation failed."
    }
    if (Test-Path -LiteralPath $WrongData) {
        throw "dist\MusicVault\data was created during Batch 11 acceptance."
    }

    if ($RunLiveMigration) {
        Write-Host "Batch 11 essential E2E passed."
    }
    else {
        Write-Host "Batch 11 Stage A passed; the essential E2E remains pending live migration."
    }
    Write-Host "Aggregate privacy-safe evidence: $FinalSummary"
}
finally {
    $env:MUSIC_VAULT_PROJECT_ROOT = $PreviousRoot
    $env:MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = $PreviousNoSecrets
    $env:MUSIC_VAULT_ACCEPTANCE_NO_NETWORK = $PreviousNoNetwork
    $env:MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT = $PreviousNetworkReport
    $env:MUSIC_VAULT_UI_REVIEW = $PreviousReview
    $env:MUSIC_VAULT_ARTIST_IMAGE_PROVIDER = $PreviousArtistProvider
    $env:LOCALAPPDATA = $PreviousLocalAppData

    if ($OwnedProcessIsLive -and $null -ne $OwnedProcess -and -not $OwnedProcess.HasExited) {
        $CloseRequested = $OwnedProcess.CloseMainWindow()
        if (-not $CloseRequested) {
            $CloseRequested = [Batch11OwnedWindow]::PostCloseOwned($OwnedProcess.Id) -gt 0
        }
        if ($CloseRequested) {
            [void]$OwnedProcess.WaitForExit($CloseTimeoutSeconds * 1000)
        }
    }
    if ($null -ne $OwnedProcess -and -not $OwnedProcess.HasExited) {
        Write-Warning "The owned Music Vault process is still running; it was not force-terminated."
    }
    if (-not $StageAClosed) {
        Write-Warning "Stage A evidence was retained because packaged acceptance did not finish."
    }
    if ($RunLiveMigration -and -not $StageBClosed) {
        Write-Warning "The rollback backup and live evidence were preserved; no automatic restore occurred."
    }
}
