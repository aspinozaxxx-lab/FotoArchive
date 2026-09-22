param([switch]$Incremental, [string]$DistPath = 'dist')
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Push-Location $projectDir
try {
    $cleanArguments = if ($Incremental) { @() } else { @('--clean') }
    & .venv\Scripts\python.exe -m PyInstaller --noconfirm @cleanArguments --distpath $DistPath FotoArchive.spec
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
    $bundleDir = Join-Path $DistPath 'FotoArchive'
    foreach ($document in @('VALIDATION.md', 'VALIDATION-v064.md', 'VALIDATION-v070.md', 'CATALOG-v070.md', 'README.md', 'requirements.lock')) {
        if (Test-Path -LiteralPath $document) { Copy-Item -LiteralPath $document -Destination (Join-Path $bundleDir $document) }
    }
} finally {
    Pop-Location
}
