@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "FAST="
set "SHADOW="
for /f "delims=" %%F in ('dir /b /o-d docs\canary_shadow_fast15m*.json 2^>nul') do if not defined FAST set "FAST=docs\%%F"
for /f "delims=" %%F in ('dir /b /o-d docs\canary_shadow_24h*.json 2^>nul') do if not defined SHADOW set "SHADOW=docs\%%F"

if not defined FAST (
  echo [finalize] ERROR: no fast-gate artifact found in docs\
  exit /b 2
)
if not defined SHADOW (
  echo [finalize] ERROR: no 24h shadow artifact found in docs\
  exit /b 2
)

echo [finalize] fast=%FAST%
echo [finalize] shadow=%SHADOW%
"%TRADER_PYTHON_EXE%" -m src.trader.cli audit finalize-build -- --evidence-root docs/audit --fast-gate-artifact "%FAST%" --shadow-artifact "%SHADOW%" --rollback-validated
exit /b %ERRORLEVEL%
