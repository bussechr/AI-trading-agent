#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

export ROOT
export PYTHONPATH="${PYTHONPATH:-$ROOT}"

if [[ -z "${FXSTACK_DATABASE_URL:-}" ]]; then
  export FXSTACK_DATABASE_URL="postgresql+psycopg://fx:fx@localhost:5432/fxstack"
fi
if [[ -z "${FXSTACK_CANDIDATE_DATABASE_URL:-}" ]]; then
  export FXSTACK_CANDIDATE_DATABASE_URL="postgresql+psycopg://fx:fx@localhost:5432/fxstack_candidate"
fi
if [[ -z "${FXSTACK_PAIRS:-}" ]]; then
  export FXSTACK_PAIRS="EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD"
fi
if [[ -z "${FXSTACK_DUKASCOPY_SOURCE_ROOT:-}" ]]; then
  export FXSTACK_DUKASCOPY_SOURCE_ROOT="$ROOT/fx-quant-stack/data/dukascopy"
fi
if [[ -z "${FXSTACK_DUKASCOPY_FILE_PATTERN:-}" ]]; then
  export FXSTACK_DUKASCOPY_FILE_PATTERN="{pair}_{granularity}.csv"
fi

if [[ -z "${TRADER_BRIDGE_IMPL:-}" ]]; then
  export TRADER_BRIDGE_IMPL="fxstack"
fi
if [[ -z "${TRADER_RUNTIME_IMPL:-}" ]]; then
  export TRADER_RUNTIME_IMPL="fxstack"
fi

if [[ -z "${FXSTACK_REQUIRE_CUDA:-}" ]]; then
  export FXSTACK_REQUIRE_CUDA="1"
fi
if [[ -z "${FXSTACK_XGB_DEVICE:-}" ]]; then
  export FXSTACK_XGB_DEVICE="auto"
fi
if [[ -z "${FXSTACK_XGB_TREE_METHOD:-}" ]]; then
  export FXSTACK_XGB_TREE_METHOD="hist"
fi
if [[ -z "${FXSTACK_XGB_ALLOW_CPU_FALLBACK:-}" ]]; then
  export FXSTACK_XGB_ALLOW_CPU_FALLBACK="1"
fi

if [[ -z "${TRADER_PYTHON_EXE:-}" ]]; then
  if [[ -x "$ROOT/fx-quant-stack/.venv/bin/python" ]]; then
    export TRADER_PYTHON_EXE="$ROOT/fx-quant-stack/.venv/bin/python"
  elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    export TRADER_PYTHON_EXE="$ROOT/.venv/bin/python"
  else
    export TRADER_PYTHON_EXE="python3"
  fi
fi

if [[ -z "${TRADER_PYTEST_EXE:-}" ]]; then
  export TRADER_PYTEST_EXE="$TRADER_PYTHON_EXE"
fi

export FXSTACK_PAIRS_SPACED="${FXSTACK_PAIRS//,/ }"
