@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [ingest] pairs: %FXSTACK_PAIRS%
for %%P in (%FXSTACK_PAIRS_SP%) do (
  for %%T in (M1 M5 M15 H4 D) do (
    echo [ingest] %%P %%T
    "%TRADER_PYTHON_EXE%" -m src.trader.cli data ingest --pair %%P --granularity %%T --source-root "%FXSTACK_DUKASCOPY_SOURCE_ROOT%" --file-pattern "%FXSTACK_DUKASCOPY_FILE_PATTERN%" --store-root fx-quant-stack/data/raw
    if errorlevel 1 (
      echo [ingest] ERROR: failed for %%P %%T
      exit /b 2
    )
    timeout /t 1 /nobreak >nul
  )
)

echo [ingest] OK
exit /b 0
