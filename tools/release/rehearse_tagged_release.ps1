param(
    [string]$ReleaseTag = "v1.0.0",
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$inventory = Join-Path $projectRoot "tools\release\third_party_licenses.json"
$sourceCache = Join-Path $projectRoot "release_artifacts\.source-cache"

function Resolve-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Boundary,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolvedCandidate = [IO.Path]::GetFullPath($Candidate)
    $resolvedBoundary = [IO.Path]::GetFullPath($Boundary).TrimEnd("\")
    if (-not $resolvedCandidate.StartsWith(
        $resolvedBoundary + "\",
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing $Label outside its approved boundary."
    }
    return $resolvedCandidate
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found."
}
if ($ReleaseTag -ne "v1.0.0") {
    throw "This corrective rehearsal is locked to v1.0.0."
}
if (git -C $projectRoot status --porcelain=v1 --untracked-files=all) {
    throw "Release tooling worktree must be clean."
}

$tagRef = "refs/tags/$ReleaseTag"
if ((git -C $projectRoot cat-file -t $tagRef).Trim() -ne "tag") {
    throw "Release tag must exist and be annotated."
}
$sourceCommit = (git -C $projectRoot rev-parse "$tagRef^{commit}").Trim()
$tagObject = (git -C $projectRoot rev-parse $tagRef).Trim()
if ($tagObject -ne "fc47cb2a3ad9e084d382739b9bc4d2e7cf771437") {
    throw "The immutable v1.0.0 tag object changed."
}
if ($sourceCommit -ne "af00394fa1e6c5c0f18c7db70d2aaf6a26e84a6b") {
    throw "The immutable v1.0.0 application commit changed."
}
$toolingCommit = (git -C $projectRoot rev-parse HEAD).Trim()
$tempParent = [IO.Path]::GetFullPath($env:TEMP).TrimEnd("\")
$tempRoot = Join-Path $tempParent ("MusicVault_Release_Rehearsal_" + [guid]::NewGuid().ToString("N"))
$tempRoot = Resolve-ContainedPath $tempRoot $tempParent "rehearsal TEMP root"
$applicationRoot = Join-Path $tempRoot "tagged-application"
$applicationRoot = Resolve-ContainedPath $applicationRoot $tempRoot "tagged worktree"
$transferRoot = Join-Path $tempRoot "transferred-payload"
$worktreeAdded = $false

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $projectRoot "release_artifacts\rehearsal-$ReleaseTag"
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) {
    if (@(Get-ChildItem -LiteralPath $output -Force).Count -ne 0) {
        throw "Rehearsal output directory must be absent or empty."
    }
} else {
    [IO.Directory]::CreateDirectory($output) | Out-Null
}

try {
    [IO.Directory]::CreateDirectory($tempRoot) | Out-Null
    $applicationRoot = Resolve-ContainedPath $applicationRoot $tempRoot "tagged worktree"
    git -C $projectRoot worktree add --detach $applicationRoot $tagRef
    if ($LASTEXITCODE -ne 0) { throw "Tagged application worktree creation failed." }
    $worktreeAdded = $true

    if ((git -C $applicationRoot rev-parse HEAD).Trim() -ne $sourceCommit) {
        throw "Detached application worktree does not match the tag."
    }
    if (git -C $applicationRoot status --porcelain=v1 --untracked-files=all) {
        throw "Tagged application worktree is dirty."
    }

    Push-Location $projectRoot
    try {
        $batchTests = @(Get-ChildItem -LiteralPath .\tests -Filter "test_batch8_1_*.py" | ForEach-Object FullName)
        & $python -B -m pytest -q .\tests\test_release_pipeline.py @batchTests
        if ($LASTEXITCODE -ne 0) { throw "Corrected release-tool tests failed." }
        & $python -B .\tools\security\pre_public_history_check.py --repo $projectRoot
        if ($LASTEXITCODE -ne 0) { throw "Complete publication-history scan failed." }

        Push-Location $applicationRoot
        try {
            & $python -B .\tools\verify_music_vault.py
            if ($LASTEXITCODE -ne 0) { throw "Tagged source verification failed." }
            & $python -B -m pytest -q
            if ($LASTEXITCODE -ne 0) { throw "Tagged regression suite failed." }
            & $python -B .\tools\security\pre_public_commit_check.py
            if ($LASTEXITCODE -ne 0) { throw "Tagged publication scan failed." }
            & $python -B -m compileall -q .\music_vault .\tests .\tools
            if ($LASTEXITCODE -ne 0) { throw "Tagged Python compilation failed." }
            & $python -m PyInstaller --noconfirm --clean .\MusicVault.spec
            if ($LASTEXITCODE -ne 0) { throw "Tagged EXE build failed." }
        } finally {
            Pop-Location
        }

        & $python -B .\tools\release\fetch_compliance_sources.py `
            --cache-dir $sourceCache `
            --inventory-path $inventory
        if ($LASTEXITCODE -ne 0) { throw "Corresponding-source preparation failed." }

        & $python -B .\tools\release\build_portable_release.py `
            --application-root $applicationRoot `
            --dist-dir (Join-Path $applicationRoot "dist\MusicVault") `
            --output-dir $output `
            --source-tag $ReleaseTag `
            --source-commit $sourceCommit `
            --release-tooling-commit $toolingCommit `
            --license-inventory $inventory `
            --source-cache $sourceCache `
            --require-clean-source
        if ($LASTEXITCODE -ne 0) { throw "Corrected tagged release build failed." }

        $portable = Join-Path $output "MusicVault-v1.0.0-Windows-x64-Portable.zip"
        & $python -B .\tools\release\verify_portable_release.py $portable
        if ($LASTEXITCODE -ne 0) { throw "Corrected release verification failed." }
        & $python -B .\tools\release\validate_release_payload.py write $output `
            --expected-source-tag $ReleaseTag `
            --expected-source-commit $sourceCommit `
            --expected-tooling-commit $toolingCommit
        if ($LASTEXITCODE -ne 0) { throw "Release payload sealing failed." }

        [IO.Directory]::CreateDirectory($transferRoot) | Out-Null
        foreach ($name in @(
            "MusicVault-v1.0.0-Windows-x64-Portable.zip",
            "MusicVault-v1.0.0-Windows-x64-Portable.zip.sha256",
            "MusicVault-v1.0.0-Source-Compliance.zip",
            "MusicVault-v1.0.0-Source-Compliance.zip.sha256",
            "release-manifest.json",
            "release-payload-index.json"
        )) {
            Copy-Item -LiteralPath (Join-Path $output $name) -Destination $transferRoot
        }
        & $python -B .\tools\release\validate_release_payload.py verify $transferRoot `
            --expected-source-tag $ReleaseTag `
            --expected-source-commit $sourceCommit `
            --expected-tooling-commit $toolingCommit
        if ($LASTEXITCODE -ne 0) { throw "Transferred release payload verification failed." }
        & $python -B .\tools\release\verify_portable_release.py `
            (Join-Path $transferRoot "MusicVault-v1.0.0-Windows-x64-Portable.zip")
        if ($LASTEXITCODE -ne 0) { throw "Transferred portable verification failed." }

        $smokeExtract = Join-Path $tempRoot "smoke-extract"
        $smokeCwd = Join-Path $tempRoot "unrelated-working-directory"
        [IO.Directory]::CreateDirectory($smokeExtract) | Out-Null
        [IO.Directory]::CreateDirectory($smokeCwd) | Out-Null
        Expand-Archive -LiteralPath (Join-Path $transferRoot "MusicVault-v1.0.0-Windows-x64-Portable.zip") -DestinationPath $smokeExtract
        $smokeExe = Join-Path $smokeExtract "MusicVault-v1.0.0-Windows-x64-Portable\MusicVault.exe"
        if (-not (Test-Path -LiteralPath $smokeExe -PathType Leaf)) {
            throw "Extracted smoke executable is missing."
        }
        $previousOverride = $env:MUSIC_VAULT_PROJECT_ROOT
        $env:MUSIC_VAULT_PROJECT_ROOT = $null
        $started = $null
        try {
            $started = Start-Process -FilePath $smokeExe -WorkingDirectory $smokeCwd -PassThru
            Start-Sleep -Seconds 5
            $running = @(Get-Process -Name MusicVault -ErrorAction SilentlyContinue | Where-Object {
                try { [IO.Path]::GetFullPath($_.Path) -eq [IO.Path]::GetFullPath($smokeExe) } catch { $false }
            })
            if ($running.Count -eq 0) { throw "Extracted packaged smoke process did not remain running." }
        } finally {
            foreach ($process in @(Get-Process -Name MusicVault -ErrorAction SilentlyContinue | Where-Object {
                try { [IO.Path]::GetFullPath($_.Path) -eq [IO.Path]::GetFullPath($smokeExe) } catch { $false }
            })) {
                [void]$process.CloseMainWindow()
                if (-not $process.WaitForExit(3000)) { Stop-Process -Id $process.Id -Force }
            }
            $env:MUSIC_VAULT_PROJECT_ROOT = $previousOverride
        }
    } finally {
        Pop-Location
    }

    Write-Host "Tagged release rehearsal passed: $output"
} finally {
    if ($worktreeAdded) {
        $applicationRoot = Resolve-ContainedPath $applicationRoot $tempRoot "tagged worktree cleanup"
        git -C $projectRoot worktree remove --force $applicationRoot
        if ($LASTEXITCODE -ne 0) { throw "Tagged application worktree removal failed." }
        git -C $projectRoot worktree prune
        if ($LASTEXITCODE -ne 0) { throw "Git worktree pruning failed." }
    }
    $resolvedTemp = Resolve-ContainedPath $tempRoot $tempParent "rehearsal TEMP cleanup"
    if (Test-Path -LiteralPath $resolvedTemp) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
