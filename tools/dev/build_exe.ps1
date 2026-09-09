$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

if (Get-Process -Name MusicVault -ErrorAction SilentlyContinue) {
    throw "Close Music Vault before rebuilding the official desktop target."
}
Set-Location $projectRoot
& $python -B -c "from music_vault.core.acquisition_runtime import acquisition_readiness; r = acquisition_readiness(); print(r.public_summary()); raise SystemExit(0 if r.ready else 1)"
if ($LASTEXITCODE -ne 0) { throw "Acquisition components are incomplete; existing build output was preserved." }

foreach ($relativePath in @("build", "dist")) {
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $relativePath))

    if (-not $target.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the project root: $target"
    }

    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

Set-Location $projectRoot
$basePython = (& $python -c "import sys; print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $basePython) {
    throw "Could not resolve the release Python base directory."
}

# PyInstaller searches PATH while resolving native dependencies. Keep that
# search deterministic and prevent DLLs from unrelated installed applications
# from entering Analysis provenance or the release candidate.
$originalPath = $env:PATH
$safePathEntries = @(
    (Split-Path -Parent $python),
    $basePython,
    (Join-Path $basePython "DLLs"),
    (Join-Path $env:SystemRoot "System32"),
    $env:SystemRoot
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } | Select-Object -Unique

try {
    $env:PATH = $safePathEntries -join [IO.Path]::PathSeparator
    & $python -m PyInstaller --noconfirm --clean .\MusicVault.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
finally {
    $env:PATH = $originalPath
}
