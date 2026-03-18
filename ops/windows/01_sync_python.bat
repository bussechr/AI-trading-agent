@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "HAS_UV=0"
where uv >nul 2>&1 && set "HAS_UV=1"

pushd "fx-quant-stack"
set "VENV_DIR=.venv_win"
set "VENV_PY=%CD%\%VENV_DIR%\Scripts\python.exe"
if exist "%VENV_PY%" (
  set "VENV_VER="
  for /f %%V in ('"%VENV_PY%" -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2^>nul') do set "VENV_VER=%%V"
  if not "!VENV_VER!"=="3.11" (
    echo [sync-python] WARN: existing %VENV_DIR% uses Python !VENV_VER!; rebuilding with Python 3.11...
    rmdir /s /q "%VENV_DIR%"
    if errorlevel 1 (
      popd
      echo [sync-python] ERROR: failed to remove incompatible %VENV_DIR%.
      exit /b 2
    )
  )
)

if "%HAS_UV%"=="1" (
  if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [sync-python] creating venv via uv...
    uv venv --python 3.11 "%VENV_DIR%"
    if errorlevel 1 (
      popd
      echo [sync-python] ERROR: uv venv failed.
      exit /b 2
    )
  )

  echo [sync-python] syncing locked dependencies via uv...
  uv sync --frozen
  if errorlevel 1 (
    popd
    echo [sync-python] ERROR: uv sync failed.
    exit /b 2
  )
  popd

  echo [sync-python] OK
  exit /b 0
)

echo [sync-python] WARN: uv not found; using pip fallback.
set "BASE_PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "BASE_PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined BASE_PY if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe" set "BASE_PY=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
if not defined BASE_PY call :pick_py311 "C:\Python311\python.exe"
if not defined BASE_PY call :pick_py311 "%TRADER_PYTHON_EXE%"
if not defined BASE_PY (
  popd
  echo [sync-python] ERROR: Python 3.11 not found. Install Python 3.11 and rerun.
  exit /b 2
)
echo [sync-python] using Python 3.11 at %BASE_PY%

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [sync-python] creating venv via %BASE_PY%...
  "%BASE_PY%" -m venv "%VENV_DIR%"
  if errorlevel 1 (
    popd
    echo [sync-python] ERROR: python venv creation failed.
    exit /b 2
  )
)

if not exist "%VENV_PY%" (
  popd
  echo [sync-python] ERROR: missing venv python at %VENV_PY%.
  exit /b 2
)

echo [sync-python] upgrading pip toolchain...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
  popd
  echo [sync-python] ERROR: pip bootstrap failed.
  exit /b 2
)

echo [sync-python] installing fx-quant-stack into venv...
"%VENV_PY%" -m pip install -e .
if errorlevel 1 (
  popd
  echo [sync-python] ERROR: fallback install failed.
  exit /b 2
)
popd

echo [sync-python] OK ^(pip fallback^)
exit /b 0

:pick_py311
set "CAND=%~1"
if not exist "%CAND%" exit /b 0
set "CAND_VER="
for /f %%V in ('"%CAND%" -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2^>nul') do set "CAND_VER=%%V"
if "%CAND_VER%"=="3.11" set "BASE_PY=%CAND%"
exit /b 0
