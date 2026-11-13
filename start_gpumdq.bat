@echo off
title gpumdq server (WSL) - keep this window open, minimise it if you like
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8765"
rem Runs start_wsl.sh from the folder this .bat is in (wsl --cd accepts a Windows path)
wsl.exe --cd "%~dp0." -e bash ./start_wsl.sh
echo.
echo gpumdq server stopped. Running GPUMD jobs keep going.
pause
