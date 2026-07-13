$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project venv interpreter not found: $python"
}

foreach ($relativePath in @("build", "dist")) {
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $relativePath))

    if (-not $target.StartsWith($projectRoot, [StringComparison]::OrdinalIgnoreCase)) {
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
