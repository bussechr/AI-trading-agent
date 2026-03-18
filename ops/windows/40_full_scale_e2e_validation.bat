@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "EQUITY=%~1"
if not defined EQUITY set "EQUITY=10000"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "EVIDENCE_DIR=docs\e2e\%STAMP%"
if not exist "%EVIDENCE_DIR%" mkdir "%EVIDENCE_DIR%" >nul 2>&1

rem Enforce validation profile contract.
set "TRADER_BRIDGE_IMPL=fxstack"
set "TRADER_RUNTIME_IMPL=fxstack"
set "FXSTACK_REQUIRE_CUDA=0"
set "FXSTACK_PAIRS=EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD"
set "FXSTACK_PAIRS_SP=%FXSTACK_PAIRS:,= %"
if not defined FXSTACK_CANDIDATE_BRIDGE_PORT set "FXSTACK_CANDIDATE_BRIDGE_PORT=58711"
if not defined FXSTACK_CANDIDATE_DATABASE_URL set "FXSTACK_CANDIDATE_DATABASE_URL=postgresql+psycopg://fx:fx@localhost:5432/fxstack_candidate"

set "BASELINE_URL=http://127.0.0.1:58710"
set "CANDIDATE_URL=http://127.0.0.1:%FXSTACK_CANDIDATE_BRIDGE_PORT%"
set "OUT_DIR=docs"
set "DURATION_SECS=900"

echo ============================================================
echo  FULL-SCALE E2E VALIDATION (v2 REQUIRED)
echo ============================================================
echo  Equity: %EQUITY%
echo  Pairs: %FXSTACK_PAIRS%
echo  Require CUDA: %FXSTACK_REQUIRE_CUDA%
echo  Baseline URL: %BASELINE_URL%
echo  Candidate URL: %CANDIDATE_URL%
echo  Evidence: %EVIDENCE_DIR%
echo ============================================================

echo [phase-0] stop all stacks...
call "%~dp090_stop_all.bat"
if errorlevel 1 goto fail

echo [phase-0] stack preflight (CPU profile)...
call "%~dp000_preflight.bat"
if errorlevel 1 goto fail

echo [phase-1] python sync...
call "%~dp001_sync_python.bat"
if errorlevel 1 goto fail

echo [phase-1] node sync/build...
call "%~dp002_sync_node.bat"
if errorlevel 1 goto fail

echo [phase-1] postgres start...
call "%~dp003_postgres_start.bat"
if errorlevel 1 goto fail

echo [phase-1] db migrate + verify...
call "%~dp004_db_migrate.bat"
if errorlevel 1 goto fail

echo [phase-1] candidate db migrate + verify...
set "BASELINE_DB_URL=%FXSTACK_DATABASE_URL%"
set "FXSTACK_DATABASE_URL=%FXSTACK_CANDIDATE_DATABASE_URL%"
call "%~dp004_db_migrate.bat"
set "FXSTACK_DATABASE_URL=%BASELINE_DB_URL%"
if errorlevel 1 goto fail

echo [phase-2] dukascopy data coverage gate...
"%TRADER_PYTHON_EXE%" -m src.trader.cli audit dukascopy-gate -- --source-root "%FXSTACK_DUKASCOPY_SOURCE_ROOT%" --pairs "%FXSTACK_PAIRS%" --timeframes "M1,M5,M15,H4,D" --file-pattern "%FXSTACK_DUKASCOPY_FILE_PATTERN%" --min-rows-m1 20000 --min-rows-m5 10000 --min-rows-m15 4000 --min-rows-h4 1000 --min-rows-d 400 --out "%EVIDENCE_DIR%\phase2_data_gate.json"
if errorlevel 1 goto fail

echo [phase-3] ingest all pairs/timeframes...
call "%~dp010_ingest_all.bat"
if errorlevel 1 goto fail

echo [phase-3] build features...
call "%~dp011_features_all.bat"
if errorlevel 1 goto fail

echo [phase-3] build labels...
call "%~dp012_labels_all.bat"
if errorlevel 1 goto fail

echo [phase-4] train all models...
call "%~dp013_train_all.bat"
if errorlevel 1 goto fail

echo [phase-4] deep stale train...
call "%~dp018_train_deep_stale.bat"
if errorlevel 1 goto fail

echo [phase-4] activate models...
call "%~dp014_activate_models.bat"
if errorlevel 1 goto fail

echo [phase-5] backtest smoke...
call "%~dp015_backtest_smoke.bat"
if errorlevel 1 goto fail

echo [phase-5] root compatibility tests...
"%TRADER_PYTHON_EXE%" -m pytest tests/test_trader_cli_fxstack_commands.py tests/test_runtime_service_v2.py tests/test_shadow_dual_run_tool.py
if errorlevel 1 goto fail

echo [phase-5] fxstack targeted tests...
"%TRADER_PYTHON_EXE%" -m pytest fx-quant-stack/tests/test_dukascopy_ingest.py fx-quant-stack/tests/test_features.py fx-quant-stack/tests/test_model_activation.py fx-quant-stack/tests/test_runtime_policy_router.py fx-quant-stack/tests/test_api_contract.py
if errorlevel 1 goto fail

echo [phase-6] start baseline bridge/runtime/dashboard/monitor...
call "%~dp020_start_bridge.bat" --background 58710
if errorlevel 1 goto fail
call "%~dp021_start_runtime.bat" --background %EQUITY% 58710
if errorlevel 1 goto fail
call "%~dp022_start_dashboard.bat" --background 3000
if errorlevel 1 goto fail
call "%~dp023_start_monitor.bat" --background 58710 2
if errorlevel 1 goto fail

echo [phase-6] baseline live stack check (requires MT4 terminal A on :58710)...
"%TRADER_PYTHON_EXE%" -m src.trader.cli audit live-stack-check -- --base-url %BASELINE_URL% --timeout-secs 2100 --poll-secs 2 --min-heartbeat-advances 20 --min-observation-secs 1800 --require-ticks --require-acked-command --command CLOSE_ALL --symbol EURUSD --out "%EVIDENCE_DIR%\phase6_baseline_live_check.json"
if errorlevel 1 goto fail

echo [phase-7] start candidate stack...
call "%~dp024_start_candidate_stack.bat"
if errorlevel 1 goto fail

echo [phase-7] candidate live stack check (requires MT4 terminal B on :%FXSTACK_CANDIDATE_BRIDGE_PORT%)...
"%TRADER_PYTHON_EXE%" -m src.trader.cli audit live-stack-check -- --base-url %CANDIDATE_URL% --timeout-secs 300 --poll-secs 2 --require-ticks --require-acked-command --command CLOSE_ALL --symbol EURUSD --out "%EVIDENCE_DIR%\phase7_candidate_live_check.json"
if errorlevel 1 goto fail

echo [phase-8] run 15m fast gate...
set "BASELINE_URL=%BASELINE_URL%"
set "CANDIDATE_URL=%CANDIDATE_URL%"
set "OUT_DIR=%OUT_DIR%"
set "DURATION_SECS=900"
call "%~dp030_fast_gate_15m.bat"
if errorlevel 1 goto fail

echo [phase-8] run 24h shadow gate...
set "DURATION_SECS=86400"
call "%~dp031_shadow_24h.bat"
if errorlevel 1 goto fail

echo [phase-9] refresh full-process audit evidence...
"%TRADER_PYTHON_EXE%" -m src.trader.cli audit full-process -- --evidence-root docs/audit --runtime-db data/state/runtime_v2.db --audit-dir data/state/audit
if errorlevel 1 goto fail

echo [phase-9] finalize GO/HOLD...
call "%~dp032_finalize_audit.bat"
if errorlevel 1 goto fail

echo ============================================================
echo  E2E VALIDATION COMPLETE
echo ============================================================
echo  Evidence dir: %EVIDENCE_DIR%
echo  Audit dir:    docs\audit
echo ============================================================
exit /b 0

:fail
echo ============================================================
echo  E2E VALIDATION FAILED - triggering controlled stop.
echo ============================================================
call "%~dp090_stop_all.bat"
echo  Review evidence/logs:
echo    %EVIDENCE_DIR%
echo ============================================================
exit /b 2
