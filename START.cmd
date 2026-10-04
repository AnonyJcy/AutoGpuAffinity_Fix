@echo off
cd /d "%~dp0"
python -c "import wmi, psutil"
if errorlevel 1 (
 echo Install dependencies with BUILD.cmd first.
 pause
 exit /b 1
)
set "AGA_ROOT=%~dp0"
powershell -NoProfile -Command "Start-Process -FilePath (Get-Command python.exe).Source -ArgumentList ([char]34 + (Join-Path $env:AGA_ROOT 'AutoGpuAffinity\main.py') + [char]34) -WorkingDirectory $env:AGA_ROOT -Verb RunAs"
if errorlevel 1 pause
