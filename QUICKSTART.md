# FX Trading System - Quick Start Guide

Complete guide to run the chaos/randomness-based FX trading system with full monitoring.

## System Overview

\`\`\`
┌─────────────────┐
│  Dashboard      │  http://localhost:3000
│  (React)        │  Real-time monitoring
└────────┬────────┘
         │
┌────────▼────────┐
│  Bridge Server  │  http://localhost:58710
│  (Flask)        │  State & signal routing
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼──┐  ┌──▼───┐
│Agent │  │ MT4  │
│(Py)  │  │ EA   │
└──────┘  └──────┘
\`\`\`

## Prerequisites

### 1. Python Environment

\`\`\`bash
# Install dependencies
poetry install
poetry install -E hmm  # Optional: HMM regime detection

# Install bridge dependencies
pip install -r requirements.txt
\`\`\`

### 2. MT4 Setup

See [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md) for complete MT4 configuration.

**Quick checklist:**
- ✅ Account 96940 configured
- ✅ Server: IG-LIVE2 (live) or IG-DEMO (demo)
- ✅ WebRequest enabled for `http://127.0.0.1:58710`
- ✅ BridgeEA compiled and ready

### 3. Market Data

Export H1 (1-hour) data from MT4:

\`\`\`bash
mkdir -p data/fx_minis

# In MT4:
# Tools → History Center
# Select symbol → H1 → Export
# Save as: data/fx_minis/SYMBOLNAME.csv

# Example: EURUSD.csv, GBPUSD.csv, etc.
\`\`\`

CSV format:
\`\`\`
time,open,high,low,close
2024-01-01 00:00:00,1.1050,1.1055,1.1048,1.1052
\`\`\`

## Running the System

### Terminal 1: Bridge Server

\`\`\`bash
python bridge_api/bridge.py
\`\`\`

**Expected output:**
\`\`\`
============================================================
MT4 Bridge Server
============================================================
ADDRESS: http://127.0.0.1:58710

Endpoints:
  GET  /v2/commands/poll - MT4 EA polls for commands
  POST /v2/commands      - Python agent posts trade commands
  POST /v2/reports       - MT4 EA posts status updates
  GET  /v2/reports       - View recent EA reports
  GET  /v2/health  - Health check
  GET  /v2/state   - Current trading state

Make sure to add http://127.0.0.1:58710 to MT4 WebRequest whitelist!
============================================================
\`\`\`

### Terminal 2: Trading Agent (with Validation)

\`\`\`bash
poetry run python src/run_fx.py --equity 10000
\`\`\`

**Expected startup:**
\`\`\`
============================================================
AGENT VALIDATION CHECKLIST
============================================================

A. MINI SYMBOLS ONLY
  IG mode: minis determined by 0.10 lot size, not suffix
✓ Mini config - 79 roots, suffixes=[]

G. RISK KNOBS BOUNDED
✓ Risk knobs - All parameters within safe ranges

B. EL MOMENTUM PARAMETERS
✓ EL parameters - window=48, ema=10, lookback=400

... (more checks)

============================================================
VALIDATION RESULT: ✓ PASS
Passed: 7/7
============================================================

============================================================
FX EL HAWKES AGENT - STARTUP
============================================================
Account equity: $10,000.00
Config: src/config/fx_el_minis.yaml
Update interval: 55s

MINI UNIVERSE (79 symbols):
  • EURUSD
  • GBPUSD
  • USDJPY
  ... (full list)
============================================================
\`\`\`

### Terminal 3: Dashboard (Optional)

\`\`\`bash
pnpm install  # First time only
pnpm dev
\`\`\`

Open browser: http://localhost:3000

### Terminal 4: MT4

1. Open any FX chart (e.g., EURUSD, H1 timeframe)
2. Enable **AutoTrading** (Alt+A - toolbar should show green icon)
3. Drag **BridgeEA** from Navigator → Expert Advisors onto chart
4. Verify settings:
   - UseIGMinis: `true`
   - ApiBase: `http://127.0.0.1:58710`
   - Magic: `246810`
5. Click OK

**Check Experts tab:**
\`\`\`
IG MT4 Bridge EA initialized
Account: 96940
Server: IG-LIVE2
Mini mode: ON (0.10 lot)
\`\`\`

## Monitoring Operation

### Agent Console Output

**Every iteration (55 seconds):**
\`\`\`
============================================================
ITERATION 1 - 2025-10-13 14:30:00
============================================================
Loaded market data for 79 symbols

EURUSD: score=0.523, pz=0.612, tilt=0.854 → BUY
EURUSD: SIGNAL BUY - score=0.523, exp_move=0.52%, cost=0.0080%, target=1.00%

GBPUSD: score=0.385, pz=0.450, tilt=0.856 → BUY
GBPUSD: |score|=0.385 < threshold=0.400, rejected

USDJPY: score=-0.412, pz=-0.520, tilt=0.792 → SELL
USDJPY: rejected: cost gate - exp_move=0.041% < 3×cost=0.024%

... (continues for all symbols)
\`\`\`

**Every 10 iterations:**
\`\`\`
REJECTION STATS (last 10 iterations):
  low_score: 145
  cost_gate: 23
  insufficient_bars: 5
  pz_invalid: 0
  tilt_invalid: 0
\`\`\`

### MT4 Experts Tab

**Heartbeat (every second):**
\`\`\`
HEARTBEAT eq=10000.00
\`\`\`

**Trade execution:**
\`\`\`
OK BUY EURUSD ticket=123456 lots=0.10 tp_cash=100.00
\`\`\`

**Cycle management:**
\`\`\`
CYCLE_START eq=10000.00 target=100.00
... (trades execute)
CYCLE_TARGET_HIT eq=10100.00 profit=100.00
\`\`\`

### Dashboard (http://localhost:3000)

**Shows:**
- ✅ System status (connected/disconnected)
- ✅ Live equity with P&L
- ✅ Active decisions (symbol, side, score, price)
- ✅ Equity curve chart
- ✅ Signal metrics (sent vs executed)
- ✅ Activity log with recent events

## Validation Checks

### A. Mini Symbols Only ✓

**Agent startup log should show:**
\`\`\`
MINI UNIVERSE (79 symbols):
  • EURUSD
  • GBPUSD
\`\`\`

For IG: standard names (no suffix) are OK - minis determined by 0.10 lot size.

### B. Signals Respect Randomness ✓

**Look for in agent log:**
\`\`\`
EURUSD: score=0.523, pz=0.612, tilt=0.854 → BUY
\`\`\`

- ✅ pz is finite (not NaN)
- ✅ tilt in [-1, 1]
- ✅ score = pz × tilt
- ✅ side matches sign(score)

### C. Gates Working ✓

**Cost gate rejections:**
\`\`\`
USDJPY: rejected: cost gate - exp_move=0.041% < 3×cost=0.024%
\`\`\`

**Score threshold rejections:**
\`\`\`
GBPUSD: |score|=0.385 < threshold=0.400, rejected
\`\`\`

**Correlation filter:**
\`\`\`
Dropped 2 symbols due to ρ > 0.70
\`\`\`

### D. Target Behavior ✓

**EA log shows 1% TP:**
\`\`\`
OK BUY EURUSD ticket=123 lots=0.10 tp_cash=100.00
\`\`\`

For $10k equity: tp_cash ≈ $100 (1%).

**Basket closes at +1%:**
\`\`\`
CYCLE_START eq=10000.00 target=100.00
CYCLE_TARGET_HIT eq=10100.00 profit=100.00
\`\`\`

## Common Issues & Solutions

### Issue: Validation Fails on Startup

**Error:**
\`\`\`
✗ Risk knobs - target_base_pct 0.05 not in [0.5%, 3%]
VALIDATION RESULT: ✗ FAIL
\`\`\`

**Fix:**
Edit `src/config/fx_el_minis.yaml`, correct the parameter:
\`\`\`yaml
target_base_pct: 0.010  # 1%
\`\`\`

### Issue: No Signals Generated

**Check:**
1. ✅ CSV files exist in `data/fx_minis/`
2. ✅ Sufficient data (400+ bars)
3. ✅ Score threshold not too high

**Debug:**
\`\`\`bash
# Lower threshold temporarily
# In fx_el_minis.yaml:
score_threshold: 0.30  # From 0.40
\`\`\`

### Issue: EA Not Receiving Signals

**Check:**
1. ✅ Bridge running: `curl http://127.0.0.1:58710/v2/health`
2. ✅ WebRequest enabled in MT4
3. ✅ AutoTrading ON (green icon)
4. ✅ EA smiley face showing

**Test bridge:**
\`\`\`bash
curl "http://127.0.0.1:58710/v2/commands/poll?format=line"
# Should return 200 (empty or signal)
\`\`\`

### Issue: Dashboard Not Connecting

**Check:**
1. ✅ Bridge server running on port 58710
2. ✅ Dashboard on port 3000: `pnpm dev`
3. ✅ CORS enabled in bridge (flask-cors installed)

**Browser console:**
Press F12, check for errors. Should see successful fetch requests every 2 seconds.

### Issue: Trades Not Executing

**Check MT4:**
- ✅ Sufficient margin (>$100 per 0.10 lot)
- ✅ Market open (FX trades 24/5)
- ✅ Symbol in Market Watch
- ✅ No connection issues (green indicator)

**EA log errors:**
\`\`\`
ERR order 134  # Not enough money - increase margin
ERR order 4   # Wrong trade operation - check side
\`\`\`

## Testing Mode (Demo Account)

**Always test on demo first!**

\`\`\`bash
# Use IG-DEMO server in MT4
# Start with small "equity" parameter
poetry run python src/run_fx.py --equity 1000
\`\`\`

Run for 24-48 hours, verify:
- ✅ Signals generated correctly
- ✅ Trades execute at 0.10 lot
- ✅ TPs hit when expected
- ✅ Basket closes at +1%
- ✅ No crashes or errors

## Going Live (After Demo Testing)

1. **Switch to IG-LIVE2** server in MT4
2. **Verify account has real funds**
3. **Start with conservative equity:**
   \`\`\`bash
   poetry run python src/run_fx.py --equity 5000  # Even if account has more
   \`\`\`
4. **Monitor closely** for first week
5. **Review daily:**
   - Rejection stats
   - Hit rates
   - Actual vs expected costs

## Performance Monitoring

### Daily

**Check logs for:**
\`\`\`bash
grep "SIGNAL" fx_agent.log | wc -l    # Signals sent
grep "rejected" fx_agent.log | wc -l  # Rejections
\`\`\`

**Rejection ratio:** Should be 80-95% (most setups rejected = good filtering).

### Weekly

**Hit rate calculation:**
\`\`\`python
# Add to analysis script:
trades = parse_ea_log("mt4_experts.log")
wins = sum(1 for t in trades if t.pnl > 0)
hit_rate = wins / len(trades)
print(f"Hit rate: {hit_rate:.1%}")  # Target: 52-55%
\`\`\`

**Coverage check:**
\`\`\`python
# Are uncertainty bands calibrated?
# 60% bands should contain 60% of outcomes
\`\`\`

## Stopping the System

**Safe shutdown order:**

1. **Stop agent:** Ctrl+C in agent terminal
   - Completes current iteration
   - No new signals sent
2. **Let EA finish:** Wait for cycle to complete
   - EA still manages open positions
   - Basket exit happens naturally
3. **Stop EA:** Remove from chart or disable AutoTrading
4. **Stop bridge:** Ctrl+C in bridge terminal
5. **Stop dashboard:** Ctrl+C in dashboard terminal

**Emergency stop:**
- Click "Close All" in MT4 Terminal
- All positions close immediately

## File Structure Reference

\`\`\`
ai-hedge-fund/
├── src/
│   ├── agents/
│   │   ├── fx_el_hawkes_agent.py  # Main strategy
│   │   └── risk_utils.py          # EL, regime, gates
│   ├── execution/
│   │   └── mt4_bridge_client.py   # HTTP client
│   ├── config/
│   │   └── fx_el_minis.yaml       # Configuration
│   ├── validation/
│   │   └── agent_validator.py     # Startup checks
│   └── run_fx.py                  # Main runner
├── bridge_api/
│   └── bridge.py                  # Flask server
├── app/                           # Next.js dashboard
├── MQL4/                          # MT4 files
├── data/fx_minis/                 # H1 CSV data
└── docs/
    └── IG_MT4_SETUP.md           # MT4 guide
\`\`\`

## Next Steps

1. ✅ **Run validation:** `poetry run python src/run_fx.py --equity 10000`
2. ✅ **Check dashboard:** http://localhost:3000
3. ✅ **Attach EA** to MT4 chart
4. ✅ **Monitor for 1 hour** - verify everything flows
5. ✅ **Review checklist:** [VALIDATION_CHECKLIST.md](VALIDATION_CHECKLIST.md)
6. ✅ **Demo test** for 1-2 weeks
7. ✅ **Go live** (if metrics look good)

## Support

- **MT4 Setup:** [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md)
- **Full System Docs:** [FX_TRADING_README.md](FX_TRADING_README.md)
- **Validation Guide:** [VALIDATION_CHECKLIST.md](VALIDATION_CHECKLIST.md)
- **IG Support:** https://www.ig.com/en/mt4

---

**Remember:** This is a chaos/randomness strategy. Expect:
- Most setups rejected (gates working)
- Hit rate ≈ 52-55% (not 70%+)
- Variable pz/tilt (not flatlined)
- Occasional dry spells (normal randomness)

**NOT a get-rich-quick scheme. Educational use only. Trade responsibly.** 🎯
