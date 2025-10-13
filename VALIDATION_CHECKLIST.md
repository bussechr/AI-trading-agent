# FX Trading Agent Validation Checklist

This checklist ensures the agent models chaos and randomness properly, not just oscillations.

## Automated Checks (Run on Startup)

The system automatically validates on startup via `AgentValidator`:

```bash
poetry run python src/run_fx.py --equity 10000
# Runs validation automatically, fails if issues found
```

### What's Checked Automatically

✅ **A. Mini Symbols Only** - Config has correct suffixes and roots  
✅ **G. Risk Knobs Bounded** - All parameters within safe ranges  
✅ **B. EL Parameters** - Window sizes and lookback sufficient  
✅ **C. Gate Thresholds** - Cost gates configured reasonably  
✅ **D. Target Ranges** - Volatility and target parameters valid  

## Runtime Checks (Logged During Trading)

The agent logs these continuously:

### B. Signals Respect Randomness

**Check logs for:**
- `pz={value}` - Should be finite, not NaN
- `tilt={value}` - Should be in [-1, 1], not stuck at extremes
- `score={value}` - Should equal pz × tilt
- Side matches `sign(score)` - BUY when score > 0

**Example:**
```
EURUSD.MINI: score=0.523, pz=0.612, tilt=0.854 → BUY
```

### C. Gates Working

**Cost Gate:**
```
GBPUSD.MINI: rejected: cost gate - exp_move=0.045% < 3×cost=0.024%
```

**Score Threshold:**
```
USDJPY.MINI: |score|=0.385 < threshold=0.400, rejected
```

**Correlation Filter:**
```
Correlation matrix saved, dropped 2 symbols due to ρ > 0.70
```

## Manual Verification Steps

### 1. Platform and Wiring

**A. Mini symbols only**

Check startup log:
```
MINI UNIVERSE (79 symbols):
  • EURUSD.MINI
  • GBPUSD.MINI
  ...
```

Should contain ONLY minis (or for IG: standard names traded at 0.10 lot).

**MT4 EA min-lot enforcement:**

In MT4 Experts tab, look for:
```
OK BUY EURUSD ticket=123456 lots=0.10 tp_cash=100.00
```

Lots should equal `MODE_MINLOT` (0.10 for IG minis).

**Bridge health:**

```bash
curl http://127.0.0.1:5000/health
# Should return: {"status": "healthy", ...}
```

### 2. Target and Exit Behavior

**D. Per-trade 1% TP:**

In EA log:
```
OK BUY EURUSD ticket=123 lots=0.10 tp_cash=100.00
```

For $10,000 equity, tp_cash should be ~$100 (1%).

**D. Cycle 1% basket exit:**

Sequence in EA log:
```
CYCLE_START eq=10000.00 target=100.00
... (trades execute)
CYCLE_TARGET_HIT eq=10100.00 profit=100.00
```

Equity gained ≥ 1% of cycle start → all positions close.

**D. Dynamic target (if enabled):**

In agent log:
```
target={value}%
```

Should be 0.7% to 1.4% under normal volatility (not 0 or huge).

### 3. Rejection Statistics

Every 10 iterations, agent logs:
```
REJECTION STATS:
  low_score: 45
  cost_gate: 12
  insufficient_bars: 3
  pz_invalid: 0
```

**Good:** Most rejections are `low_score` or `cost_gate` (gates working).  
**Bad:** Many `pz_invalid` or `tilt_invalid` (indicators broken).

### 4. Decision Log Audit

Agent stores last 1000 decisions in memory. Access via:

```python
# In Python console or add to dashboard
agent.decision_log[-10:]  # Last 10 decisions
```

Each entry has:
- `symbol`, `time`, `pz`, `tilt`, `score`, `vol`

Verify:
- pz values vary (not flatlined)
- tilt median ≈ 0 (not stuck at ±1)
- score = pz × tilt (formula correct)

## Dashboard Monitoring

The web dashboard at http://localhost:3000 shows:

- **System Status** - Heartbeat, connection health
- **Active Decisions** - Current signals with pz/tilt/score
- **Rejection Stats** - Visualized rejection reasons
- **Equity Curve** - Real-time performance
- **Activity Log** - Recent EA events

**Check:**
- ✅ Connection indicator green
- ✅ Heartbeat updates every ~1 second
- ✅ Decisions show realistic scores (not all 0 or 1)
- ✅ Activity log flows continuously

## MT4 Specific Checks

### H. EA Configuration

In MT4, after attaching EA, check **Inputs** tab:

```
UseIGMinis = true
Magic = 246810
ApiBase = http://127.0.0.1:5000
```

**WebRequest enabled:**
Tools → Options → Expert Advisors:
- ☑ Allow WebRequest for listed URL
- URL: `http://127.0.0.1:5000`

### H. Margin Check

If EA refuses order:
```
ERR order 134  # Not enough money
```

Check:
- Free margin > $100 per mini
- Leverage sufficient (1:50+)

## Failure Mode Tests

### I. Bridge Down Test

1. Stop bridge: Ctrl+C in bridge terminal
2. Check EA still manages cycle:
```
CYCLE_TARGET_HIT eq=10100.00
# Should still close at +1% without Python
```

3. Restart bridge - trading resumes

### I. Data Gap Test

Delete one CSV temporarily:
```bash
mv data/fx_minis/EURUSD.csv /tmp/
```

Agent log should show:
```
rejected: insufficient_bars
```

Symbol skipped, no crash.

## Performance Metrics (Weekly Check)

### F. Coverage and Hit Rates

Not auto-logged yet, but implement:

```python
# Add to agent after 1 week:
trade_wins = sum(1 for t in trades if t.pnl > 0)
hit_rate = trade_wins / len(trades)
# Should be 0.52-0.55
```

### F. Cost Sanity

Compare realized spreads to config:
```python
avg_realized_spread = sum(t.spread for t in trades) / len(trades)
# Should match avg_spread_pips ± 20%
```

## Configuration Safety

### G. Validated on Startup

These are checked automatically:

| Parameter | Safe Range | Why |
|-----------|------------|-----|
| target_base_pct | 0.5% - 3% | Too low = grind, too high = gamma risk |
| corr_max | 0.5 - 0.9 | Too low = few trades, too high = clustering |
| score_threshold | 0.2 - 0.8 | Too low = noise trades, too high = rare signals |
| max_concurrent | 1 - 10 | Breadth cap for mini contracts |
| el_window | ≥ 20 | Need enough data for statistics |

### G. No Standard Symbols

If you accidentally put `EURUSD.csv` (not mini) in `data/fx_minis/`:

**IG mode (no suffixes):** Agent accepts it (OK, traded as mini at 0.10)  
**Generic mode (with suffixes):** Agent rejects it (good, not a mini)

## Quick Diagnostic Commands

```bash
# Check bridge health
curl http://127.0.0.1:5000/health

# Check recent reports
curl http://127.0.0.1:5000/reports | jq '.reports[-5:]'

# Check trading state
curl http://127.0.0.1:5000/state | jq

# View agent logs
tail -f fx_agent.log  # If logging to file

# Dashboard
open http://localhost:3000
```

## Summary - What "Chaos Strategy" Means

**✅ You're modeling randomness if:**
- Score varies continuously (not binary 0/1)
- Tilt distribution is centered (not stuck at extremes)
- Cost gate rejects low-edge setups
- Correlation filter limits clustering
- Hit rate ≈ 52-55%, not 70%+
- Targets scale with volatility

**❌ You're just oscillating if:**
- Score is always above threshold (no gate working)
- All signals execute (no cost/edge filter)
- Positions are 100% correlated (no diversification)
- Hit rate > 60% (overfitting, not chaos)
- Flatlined pz or stuck tilt (broken indicators)

## Recommended Schedule

- **Pre-session:** Run validation (automatic)
- **Hourly:** Check dashboard (quick glance)
- **Daily:** Review rejection stats in logs
- **Weekly:** Audit decision log, check hit rates
- **Monthly:** Coverage/PIT analysis (implement metrics)

---

**Golden Rule:** If validation fails on startup, FIX IT. Don't bypass with `--skip-validation` unless you know exactly why it's failing and it's safe.
