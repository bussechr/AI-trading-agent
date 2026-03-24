@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if /I "%FXSTACK_PACKAGE_MODE%"=="1" (
  if exist "%TRADER_PYTHON_EXE%" (
    echo [sync-python] package mode; bundled python runtime ready.
    exit /b 0
  )
  echo [sync-python] ERROR: bundled python runtime not found: %TRADER_PYTHON_EXE%
  exit /b 2
)

set "HAS_UV=0"
where uv >nul 2>&1 && set "HAS_UV=1"

pushd "fx-quant-stack"
set "ACTIVE_VENV_FILE=%CD%\.venv_win.active"
set "VENV_DIR=.venv_win"
if exist "%ACTIVE_VENV_FILE%" (
  set "ACTIVE_VENV_DIR="
  for /f "usebackq delims=" %%V in ("%ACTIVE_VENV_FILE%") do set "ACTIVE_VENV_DIR=%%V"
  if defined ACTIVE_VENV_DIR if exist "%CD%\!ACTIVE_VENV_DIR!\Scripts\python.exe" set "VENV_DIR=!ACTIVE_VENV_DIR!"
)
set "VENV_PY=%CD%\%VENV_DIR%\Scripts\python.exe"
set "NEED_REBUILD=0"
if exist "%VENV_PY%" (
  set "VENV_VER="
  for /f "tokens=2 delims= " %%V in ('"%VENV_PY%" --version 2^>nul') do set "VENV_VER=%%V"
  if defined VENV_VER (
    if /I not "!VENV_VER:~0,4!"=="3.11" set "NEED_REBUILD=1"
  ) else (
    echo [sync-python] WARN: unable to read existing %VENV_DIR% version; attempting in-place sync.
  )
  if "!NEED_REBUILD!"=="1" (
    echo [sync-python] WARN: existing %VENV_DIR% uses Python !VENV_VER!; rebuilding with Python 3.11...
    call "%ROOT%\ops\windows\90_stop_all.bat" >nul 2>&1
    call :reset_dir "%VENV_DIR%"
    if errorlevel 1 (
      echo [sync-python] WARN: failed to remove incompatible %VENV_DIR%; continuing with existing environment.
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
  set "UV_PROJECT_ENVIRONMENT=%VENV_DIR%"
  uv sync --frozen --python "%VENV_PY%"
  if errorlevel 1 (
    echo [sync-python] WARN: uv sync failed for %VENV_DIR%; attempting side-by-side rebuild.
    call :build_side_by_side_uv_env
    if errorlevel 1 (
      popd
      echo [sync-python] ERROR: uv sync failed.
      exit /b 2
    )
  )
  call :set_active_venv "%VENV_DIR%"
  > "%VENV_DIR%\.fxstack_sync_ok" echo synced_at=%DATE% %TIME%
  set "SYNC_PY=%VENV_PY%"
  for %%P in ("!SYNC_PY!") do (
    popd
    endlocal & set "TRADER_PYTHON_EXE=%%~fP" & echo [sync-python] OK & exit /b 0
  )
)

echo [sync-python] WARN: uv not found; using pip fallback.
set "BASE_PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "BASE_PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined BASE_PY if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe" set "BASE_PY=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
if not defined BASE_PY for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%P"
if not defined BASE_PY call :pick_py311 "C:\Python311\python.exe"
if not defined BASE_PY call :pick_py311 "%TRADER_PYTHON_EXE%"
if not defined BASE_PY (
  popd
  echo [sync-python] ERROR: Python 3.11 not found. Install Python 3.11 and rerun.
  exit /b 2
)
echo [sync-python] using Python 3.11 at %BASE_PY%

set "REBUILD_VENV=0"
if not exist "%VENV_DIR%\Scripts\python.exe" set "REBUILD_VENV=1"
if exist "%VENV_DIR%\Scripts\python.exe" if not exist "%VENV_DIR%\pyvenv.cfg" (
  echo [sync-python] WARN: invalid %VENV_DIR% ^(missing pyvenv.cfg^); rebuilding.
  set "REBUILD_VENV=1"
)
if exist "%VENV_DIR%\Scripts\python.exe" (
  "%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
  if errorlevel 1 (
    echo [sync-python] WARN: invalid %VENV_DIR% ^(pip missing^); rebuilding.
    set "REBUILD_VENV=1"
  )
)
if "%REBUILD_VENV%"=="0" if exist "%VENV_DIR%\Scripts\python.exe" (
  call :venv_health_check "%VENV_DIR%\Scripts\python.exe"
  if errorlevel 1 (
    echo [sync-python] WARN: invalid %VENV_DIR% ^(core package health check failed^); rebuilding.
    set "REBUILD_VENV=1"
  )
)
if "%REBUILD_VENV%"=="0" if exist "%VENV_DIR%\.fxstack_sync_ok" (
  echo [sync-python] reusing healthy %VENV_DIR%.
  call :set_active_venv "%VENV_DIR%"
  set "SYNC_PY=%VENV_PY%"
  for %%P in ("!SYNC_PY!") do (
    popd
    endlocal & set "TRADER_PYTHON_EXE=%%~fP" & echo [sync-python] OK & exit /b 0
  )
)
if "%REBUILD_VENV%"=="1" (
  call "%ROOT%\ops\windows\90_stop_all.bat" >nul 2>&1
  if exist "%VENV_DIR%" (
    call :reset_dir "%VENV_DIR%"
    if errorlevel 1 (
      echo [sync-python] WARN: failed to remove broken %VENV_DIR%; building side-by-side fallback.
      call :allocate_side_by_side_venv
      if errorlevel 1 (
        popd
        echo [sync-python] ERROR: failed to allocate side-by-side fallback venv.
        exit /b 2
      )
      set "VENV_PY=%CD%\%VENV_DIR%\Scripts\python.exe"
    )
  )
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
set "LIGHTWEIGHT_PROFILE=0"
if /I "%FXSTACK_SWING_MODEL_POLICY%"=="xgb_only" if /I "%FXSTACK_INTRADAY_MODEL_POLICY%"=="xgb_only" if "%FXSTACK_REQUIRE_CUDA%"=="0" set "LIGHTWEIGHT_PROFILE=1"
if "%LIGHTWEIGHT_PROFILE%"=="1" (
  echo [sync-python] using lightweight runtime dependency set for xgb_only profile...
  "%VENV_PY%" -m pip install -e . --no-deps
  if errorlevel 1 (
    popd
    echo [sync-python] ERROR: lightweight editable install failed.
    exit /b 2
  )
  "%VENV_PY%" -m pip install ^
    "numpy>=1.26" ^
    "pandas>=2.2" ^
    "scipy>=1.12" ^
    "pyarrow>=15.0" ^
    "pydantic>=2.8" ^
    "pydantic-settings>=2.4" ^
    "pyyaml>=6.0" ^
    "scikit-learn>=1.5" ^
    "joblib>=1.4" ^
    "xgboost>=2.1" ^
    "hmmlearn>=0.3.3" ^
    "sqlalchemy>=2.0" ^
    "psycopg[binary]>=3.2" ^
    "alembic>=1.13" ^
    "fastapi>=0.115" ^
    "uvicorn>=0.30" ^
    "requests>=2.31" ^
    "dukascopy-python>=4.0.1,<5"
  if errorlevel 1 (
    popd
    echo [sync-python] ERROR: lightweight dependency install failed.
    exit /b 2
  )
) else (
  "%VENV_PY%" -m pip install -e .
)
if errorlevel 1 (
  popd
  echo [sync-python] ERROR: fallback install failed.
  exit /b 2
)
> "%VENV_DIR%\.fxstack_sync_ok" echo synced_at=%DATE% %TIME%
call :set_active_venv "%VENV_DIR%"
set "SYNC_PY=%VENV_PY%"
for %%P in ("!SYNC_PY!") do (
  popd
  endlocal & set "TRADER_PYTHON_EXE=%%~fP" & echo [sync-python] OK ^(pip fallback^) & exit /b 0
)

:pick_py311
set "CAND=%~1"
if not exist "%CAND%" exit /b 0
set "CAND_VER="
for /f "tokens=2 delims= " %%V in ('"%CAND%" --version 2^>nul') do set "CAND_VER=%%V"
if /I "%CAND_VER:~0,4%"=="3.11" set "BASE_PY=%CAND%"
exit /b 0

:venv_health_check
set "CHECK_PY=%~1"
if not exist "%CHECK_PY%" exit /b 1
"%CHECK_PY%" -c "import fastapi, pydantic, xgboost, pyarrow; import pandas as pd; assert hasattr(pd, 'read_parquet') and hasattr(pd, '__version__') and getattr(pd, '__file__', None)" >nul 2>&1
exit /b %errorlevel%

:set_active_venv
set "ACTIVE_NAME=%~1"
if not defined ACTIVE_NAME exit /b 1
> "%ACTIVE_VENV_FILE%" echo %ACTIVE_NAME%
exit /b 0

:build_side_by_side_uv_env
set "STAMP="
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%T"
set "NEW_VENV_DIR=.venv_win_%STAMP%_%RANDOM%"
set "NEW_VENV_PY=%CD%\%NEW_VENV_DIR%\Scripts\python.exe"
echo [sync-python] WARN: building clean fallback env %NEW_VENV_DIR%...
uv venv --python 3.11 "%NEW_VENV_DIR%"
if errorlevel 1 exit /b 1
set "UV_PROJECT_ENVIRONMENT=%NEW_VENV_DIR%"
uv sync --frozen --python "%NEW_VENV_PY%"
if errorlevel 1 (
  call :reset_dir "%NEW_VENV_DIR%"
  exit /b 1
)
set "VENV_DIR=%NEW_VENV_DIR%"
set "VENV_PY=%NEW_VENV_PY%"
exit /b 0

:allocate_side_by_side_venv
set "STAMP="
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%T"
set "VENV_DIR=.venv_win_%STAMP%_%RANDOM%"
exit /b 0

:remove_dir
set "TARGET=%~1"
if not exist "%TARGET%" exit /b 0
for /l %%I in (1,1,5) do (
  attrib -r -s -h /s /d "%TARGET%\*" >nul 2>&1
  rmdir /s /q "%TARGET%" >nul 2>&1
  if not exist "%TARGET%" exit /b 0
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)
exit /b 1

:reset_dir
set "TARGET=%~1"
call :remove_dir "%TARGET%"
if not errorlevel 1 exit /b 0
call :quarantine_dir "%TARGET%"
exit /b %errorlevel%

:quarantine_dir
setlocal enabledelayedexpansion
set "TARGET=%~1"
if not exist "%TARGET%" (
  endlocal
  exit /b 0
)
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%T"
set "RENAMED=%TARGET%_quarantine_!STAMP!_%RANDOM%"
move /Y "%TARGET%" "!RENAMED!" >nul 2>&1
if exist "!RENAMED!" (
  echo [sync-python] WARN: quarantined %TARGET% as !RENAMED!.
  endlocal
  exit /b 0
)
endlocal
exit /b 1
