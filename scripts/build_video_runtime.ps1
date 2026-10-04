param([string]$Data = 'D:\FotoArchiveData')
$ErrorActionPreference = 'Stop'
$videoRoot = Join-Path $Data 'runtime'
$videoCompiler = Join-Path $videoRoot 'toolchain\llvm-mingw-20260922-ucrt-x86_64\bin'
$env:PATH = $videoCompiler + ';' + (Join-Path $PSScriptRoot '..\.venv\Scripts') + ';' + $env:PATH
$env:VULKAN_SDK = Join-Path $videoRoot 'vulkan-sdk'
$videoSource = Join-Path $videoRoot 'omni-source'
$videoHeader = Join-Path $videoSource 'src\llama-graph.h'
$videoHeaderText = [System.IO.File]::ReadAllText($videoHeader)
if (-not $videoHeaderText.Contains('#include <cstdlib>')) {
    [System.IO.File]::WriteAllText($videoHeader, '#include <cstdlib>' + [Environment]::NewLine + $videoHeaderText)
}
$videoHeader = Join-Path $videoSource 'src\llama-hparams.cpp'
$videoHeaderText = [System.IO.File]::ReadAllText($videoHeader)
if (-not $videoHeaderText.Contains('#include <algorithm>')) {
    [System.IO.File]::WriteAllText($videoHeader, '#include <algorithm>' + [Environment]::NewLine + $videoHeaderText)
}
$videoBuild = Join-Path $videoRoot 'omni-build'
$videoHeader = Join-Path $videoSource 'tools\mtmd\mtmd-audio.cpp'
$videoHeaderText = [System.IO.File]::ReadAllText($videoHeader)
if (-not $videoHeaderText.Contains('#include <functional>')) {
    [System.IO.File]::WriteAllText($videoHeader, '#include <functional>' + [Environment]::NewLine + $videoHeaderText)
}
$videoHeader = Join-Path $videoSource 'tools\omni\omni.h'
$videoHeaderText = [System.IO.File]::ReadAllText($videoHeader)
if (-not $videoHeaderText.Contains('#if !defined(__MINGW32__)')) {
    [System.IO.File]::WriteAllText($videoHeader,$videoHeaderText.Replace('typedef int pid_t;', "#if !defined(__MINGW32__)`ntypedef int pid_t;`n#endif"))
}
$videoHeader = Join-Path $videoSource 'tools\omni\audition.cpp'
$videoHeaderText = [System.IO.File]::ReadAllText($videoHeader)
if (-not $videoHeaderText.Contains('FotoArchive audio backend:')) {
    $videoHeaderText=$videoHeaderText.Replace('backend_ptrs.push_back(backend_cpu);', 'fprintf(stderr, "FotoArchive audio backend: %s\n", ggml_backend_name(backend));' + [Environment]::NewLine + 'backend_ptrs.push_back(backend_cpu);')
    $videoHeaderText=$videoHeaderText.Replace('ggml_backend_tensor_get(final_out, vec, 0, ggml_nbytes(final_out));', 'ggml_backend_tensor_get(final_out, vec, 0, ggml_nbytes(final_out));' + [Environment]::NewLine + 'fprintf(stderr, "FotoArchive audio computed: %s\n", ggml_backend_name(ggml_backend_sched_get_tensor_backend(ctx->sched.get(), final_out)));')
    [System.IO.File]::WriteAllText($videoHeader,$videoHeaderText)
}
& cmake -S $videoSource -B $videoBuild -G Ninja '-DCMAKE_BUILD_TYPE=Release' '-DCMAKE_SYSTEM_NAME=Windows' '-DCMAKE_C_COMPILER=clang.exe' '-DCMAKE_CXX_COMPILER=clang++.exe' '-DCMAKE_CXX_FLAGS=-include cstdlib -include algorithm -include new' '-DGGML_VULKAN=ON' '-DGGML_OPENMP=OFF' '-DLLAMA_CURL=OFF' '-DLLAMA_BUILD_TESTS=OFF' '-DLLAMA_BUILD_EXAMPLES=OFF' '-DLLAMA_BUILD_SERVER=ON' '-DBUILD_SHARED_LIBS=ON'
if ($LASTEXITCODE) { throw 'Video runtime configuration failed' }
& cmake --build $videoBuild --target llama-server --parallel 8
if ($LASTEXITCODE) { throw 'Video runtime build failed' }
$videoTarget = Join-Path $videoRoot 'video-omni'
New-Item -ItemType Directory -Path $videoTarget -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $videoBuild 'bin') -File | Where-Object { $_.Extension -eq '.dll' -or $_.Name -eq 'llama-server.exe' } | Copy-Item -Destination $videoTarget
foreach ($library in @('libc++.dll','libunwind.dll','libwinpthread-1.dll')) {
    $videoLibrary = Join-Path $videoCompiler $library
    if (Test-Path -LiteralPath $videoLibrary) { Copy-Item -LiteralPath $videoLibrary -Destination $videoTarget }
}
'61d8393fdddff58e8f831ccfdf8a80ff989ebf9c' | Set-Content -LiteralPath (Join-Path $videoTarget 'revision.txt')
Write-Output "Vulkan video runtime ready: $videoTarget"
