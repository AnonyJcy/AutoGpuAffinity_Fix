$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
try {
    python -c "import PyInstaller, wmi, psutil"
    if ($LASTEXITCODE -ne 0) { throw 'Dependencies missing. Run BUILD.cmd first.' }
    $buildArguments = @(
        '.\AutoGpuAffinity\main.py', '--noconfirm', '--clean', '--onefile', '--uac-admin',
        '--name', 'AutoGpuAffinity',
        '--add-binary', "${PSScriptRoot}\AutoGpuAffinity\bin\liblava\lava-triangle.exe;bin\liblava",
        '--add-binary', "${PSScriptRoot}\AutoGpuAffinity\bin\PresentMon\PresentMon-1.10.0-x64.exe;bin\PresentMon",
        '--add-binary', "${PSScriptRoot}\AutoGpuAffinity\bin\PresentMon\PresentMon-1.6.0-x64.exe;bin\PresentMon",
        '--add-binary', "${PSScriptRoot}\AutoGpuAffinity\bin\D3D9-benchmark.exe;bin",
        '--add-data', "${PSScriptRoot}\AutoGpuAffinity\bin\liblava\LICENSE.txt;bin\liblava",
        '--add-data', "${PSScriptRoot}\AutoGpuAffinity\bin\PresentMon\LICENSE.txt;bin\PresentMon",
        '--add-data', "${PSScriptRoot}\AutoGpuAffinity\config.ini;.",
        '--add-data', "${PSScriptRoot}\LICENSE;.", '--add-data', "${PSScriptRoot}\NOTICE.md;.",
        '--distpath', '.\build\AutoGpuAffinity', '--workpath', '.\build\pyinstaller', '--specpath', '.\build\pyinstaller'
    )
    python -m PyInstaller @buildArguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed; no package created.' }
    $exePath = Join-Path $PSScriptRoot 'build\AutoGpuAffinity\AutoGpuAffinity.exe'
    if (-not (Test-Path $exePath)) { throw 'Executable missing after build.' }
    # All renderer/PresentMon binaries and their runtime DLLs are embedded above.
    Copy-Item .\README.md, .\LICENSE, .\NOTICE.md .\build\AutoGpuAffinity\ -Force
    Write-Host 'Build succeeded: build\AutoGpuAffinity\AutoGpuAffinity.exe'
    exit 0
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
