#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"

STAGE="full"
START_TS_ARG="2024-01-01T00:00:00Z"
END_TS_ARG=""
EVIDENCE_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage)
      STAGE="${2:-}"
      shift 2
      ;;
    --start)
      START_TS_ARG="${2:-}"
      shift 2
      ;;
    --end)
      END_TS_ARG="${2:-}"
      shift 2
      ;;
    --evidence-dir)
      EVIDENCE_DIR="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1"
      exit 2
      ;;
  esac
done

if [[ "$STAGE" != "smoke" && "$STAGE" != "full" ]]; then
  echo "--stage must be one of: smoke, full"
  exit 2
fi

if [[ -z "$END_TS_ARG" ]]; then
  END_TS_ARG="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

if [[ -z "$EVIDENCE_DIR" ]]; then
  STAMP="$(date -u +%Y%m%d_%H%M%S)"
  EVIDENCE_DIR="$ROOT/docs/backtests/$STAMP"
fi
mkdir -p "$EVIDENCE_DIR"
PHASE_RESULTS="$EVIDENCE_DIR/phases.jsonl"
: > "$PHASE_RESULTS"

if [[ "$STAGE" == "smoke" ]]; then
  PAIRS="EURUSD"
  MIN_ROWS_M1=5000
  MIN_ROWS_M5=1000
  MIN_ROWS_M15=500
  MIN_ROWS_H4=100
  MIN_ROWS_D=50
  MAX_ROWS_PER_PAIR=20000
else
  PAIRS="$FXSTACK_PAIRS"
  MIN_ROWS_M1=20000
  MIN_ROWS_M5=10000
  MIN_ROWS_M15=4000
  MIN_ROWS_H4=1000
  MIN_ROWS_D=400
  MAX_ROWS_PER_PAIR=0
fi

IFS=',' read -r -a PAIR_ARRAY <<< "$PAIRS"

FAILED_PHASE=""
RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

on_exit() {
  local rc="$?"
  local run_ended_at
  run_ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  "$TRADER_PYTHON_EXE" - "$PHASE_RESULTS" "$EVIDENCE_DIR/summary.json" "$STAGE" "$FAILED_PHASE" "$rc" "$RUN_STARTED_AT" "$run_ended_at" "$PAIRS" <<'PY'
import json
import sys
from pathlib import Path

phases_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
stage = str(sys.argv[3])
failed_phase = str(sys.argv[4])
rc = int(sys.argv[5])
run_started_at = str(sys.argv[6])
run_ended_at = str(sys.argv[7])
pairs = [x.strip() for x in str(sys.argv[8]).split(",") if x.strip()]

rows = []
if phases_path.exists():
    for line in phases_path.read_text(encoding="utf-8").splitlines():
        txt = line.strip()
        if not txt:
            continue
        try:
            rows.append(json.loads(txt))
        except Exception:
            continue

payload = {
    "generated_at": run_ended_at,
    "run_started_at": run_started_at,
    "run_ended_at": run_ended_at,
    "stage": stage,
    "pairs": pairs,
    "status": "pass" if rc == 0 else "fail",
    "exit_code": rc,
    "failed_phase": failed_phase,
    "phase_count": len(rows),
    "phases": rows,
}
summary_path.parent.mkdir(parents=True, exist_ok=True)
summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps({"summary": str(summary_path), "status": payload["status"], "exit_code": rc}, indent=2))
PY
}
trap on_exit EXIT

append_phase_json() {
  local name="$1"
  local started="$2"
  local ended="$3"
  local rc="$4"
  local log_path="$5"
  local cmd_txt="$6"
  "$TRADER_PYTHON_EXE" - "$PHASE_RESULTS" "$name" "$started" "$ended" "$rc" "$log_path" "$cmd_txt" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
row = {
    "phase": str(sys.argv[2]),
    "started_at": str(sys.argv[3]),
    "ended_at": str(sys.argv[4]),
    "rc": int(sys.argv[5]),
    "log": str(sys.argv[6]),
    "command": str(sys.argv[7]),
}
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, sort_keys=True) + "\n")
PY
}

run_phase() {
  local phase="$1"
  shift
  local started ended rc cmd_txt log_file
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log_file="$EVIDENCE_DIR/${phase}.log"
  cmd_txt="$(printf '%q ' "$@")"

  echo "[phase] $phase"
  set +e
  "$@" >"$log_file" 2>&1
  rc=$?
  set -e
  ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  append_phase_json "$phase" "$started" "$ended" "$rc" "$log_file" "$cmd_txt"

  if [[ "$rc" -ne 0 ]]; then
    FAILED_PHASE="$phase"
    echo "[phase] FAIL: $phase (rc=$rc)"
    tail -n 60 "$log_file" || true
    return "$rc"
  fi

  echo "[phase] OK: $phase"
  return 0
}

ingest_all() {
  for pair in "${PAIR_ARRAY[@]}"; do
    for tf in M1 M5 M15 H4 D; do
      "$TRADER_PYTHON_EXE" -m src.trader.cli data ingest \
        --pair "$pair" \
        --granularity "$tf" \
        --source-root "$FXSTACK_DUKASCOPY_SOURCE_ROOT" \
        --file-pattern "$FXSTACK_DUKASCOPY_FILE_PATTERN" \
        --store-root fx-quant-stack/data/raw
    done
  done
}

features_all() {
  for pair in "${PAIR_ARRAY[@]}"; do
    for tf in M1 M5 M15 H4 D; do
      "$TRADER_PYTHON_EXE" -m src.trader.cli features build \
        --pair "$pair" \
        --timeframe "$tf" \
        --input-root fx-quant-stack/data/raw \
        --output-root fx-quant-stack/data/features
    done
  done
}

labels_all() {
  for pair in "${PAIR_ARRAY[@]}"; do
    "$TRADER_PYTHON_EXE" -m src.trader.cli labels build \
      --pair "$pair" \
      --timeframe D \
      --feature-root fx-quant-stack/data/features \
      --label-root fx-quant-stack/data/labels \
      --horizon-bars 24 \
      --tp-atr-mult 2.0 \
      --sl-atr-mult 1.5

    "$TRADER_PYTHON_EXE" -m src.trader.cli labels build \
      --pair "$pair" \
      --timeframe M5 \
      --feature-root fx-quant-stack/data/features \
      --label-root fx-quant-stack/data/labels \
      --horizon-bars 18 \
      --tp-atr-mult 1.5 \
      --sl-atr-mult 1.2
  done
}

train_all_pairs() {
  for pair in "${PAIR_ARRAY[@]}"; do
    "$TRADER_PYTHON_EXE" -m src.trader.cli train all \
      --pair "$pair" \
      --swing-timeframe D \
      --intraday-timeframe M5 \
      --regime-timeframe H4 \
      --feature-root fx-quant-stack/data/features \
      --label-root fx-quant-stack/data/labels \
      --artifact-root fx-quant-stack/artifacts \
      --training-config fx-quant-stack/configs/training.yaml \
      --registry-root fx-quant-stack/artifacts/registry \
      --deep-stale-hours "${FXSTACK_DEEP_MODEL_STALE_HOURS:-24}"
  done
}

train_deep_stale() {
  local pair_args=()
  for pair in "${PAIR_ARRAY[@]}"; do
    pair_args+=(--pair "$pair")
  done

  "$TRADER_PYTHON_EXE" -m src.trader.cli train deep-stale \
    "${pair_args[@]}" \
    --swing-timeframe D \
    --intraday-timeframe M5 \
    --feature-root fx-quant-stack/data/features \
    --label-root fx-quant-stack/data/labels \
    --artifact-root fx-quant-stack/artifacts \
    --stale-hours "${FXSTACK_DEEP_MODEL_STALE_HOURS:-24}"
}

activate_models() {
  local pair_args=()
  for pair in "${PAIR_ARRAY[@]}"; do
    pair_args+=(--pair "$pair")
  done

  "$TRADER_PYTHON_EXE" -m src.trader.cli models activate \
    --registry-root fx-quant-stack/artifacts/registry \
    --manifest fx-quant-stack/artifacts/active_models.json \
    --require-all \
    "${pair_args[@]}"
}

run_targeted_tests() {
  if [[ "$STAGE" == "smoke" ]]; then
    "$TRADER_PYTEST_EXE" -m pytest -s \
      tests/test_trader_cli.py \
      tests/test_trader_cli_fxstack_commands.py \
      tests/test_audit_tools.py

    "$TRADER_PYTEST_EXE" -m pytest -s \
      fx-quant-stack/tests/test_dukascopy_ingest.py \
      fx-quant-stack/tests/test_features.py \
      fx-quant-stack/tests/test_model_activation.py
  else
    "$TRADER_PYTEST_EXE" -m pytest -s \
      tests/test_trader_cli_fxstack_commands.py \
      tests/test_runtime_service_v2.py \
      tests/test_shadow_dual_run_tool.py

    "$TRADER_PYTEST_EXE" -m pytest -s \
      fx-quant-stack/tests/test_dukascopy_ingest.py \
      fx-quant-stack/tests/test_features.py \
      fx-quant-stack/tests/test_model_activation.py \
      fx-quant-stack/tests/test_runtime_policy_router.py \
      fx-quant-stack/tests/test_api_contract.py
  fi
}

backtest_smoke() {
  for pair in "${PAIR_ARRAY[@]}"; do
    "$TRADER_PYTHON_EXE" -m src.trader.cli backtest run --pair "$pair" --timeframe M5 --feature-root fx-quant-stack/data/features
  done
}

backtest_full() {
  local extra_args=()
  if [[ "$MAX_ROWS_PER_PAIR" -gt 0 ]]; then
    extra_args+=(--max-rows-per-pair "$MAX_ROWS_PER_PAIR")
  fi
  if [[ "$STAGE" == "full" ]]; then
    extra_args+=(--require-nonzero-trades)
  fi

  "$TRADER_PYTHON_EXE" -m src.trader.cli backtest full -- \
    --pairs "$PAIRS" \
    --timeframe M5 \
    --feature-root fx-quant-stack/data/features \
    --artifact-root fx-quant-stack/artifacts \
    --out-dir "$EVIDENCE_DIR/backtest_full" \
    "${extra_args[@]}"
}

echo "============================================================"
echo " FULL-SCALE GPU-FIRST BACKTEST (WSL, OFFLINE E2E)"
echo "============================================================"
echo " stage:   $STAGE"
echo " pairs:   $PAIRS"
echo " start:   $START_TS_ARG"
echo " end:     $END_TS_ARG"
echo " evidence:$EVIDENCE_DIR"
echo " python:  $TRADER_PYTHON_EXE"
echo "============================================================"

run_phase sync_python bash -lc "cd '$ROOT/fx-quant-stack' && uv sync --frozen --extra dev"
run_phase preflight "$TRADER_PYTHON_EXE" -m src.trader.cli stack preflight
run_phase gpu_check "$TRADER_PYTHON_EXE" -m src.trader.cli stack gpu-check

run_phase data_fetch "$TRADER_PYTHON_EXE" -m src.trader.cli data fetch-dukascopy-matrix -- \
  --source-root "$FXSTACK_DUKASCOPY_SOURCE_ROOT" \
  --pairs "$PAIRS" \
  --timeframes "M1,M5,M15,H4,D" \
  --start "$START_TS_ARG" \
  --end "$END_TS_ARG" \
  --resume \
  --out "$EVIDENCE_DIR/phase_data_fetch.json"

run_phase data_gate "$TRADER_PYTHON_EXE" -m src.trader.cli audit dukascopy-gate -- \
  --source-root "$FXSTACK_DUKASCOPY_SOURCE_ROOT" \
  --pairs "$PAIRS" \
  --timeframes "M1,M5,M15,H4,D" \
  --file-pattern "$FXSTACK_DUKASCOPY_FILE_PATTERN" \
  --min-rows-m1 "$MIN_ROWS_M1" \
  --min-rows-m5 "$MIN_ROWS_M5" \
  --min-rows-m15 "$MIN_ROWS_M15" \
  --min-rows-h4 "$MIN_ROWS_H4" \
  --min-rows-d "$MIN_ROWS_D" \
  --out "$EVIDENCE_DIR/phase_data_gate.json"

run_phase ingest ingest_all
run_phase features features_all
run_phase labels labels_all
run_phase train_all train_all_pairs
run_phase train_deep_stale train_deep_stale
run_phase activate_models activate_models
run_phase tests run_targeted_tests
run_phase backtest_smoke backtest_smoke
run_phase backtest_full backtest_full

echo "============================================================"
echo " OFFLINE E2E BACKTEST COMPLETE"
echo "============================================================"
echo " summary: $EVIDENCE_DIR/summary.json"
echo "============================================================"
