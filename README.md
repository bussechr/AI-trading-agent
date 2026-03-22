# FX Trading System

**EL Momentum + Regime Filtering Strategy for IG MT4 (Hexagonal Runtime + v2 Protocol)**

## Rebuild Status

- Active strategy rebuild lives in [`fx-quant-stack`](fx-quant-stack/README.md).
- `trader bridge serve` and `trader runtime run` now default to the v2 `fxstack` runtime path.
- Windows one-click production launcher is `launch_all.bat live` and uses the modular scripts under `ops/windows/`.
- Runtime and bridge execution are v2-only (`fxstack`).
- Active Python package surface is `fx-quant-stack/pyproject.toml`; root `pyproject.toml` and `requirements.txt` are legacy compatibility files.

A chaos/randomness-based FX trading system that uses:
- **EL Generalized Momentum** (display variant with z-scored returns)
- **Regime Tilt Filtering** (proxy or HMM-based)
- **Correlation Gates** (limit position clustering)
- **Cost Gates** (expected move > 3× transaction cost)
- **Dynamic Volatility Scaling** (optional)

## Strategy Overview

This system is NOT a simple oscillator - it models randomness and market microstructure:

### Core Algorithm

\`\`\`
score_t = pz_t × tilt_t

where:
  pz_t   = EMA(z-scored returns)  [EL momentum]
  tilt_t = tanh(z-score of MA)    [regime proxy, ∈ [-1, 1]]
\`\`\`

### Execution Rules

- **Universe:** IG MT4 FX pairs (79 symbols), mini contracts only (0.10 lot)
- **Entry:** Market orders when |score| ≥ threshold (default 0.40)
- **Position Sizing:** Minimum lot (0.10 for IG minis)
- **Per-Trade TP:** 1% of equity (volatility-scaled if enabled)
- **Basket TP:** Close all positions when total profit ≥ +1% of cycle start equity
- **Max Concurrent:** 4 positions (configurable)
- **Correlation Limit:** ρ ≤ 0.70 between positions
- **Update Frequency:** H1 bars (every 55 seconds in forward mode)

### Key Features

✅ **Mini Contracts Only** - IG MT4 minis (0.10 lot), no standard or micro  
✅ **Chaos Modeling** - Not a simple oscillator, models randomness properly  
✅ **Cost-Aware** - Expected move must exceed 3× transaction cost  
✅ **Correlation-Aware** - Limits position clustering  
✅ **Regime-Adaptive** - Dynamic targets scale with volatility  
✅ **Real-time Dashboard** - Web UI for monitoring  
✅ **Validation Enforced** - Startup checks ensure proper configuration  

## Quick Start

### 1. Install

\`\`\`bash
# Clone repository
git clone <your-repo-url>
cd ai-hedge-fund

# Authoritative Python environment
cd fx-quant-stack
uv sync --extra dev
cd ..

# Dashboard dependencies
pnpm install
\`\`\`

### 2. Configure MT4

See [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md) for complete MT4 setup.

**Requirements:**
- IG MT4 account (demo or live)
- Account: 96940 (BXAWM reference)
- Server: IG-LIVE2 (live) or IG-DEMO (demo)
- WebRequest enabled for `http://127.0.0.1:58710`
- BridgeEA.mq4 compiled and attached to chart

### 3. Prepare Data

Export H1 (1-hour) data from MT4:

\`\`\`bash
mkdir -p data/fx_minis

# In MT4:
# Tools → History Center
# Select symbol (e.g., EURUSD) → H1 → Export
# Save as: data/fx_minis/EURUSD.csv
\`\`\`

### 4. Run System (Unified CLI)

**Recommended operator path (`http://127.0.0.1:3000`):**
\`\`\`bash
launch_all.bat live 10000
# Open http://127.0.0.1:3000
\`\`\`

**Status / shutdown helpers:**
\`\`\`bash
launch_all.bat status
launch_all.bat stop
\`\`\`

**Manual dashboard start only after a production build exists:**
\`\`\`bash
ops/windows/02_sync_node.bat
ops/windows/22_start_dashboard.bat --run 3000
\`\`\`

**Developer preview only (`http://127.0.0.1:3001`):**
\`\`\`bash
pnpm dev
\`\`\`

**Training / activation using the active `fxstack` package:**
\`\`\`bash
uv run --project fx-quant-stack python -m src.trader.cli stack preflight
uv run --project fx-quant-stack python -m src.trader.cli train all --pair EURUSD --force-retrain
uv run --project fx-quant-stack python -m src.trader.cli models activate --require-all
\`\`\`

**Windows launchers (same CLI under the hood):**
- `launch_all.bat live [EQUITY]`
- `launch_all.bat status`
- `launch_all.bat stop`
- `ops/windows/40_full_scale_e2e_validation.bat [EQUITY]` (full fail-fast training -> live -> gate -> finalization validation)
- `run_full_scale_backtest_gpu.sh [--stage smoke|full ...]` (WSL offline full-pipeline GPU-first backtest)
- `ops/windows/23_start_monitor.bat --background [BRIDGE_PORT] [POLL_SECS]`
- `ops/windows/31_shadow_24h.bat`

**MT4:**
- Open any FX chart (H1 timeframe)
- Enable AutoTrading (Alt+A)
- Drag BridgeEA onto chart
- Verify EA running (smiley face icon)

## System Architecture

\`\`\`
┌─────────────────┐
│  Dashboard      │  http://127.0.0.1:3000
│  (Next.js app/) │  Real-time monitoring
└────────┬────────┘
         │
┌────────▼────────┐
│  Bridge Server  │  http://127.0.0.1:58710
│ (FastAPI fxstack)│ v2 state + command lifecycle
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼──┐  ┌──▼───┐
│Agent │  │ MT4  │
│(Py)  │  │ EA   │
└──────┘  └──────┘
\`\`\`

## Configuration

Edit `src/config/fx_el_minis.yaml`:

\`\`\`yaml
# Symbol universe (all 79 IG FX pairs with mini contracts)
symbols_roots: [EURUSD, GBPUSD, USDJPY, ...]

# Algorithm parameters
el_window: 48              # Momentum lookback (H1 bars)
el_ema_span: 10            # EMA smoothing
score_threshold: 0.40      # Minimum |score| to trade

# Risk management
max_concurrent: 4          # Max simultaneous positions
corr_max: 0.70            # Max pairwise correlation

# Execution
ig_mini_lot_size: 0.10    # IG mini contract size
target_base_pct: 0.010    # Base 1% per trade
use_dynamic_target: true  # Volatility scaling

# Cost model
avg_spread_pips: 0.8      # Average spread estimate
pip_value_per_lot: 1.0    # USD/pip for 0.10 lot
\`\`\`

## Validation & Monitoring

### Automatic Validation

On startup, the system validates:
- ✅ Mini symbols configuration
- ✅ Risk parameters within safe bounds
- ✅ EL momentum parameters sufficient
- ✅ Gate thresholds reasonable
- ✅ Target ranges valid

**Fails if config is unsafe.**

### Real-Time Monitoring

**Agent logs:**
\`\`\`
EURUSD: score=0.523, pz=0.612, tilt=0.854 → BUY
EURUSD: SIGNAL BUY - score=0.523, exp_move=0.52%, cost=0.0080%, target=1.00%

GBPUSD: |score|=0.385 < threshold=0.400, rejected

REJECTION STATS:
  low_score: 145
  cost_gate: 23
\`\`\`

**Dashboard shows:**
- System connection status
- Live equity & P&L
- Active decisions
- Equity curve
- Signal metrics
- Activity log

**MT4 Experts tab:**
\`\`\`
HEARTBEAT eq=10000.00
OK BUY EURUSD ticket=123456 lots=0.10 tp_cash=100.00
CYCLE_START eq=10000.00 target=100.00
CYCLE_TARGET_HIT eq=10100.00 profit=100.00
\`\`\`

## Documentation

- **[Quick Start Guide](QUICKSTART.md)** - Complete setup walkthrough
- **[Full-Scale E2E Runbook](docs/FULL_SCALE_E2E_RUNBOOK.md)** - training-to-execution validation profile
- **[Full-Scale GPU Backtest Runbook](docs/FULL_SCALE_BACKTEST_GPU_RUNBOOK.md)** - WSL offline full-pipeline backtest profile
- **[IG MT4 Setup](docs/IG_MT4_SETUP.md)** - MT4 configuration for IG account 96940
- **[Validation Checklist](VALIDATION_CHECKLIST.md)** - Ensure chaos/randomness modeling
- **[FX Trading README](FX_TRADING_README.md)** - Full system documentation
- **[Shadow Dual-Run Runbook](docs/SHADOW_DUAL_RUN_RUNBOOK.md)** - canary and cutover process
- **[Full Process Audit Runbook](docs/FULL_PROCESS_AUDIT_RUNBOOK.md)** - end-to-end audit and GO/HOLD finalization

## Active Architecture

- `http://127.0.0.1:3000` is the operator-facing dashboard and is served by `next build` + `next start` only.
- `pnpm dev` is reserved for developer preview on `http://127.0.0.1:3001`.
- Live cards use `/api/trading/state` as the truth-first adapter for bridge heartbeat, tick freshness, equity visibility, and signal visibility.
- AI training telemetry remains observe-only and has no execution authority.

## Project Structure

\`\`\`
fx-trading-system/
├── fx-quant-stack/                # v2 models/runtime/api implementation
├── src/trader/                    # compatibility CLI + DB shim
├── ops/                           # Windows/WSL orchestration scripts
├── tools/                         # audit/backtest helpers
├── app/                           # Next.js dashboard
└── MQL4/                          # MT4 EA/utility scripts
\`\`\`

## Testing

### Demo Account (Required Before Live)

\`\`\`bash
# Use IG-DEMO server in MT4
# Start with conservative equity
launch_all.bat live 1000
\`\`\`

**Run for 24-48 hours, verify:**
- ✅ Signals generated correctly  
- ✅ Trades execute at 0.10 lot
- ✅ TPs hit when expected
- ✅ Basket closes at +1%
- ✅ No crashes or errors

### Validation Checklist

See [VALIDATION_CHECKLIST.md](VALIDATION_CHECKLIST.md) for complete checklist covering:
- A. Platform and wiring (mini symbols, lot enforcement, TPs)
- B. Signals respect randomness (pz/tilt/score)
- C. Gates working (cost, correlation, score threshold)
- D. Target behavior (1% per trade, 1% per basket)
- F. Logs and metrics

## Performance Expectations

This is a **chaos/randomness strategy**, expect:

| Metric | Target | Meaning |
|--------|--------|---------|
| **Hit Rate** | 52-55% | Slightly better than random (good!) |
| **Rejection Rate** | 80-95% | Most setups rejected (gates working) |
| **pz Variation** | Continuous | Not flatlined (momentum alive) |
| **tilt Range** | Near-zero median | Not stuck at ±1 (regime filter working) |
| **Max Drawdown** | 5-10% | Normal for 1% per trade |
| **Sharpe Ratio** | 0.8-1.2 | Moderate risk-adjusted return |

**NOT a get-rich-quick scheme.** Expect variability, dry spells, and mean reversion.

## License & Disclaimer

This project is for **educational and research purposes only**.

- ❌ Not financial advice
- ❌ No guarantees of profitability
- ❌ Past performance ≠ future results
- ⚠️ Test thoroughly on demo before live
- ⚠️ Consult a licensed financial advisor

**By using this software, you accept all risks and acknowledge this is for learning purposes.**

## Support

- **MT4 Setup:** [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md)
- **IG Support:** https://www.ig.com/en/mt4
- **IG FX Specs:** https://www.ig.com/en/help-and-support/cfds/fees-and-charges/what-are-igs-forex-cfd-product-details

## Contributing

Contributions welcome! Focus areas:
- Additional regime detection methods (HMM, Markov-switching)
- Alternative momentum indicators
- Risk management enhancements
- Execution improvements

## Credits

Based on the ai-hedge-fund repository structure, adapted for FX trading with:
- EL generalized momentum (display variant)
- Regime filtering (proxy or HMM)
- IG MT4 mini contract execution
- Real-time monitoring dashboard

---

**Trade Responsibly. Test on Demo. Understand the Math.** 🎯
