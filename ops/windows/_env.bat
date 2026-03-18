@echo off
setlocal

set "ROOT=%~dp0\..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
cd /d "%ROOT%"

if not defined FXSTACK_DATABASE_URL set "FXSTACK_DATABASE_URL=postgresql+psycopg://fx:fx@localhost:5432/fxstack"
if not defined FXSTACK_PAIRS set "FXSTACK_PAIRS=EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD"
if not defined FXSTACK_DATA_PROVIDER set "FXSTACK_DATA_PROVIDER=dukascopy"
if not defined FXSTACK_DUKASCOPY_SOURCE_ROOT set "FXSTACK_DUKASCOPY_SOURCE_ROOT=%ROOT%\fx-quant-stack\data\dukascopy"
if not defined FXSTACK_DUKASCOPY_FILE_PATTERN set "FXSTACK_DUKASCOPY_FILE_PATTERN={pair}_{granularity}.csv"
if not defined FXSTACK_ALLOW_SQLITE set "FXSTACK_ALLOW_SQLITE=0"
if not defined FXSTACK_START_PROFILE set "FXSTACK_START_PROFILE=staged_safe"
if not defined FXSTACK_RUN_FAST_GATE set "FXSTACK_RUN_FAST_GATE=0"
if not defined FXSTACK_RUN_SHADOW_24H set "FXSTACK_RUN_SHADOW_24H=0"
if not defined FXSTACK_REQUIRE_CUDA set "FXSTACK_REQUIRE_CUDA=1"
if not defined FXSTACK_DEEP_MODEL_STALE_HOURS set "FXSTACK_DEEP_MODEL_STALE_HOURS=24"
if not defined FXSTACK_SWING_MODEL_POLICY set "FXSTACK_SWING_MODEL_POLICY=transformer_primary_xgb_fallback"
if not defined FXSTACK_INTRADAY_MODEL_POLICY set "FXSTACK_INTRADAY_MODEL_POLICY=tcn_primary_xgb_fallback"
if not defined TRADER_BRIDGE_IMPL set "TRADER_BRIDGE_IMPL=fxstack"
if not defined TRADER_RUNTIME_IMPL set "TRADER_RUNTIME_IMPL=fxstack"
if not defined TRADER_BRIDGE_PORT set "TRADER_BRIDGE_PORT=58710"

set "FXSTACK_PAIRS_SP=%FXSTACK_PAIRS:,= %"

set "PY_EXE="
if defined TRADER_PYTHON_EXE if exist "%TRADER_PYTHON_EXE%" set "PY_EXE=%TRADER_PYTHON_EXE%"
if not defined PY_EXE if exist "%ROOT%\fx-quant-stack\.venv_win\Scripts\python.exe" set "PY_EXE=%ROOT%\fx-quant-stack\.venv_win\Scripts\python.exe"
if not defined PY_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_EXE if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe" set "PY_EXE=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
if not defined PY_EXE if exist "C:\Python311\python.exe" set "PY_EXE=C:\Python311\python.exe"
if not defined PY_EXE if exist "%ROOT%\fx-quant-stack\.venv\Scripts\python.exe" set "PY_EXE=%ROOT%\fx-quant-stack\.venv\Scripts\python.exe"
if not defined PY_EXE if exist "%ROOT%\.venv\Scripts\python.exe" set "PY_EXE=%ROOT%\.venv\Scripts\python.exe"
if not defined PY_EXE set "PY_EXE=python"

set "TRADER_PYTHON_EXE=%PY_EXE%"
set "PYTHONPATH=%ROOT%"

endlocal & (
  set "ROOT=%ROOT%"
  set "FXSTACK_DATABASE_URL=%FXSTACK_DATABASE_URL%"
  set "FXSTACK_PAIRS=%FXSTACK_PAIRS%"
  set "FXSTACK_PAIRS_SP=%FXSTACK_PAIRS_SP%"
  set "FXSTACK_DATA_PROVIDER=%FXSTACK_DATA_PROVIDER%"
  set "FXSTACK_DUKASCOPY_SOURCE_ROOT=%FXSTACK_DUKASCOPY_SOURCE_ROOT%"
  set "FXSTACK_DUKASCOPY_FILE_PATTERN=%FXSTACK_DUKASCOPY_FILE_PATTERN%"
  set "FXSTACK_ALLOW_SQLITE=%FXSTACK_ALLOW_SQLITE%"
  set "FXSTACK_START_PROFILE=%FXSTACK_START_PROFILE%"
  set "FXSTACK_RUN_FAST_GATE=%FXSTACK_RUN_FAST_GATE%"
  set "FXSTACK_RUN_SHADOW_24H=%FXSTACK_RUN_SHADOW_24H%"
  set "FXSTACK_REQUIRE_CUDA=%FXSTACK_REQUIRE_CUDA%"
  set "FXSTACK_DEEP_MODEL_STALE_HOURS=%FXSTACK_DEEP_MODEL_STALE_HOURS%"
  set "FXSTACK_SWING_MODEL_POLICY=%FXSTACK_SWING_MODEL_POLICY%"
  set "FXSTACK_INTRADAY_MODEL_POLICY=%FXSTACK_INTRADAY_MODEL_POLICY%"
  set "TRADER_BRIDGE_IMPL=%TRADER_BRIDGE_IMPL%"
  set "TRADER_RUNTIME_IMPL=%TRADER_RUNTIME_IMPL%"
  set "TRADER_BRIDGE_PORT=%TRADER_BRIDGE_PORT%"
  set "TRADER_PYTHON_EXE=%TRADER_PYTHON_EXE%"
  set "PYTHONPATH=%PYTHONPATH%"
)

exit /b 0
