@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [preflight] root: %ROOT%
echo [preflight] python: %TRADER_PYTHON_EXE%
echo [preflight] database: %FXSTACK_DATABASE_URL%

if /I "%FXSTACK_PACKAGE_MODE%"=="1" goto package_mode

where uv >nul 2>&1
if errorlevel 1 (
  echo [preflight] WARN: uv is not available in PATH ^(pip fallback mode^).
) else (
  echo [preflight] uv detected.
)

where node >nul 2>&1
if errorlevel 1 (
  echo [preflight] ERROR: node is not available in PATH.
  exit /b 2
)

where pnpm >nul 2>&1
if errorlevel 1 (
  echo [preflight] ERROR: pnpm is not available in PATH.
  exit /b 2
)

"%TRADER_PYTHON_EXE%" -m src.trader.cli stack preflight
if errorlevel 1 (
  echo [preflight] ERROR: stack preflight failed.
  exit /b 2
)

echo [preflight] OK
exit /b 0

:package_mode
echo [preflight] package mode enabled.
if not exist "%TRADER_PYTHON_EXE%" (
  echo [preflight] ERROR: bundled python runtime not found: %TRADER_PYTHON_EXE%
  exit /b 2
)
if not defined NODE_EXE (
  echo [preflight] ERROR: bundled node runtime not found.
  exit /b 2
)
if not exist "%NODE_EXE%" (
  echo [preflight] ERROR: bundled node runtime not found: %NODE_EXE%
  exit /b 2
)
"%TRADER_PYTHON_EXE%" -m src.trader.cli stack preflight --allow-sqlite
if errorlevel 1 (
  echo [preflight] ERROR: stack preflight failed.
  exit /b 2
)
echo [preflight] OK
exit /b 0
