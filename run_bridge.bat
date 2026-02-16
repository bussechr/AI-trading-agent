@echo off
title MT4 Bridge Server
echo Starting Python Bridge Server...
echo.
cd /d "%~dp0"
set PYTHONPATH=%~dp0

:: Find Python - check known locations first (Windows Store alias interferes with PATH)
if exist "C:\Python311\python.exe" (
    "C:\Python311\python.exe" bridge_api\bridge.py
) else if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" bridge_api\bridge.py
) else (
    :: Last resort: try PATH (may hit Windows Store alias)
    python bridge_api\bridge.py
)
pause
