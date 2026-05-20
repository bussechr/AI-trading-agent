@echo off
REM ==========================================================================
REM Canonical dev bootstrap for Windows. Idempotent.
REM
REM Brings a fresh checkout to a known-good developer state:
REM   1. Verifies uv and pnpm are installed.
REM   2. Detects + repairs the WSL-leftover lib64 symlink in
REM      fx-quant-stack/.venv that breaks uv on Windows.
REM   3. Runs `uv sync --extra dev` in fx-quant-stack/.
REM   4. Runs `pnpm install` for the dashboard.
REM   5. Verifies the bridge module imports cleanly.
REM
REM Usage: ops\windows\00_dev_setup.bat
REM
REM This is the *only* setup path. Everything else
REM (launch_all.bat, ops\windows\01_sync_python.bat, etc.) assumes the
REM dev bootstrap has already been run successfully.
REM ==========================================================================
setlocal EnableDelayedExpansion

set "REPO_ROOT=%~dp0..\.."
pushd "%REPO_ROOT%" || (
  echo [dev-setup] cannot enter repo root & exit /b 1
)

echo [dev-setup] repo root: %CD%

REM --- 1. tool checks ---------------------------------------------------------
where uv >nul 2>&1
if errorlevel 1 (
  echo [dev-setup] ERROR: uv not found on PATH.
  echo            Install via: powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  popd & exit /b 2
)

where pnpm >nul 2>&1
if errorlevel 1 (
  echo [dev-setup] WARNING: pnpm not found on PATH. Dashboard install will be skipped.
  set "SKIP_PNPM=1"
)

REM --- 2. detect+repair broken fx-quant-stack venv ---------------------------
set "FXVENV=fx-quant-stack\.venv"
set "FXVENV_OK="
if exist "%FXVENV%\Scripts\python.exe" set "FXVENV_OK=1"

if defined FXVENV_OK (
  echo [dev-setup] fx-quant-stack venv looks healthy ^(Scripts\python.exe present^).
) else (
  if exist "%FXVENV%" (
    echo [dev-setup] fx-quant-stack venv missing Scripts\python.exe ^(likely WSL stub^); wiping.
    rmdir /S /Q "%FXVENV%" 2>nul
  ) else (
    echo [dev-setup] fx-quant-stack venv not present; will be created by uv sync.
  )
)

REM --- 3. uv sync -------------------------------------------------------------
echo [dev-setup] running uv sync --extra dev in fx-quant-stack ...
pushd fx-quant-stack || (
  echo [dev-setup] cannot enter fx-quant-stack & popd & exit /b 3
)
REM VIRTUAL_ENV from the parent shell can mislead uv; clear it for this scope.
set "VIRTUAL_ENV="
uv sync --extra dev
set "UV_EXIT=%ERRORLEVEL%"
popd
if not "%UV_EXIT%"=="0" (
  echo [dev-setup] uv sync failed with code %UV_EXIT%
  popd & exit /b 4
)

REM --- 4. pnpm install --------------------------------------------------------
if not defined SKIP_PNPM (
  echo [dev-setup] running pnpm install ...
  call pnpm install --frozen-lockfile
  if errorlevel 1 (
    echo [dev-setup] pnpm install failed
    popd & exit /b 5
  )
) else (
  echo [dev-setup] skipping pnpm install ^(pnpm missing^)
)

REM --- 5. smoke-import the bridge --------------------------------------------
echo [dev-setup] verifying bridge module imports ...
"%FXVENV%\Scripts\python.exe" -c "from fxstack.api import wire; print('[dev-setup] bridge OK protocol=' + wire.BRIDGE_PROTOCOL_VERSION)"
if errorlevel 1 (
  echo [dev-setup] bridge import check failed
  popd & exit /b 6
)

echo.
echo [dev-setup] SUCCESS — environment ready.
echo            Next steps:
echo              launch_all.bat live 10000           start the staged stack
echo              ops\windows\00_preflight.bat        run preflight only
echo              uv run pytest                       run fxstack tests
echo.
popd
endlocal
exit /b 0
