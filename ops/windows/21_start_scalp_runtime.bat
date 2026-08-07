REM AGENT: ROLE: Launch the signed-release IG-DEMO MTVCLC strategy through the canonical runtime process.
REM AGENT: ENTRYPOINT: `ops/windows/21_start_scalp_runtime.bat --validate|--validate-models|--run|--background [EQUITY] [BRIDGE_PORT]`.
REM AGENT: PRIMARY INPUTS: exact IG MT4 symbol scope plus deployment-owned live-posture settings.
REM AGENT: PRIMARY OUTPUTS: delegates validation/process ownership/readiness to `21_start_runtime.bat` with an exact IG MT4 scope.
REM AGENT: STATE / SIDE EFFECTS: does not start MT4; run/background delegate only to the canonical runtime launcher.
REM AGENT: HANDSHAKES: exact 22-symbol MTVCLC scalp scope + IG-DEMO signed runtime release + hash-pinned cost contract + deployment-owned live posture -> immediate market BUY/SELL broker/account/risk/queue authority; pending orders forbidden.
@echo off
setlocal

REM Load the operator/installed environment once, then freeze it for the
REM delegated canonical launcher. Otherwise 21_start_runtime.bat reloads
REM installed_env.bat after the exact scalp pins below and can silently replace
REM the 22-symbol/demo scope with a stale model-stack deployment scope.
call "%~dp0_env.bat" || exit /b 1
set "FXSTACK_SKIP_INSTALLED_ENV=1"

set "SCALP_IG_MT4_PAIRS=EURUSD,USDJPY,AUDUSD,GBPUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD,AUDJPY,CADJPY,CHFJPY,EURAUD,EURCAD,EURCHF,GBPCAD,GBPCHF,GBPJPY,BTCUSD,ETHUSD,AUDCAD,NZDJPY"
set "FXSTACK_ENTRY_STRATEGY_FAMILY=mtvclc"
set "FXSTACK_PAIRS=%SCALP_IG_MT4_PAIRS%"
set "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST=%SCALP_IG_MT4_PAIRS%"
set "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST=scalp"
set "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST=enter,exit"
REM Scalp lots are capped dynamically by stop-risk, broker max, and margin.
REM Keep the generic 0.10-order and 0.30/0.20 cross-instrument lot ceilings
REM out of this lane so they cannot suppress cash-risk sizing. Position-count
REM limits remain binding and total open stop risk is bounded by those counts.
set "FXSTACK_MAX_ORDER_LOTS=100.0"
set "FXSTACK_RISK_MAX_GROSS_EXPOSURE=100.0"
set "FXSTACK_RISK_MAX_NET_EXPOSURE=100.0"
set "FXSTACK_RUNTIME_LOOP_SLEEP_SECS=1"
set "FXSTACK_PRODUCTION_SCALP_BAR_HISTORY_LIMIT=242"
set "FXSTACK_PRODUCTION_SCALP_COST_CAPTURE_FILE=%ROOT%\artifacts\scalp_research\staging\ig_tick_cost_snapshot_20260804_post_reload\ig_mt4_bid_ask_capture.json"
set "FXSTACK_PRODUCTION_SCALP_COST_CAPTURE_SHA256=2c8b1239113dc76b9c1bd1f55da2010a44ebb1f6020cf358ac2110afcecd6190"
set "FXSTACK_PRODUCTION_SCALP_DEMO_PROBE_ID="
set "FXSTACK_PRODUCTION_SCALP_DEMO_PROBE_SYMBOL="
set "FXSTACK_PRODUCTION_SCALP_DEMO_PROBE_SIDE="

REM This wrapper identifies the strategy and exact broker scope. The canonical
REM launcher owns live/live/armed, shadow-off, process, and readiness admission.
REM The canonical launcher validates the configured IG-DEMO signed runtime release
REM before process reset, and runner startup repeats that public verification.
call "%~dp021_start_runtime.bat" %*
exit /b %errorlevel%
