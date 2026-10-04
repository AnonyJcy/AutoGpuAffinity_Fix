@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto failed
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
if errorlevel 1 goto failed
explorer "%~dp0build\AutoGpuAffinity"
pause
exit /b 0
:failed
echo Build failed. Keep this window open and send the error.
pause
exit /b 1
