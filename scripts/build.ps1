param([switch]$Incremental, [switch]$ExecutableOnly, [string]$DistPath = 'dist')
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Push-Location $projectDir
$previousExeOnly = $env:FOTOARCHIVE_EXE_ONLY
try {
    $env:FOTOARCHIVE_EXE_ONLY = if ($ExecutableOnly) { '1' } else { '0' }
    [string[]]$buildArguments = @('--noconfirm', '--distpath', $DistPath, 'FotoArchive.spec')
    if (-not $Incremental) { $buildArguments = @('--clean') + $buildArguments }
    & .venv\Scripts\python.exe -m PyInstaller @buildArguments
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
    $bundleDir = Join-Path $DistPath 'FotoArchive'
    if ($ExecutableOnly) {
        # For code-only updates against an already installed matching runtime.
        # A full build is required after changing dependencies or bundled assets.
        New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null
        Copy-Item -LiteralPath 'build\FotoArchive\FotoArchive.exe' -Destination (Join-Path $bundleDir 'FotoArchive.exe')
    }
    foreach ($document in @('VALIDATION.md', 'VALIDATION-v064.md', 'VALIDATION-v070.md', 'VALIDATION-v071.md', 'VALIDATION-v072.md', 'VALIDATION-v080.md', 'VALIDATION-v081.md', 'VALIDATION-v082.md', 'VALIDATION-v083.md', 'VALIDATION-v084.md', 'CATALOG-v070.md', 'README.md', 'requirements.lock')) {
        if (Test-Path -LiteralPath $document) { Copy-Item -LiteralPath $document -Destination (Join-Path $bundleDir $document) }
    }
} finally {
    $env:FOTOARCHIVE_EXE_ONLY = $previousExeOnly
    Pop-Location
}
