# Heston Options Integration

## Overview

The trading agent now supports optional FX options market data integration for volatility surface calibration using the Heston stochastic volatility model. This provides an additional risk guard by comparing realized volatility against market-implied volatility expectations.

## Two Integration Paths

### 1. Live Options Feed (Recommended for Production)

Use real market option quotes from an HTTP API:

\`\`\`yaml
use_heston_guard: true
heston_provider: "http"
options_url_template: "https://your-api.com/fx/chain?symbol={symbol}"
options_api_key: "your_api_key_here"
\`\`\`

**Benefits:**
- Calibrates to actual market risk-neutral volatility surface
- Institutional-grade volatility estimates
- Captures market sentiment and skew

**Requirements:**
- Access to FX options market data API
- API should return option chains with strikes, tenors, bid/ask prices

### 2. Proxy/Synthetic Options (Fallback)

Build synthetic option chains from spot price statistics:

\`\`\`yaml
use_heston_guard: true
heston_provider: "proxy"
rd: 0.05  # USD risk-free rate
rf: 0.03  # Foreign risk-free rate
\`\`\`

**Benefits:**
- Works with only spot price data (your existing CSV files)
- No external API dependencies
- Reasonable volatility proxy for risk management

**Limitations:**
- Not true market calibration (uses realized vol + skew/kurtosis heuristics)
- Should be treated as secondary guard, not primary signal

## How It Works

1. **Calibration**: Agent fetches option chain (live or synthetic) and calibrates Heston model parameters
2. **Caching**: Parameters cached to `data/heston/{SYMBOL}_heston.json` for 18 hours
3. **Vol Guard**: Before taking a trade, compares current realized vol against Heston's long-term variance (theta)
4. **Rejection**: If realized vol > 1.5 × sqrt(theta), trade is rejected as too volatile

## Configuration Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `use_heston_guard` | `false` | Enable/disable Heston volatility guard |
| `heston_provider` | `"proxy"` | Provider type: `"http"` or `"proxy"` |
| `heston_outdir` | `"data/heston"` | Cache directory for calibrated params |
| `heston_recalc_secs` | `64800` | Recalibration interval (18 hours) |
| `options_url_template` | - | HTTP API endpoint template |
| `options_api_key` | - | API authentication key |
| `rd` | `0.05` | Domestic risk-free rate (proxy mode) |
| `rf` | `0.03` | Foreign risk-free rate (proxy mode) |

## API Response Format

For HTTP provider, your API should return JSON in one of these formats:

**Format 1: With metadata**
\`\`\`json
{
  "meta": {
    "S0": 1.0850,
    "rd": 0.05,
    "rf": 0.03
  },
  "rows": [
    {"K": 1.0800, "T": 0.0833, "cp": "C", "bid": 0.0045, "ask": 0.0047},
    {"K": 1.0900, "T": 0.0833, "cp": "P", "bid": 0.0052, "ask": 0.0054}
  ]
}
\`\`\`

**Format 2: Rows only**
\`\`\`json
[
  {"strike": 1.0800, "tenor_years": 0.0833, "cp": "C", "bid": 0.0045, "ask": 0.0047, "spot": 1.0850, "rd": 0.05, "rf": 0.03}
]
\`\`\`

Use `options_field_map` to map your API's field names to the standard format.

## Monitoring

Check rejection stats in agent logs:

\`\`\`python
logger.info(f"Rejection stats: {agent.rejection_stats}")
# Example: {'heston_vol_guard': 3, 'low_score': 12, 'cost_gate': 5}
\`\`\`

Calibrated parameters are logged:
\`\`\`
INFO: EURUSD: calibrated Heston - v0=0.0089, theta=0.0089, kappa=2.00
\`\`\`

## Disabling

To disable Heston integration and return to pure chaos strategy:

\`\`\`yaml
use_heston_guard: false
\`\`\`

The agent will continue using EL momentum + regime tilt without the volatility guard.
