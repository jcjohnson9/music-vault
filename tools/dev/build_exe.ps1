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
& $python -m PyInstaller --noconfirm --clean .\MusicVault.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
