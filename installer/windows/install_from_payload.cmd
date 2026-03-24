@echo off
setlocal
set "SOURCE_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -SourceRoot "%SOURCE_ROOT%"
exit /b %errorlevel%
