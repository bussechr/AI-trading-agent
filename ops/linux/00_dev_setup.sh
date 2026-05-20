#!/usr/bin/env bash
# ==========================================================================
# Canonical dev bootstrap for Linux / WSL. Idempotent.
#
# Brings a fresh checkout to a known-good developer state:
#   1. Verifies uv and pnpm are installed.
#   2. Detects + repairs Windows-leftover `Scripts/` layout in
#      fx-quant-stack/.venv that breaks uv on Linux.
#   3. Runs `uv sync --extra dev` in fx-quant-stack/.
#   4. Runs `pnpm install` for the dashboard.
#   5. Verifies the bridge module imports cleanly.
#
# Usage: ops/linux/00_dev_setup.sh
#
# This is the *only* setup path on Linux/WSL. Everything else assumes the
# dev bootstrap has already been run successfully.
# ==========================================================================
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"
echo "[dev-setup] repo root: ${REPO_ROOT}"

# --- 1. tool checks --------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "[dev-setup] ERROR: uv not found on PATH."
  echo "           Install via: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 2
fi

SKIP_PNPM=""
if ! command -v pnpm >/dev/null 2>&1; then
  echo "[dev-setup] WARNING: pnpm not found; dashboard install will be skipped."
  SKIP_PNPM=1
fi

# --- 2. detect + repair broken fx-quant-stack venv ------------------------
FXVENV="fx-quant-stack/.venv"
if [[ -x "${FXVENV}/bin/python" ]]; then
  echo "[dev-setup] fx-quant-stack venv looks healthy (bin/python present)."
elif [[ -d "${FXVENV}" ]]; then
  echo "[dev-setup] fx-quant-stack venv exists but is missing bin/python (likely a Windows stub); wiping."
  rm -rf "${FXVENV}"
else
  echo "[dev-setup] fx-quant-stack venv not present; will be created by uv sync."
fi

# --- 3. uv sync ------------------------------------------------------------
echo "[dev-setup] running uv sync --extra dev in fx-quant-stack ..."
(
  cd fx-quant-stack
  # VIRTUAL_ENV from the parent shell can mislead uv; clear it for this scope.
  unset VIRTUAL_ENV
  uv sync --extra dev
)

# --- 4. pnpm install -------------------------------------------------------
if [[ -z "${SKIP_PNPM}" ]]; then
  echo "[dev-setup] running pnpm install ..."
  pnpm install --frozen-lockfile
else
  echo "[dev-setup] skipping pnpm install (pnpm missing)"
fi

# --- 5. smoke-import the bridge -------------------------------------------
echo "[dev-setup] verifying bridge module imports ..."
"${FXVENV}/bin/python" -c "from fxstack.api import wire; print('[dev-setup] bridge OK protocol=' + wire.BRIDGE_PROTOCOL_VERSION)"

cat <<'EOF'

[dev-setup] SUCCESS — environment ready.
           Next steps:
             ops/windows/launch_all.bat live 10000   (Windows operator path)
             uv run --project fx-quant-stack pytest  run fxstack tests
EOF
