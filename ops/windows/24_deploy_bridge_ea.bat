@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp024_deploy_bridge_ea.ps1" %*
exit /b %errorlevel%
