# Options Feed Integration Guide

## Overview

This guide explains how to add options-based volatility intelligence to your FX trading agent. The system provides **two paths**:

1. **Live Options Feed (Recommended)**: Calibrate Heston model to real market quotes
2. **Proxy Options (Fallback)**: Build synthetic options from spot statistics

## Why Add Options?

Without options data, you're operating with **realized volatility** only. Options give you:

- **Risk-neutral** (forward-looking) volatility expectations
- **Volatility smile** structure (skew, kurtosis) priced by the market
- **Term structure** of volatility across maturities
- **Regime detection** (cheap vs expensive options)

This improves:
- Entry timing (avoid expensive vol regimes)
- Position sizing (scale down in high IV)
- Risk guards (dynamic stops based on option-implied moves)

## Architecture

```
┌─────────────────────┐
│  Option Provider    │  ← HTTP (live quotes) OR Proxy (synthetic)
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│  Heston Service     │  ← Calibrates, caches JSON
└──────────┬──────────┘
           │
           ↓
┌─────────────────────┐
│  Trading Agent      │  ← Uses IV guards, scalers
└─────────────────────┘
```

## Installation

All required files are already in place:

```
src/
├── marketdata/
│   ├── __init__.py
│   ├── http_fx_options.py      # Live options provider
│   ├── proxy_options.py        # Synthetic chain builder
│   └── proxy_provider.py       # Proxy provider wrapper
├── quant/
│   ├── __init__.py
│   └── iv.py                   # Black-Scholes/GK pricing
└── agents/
    ├── heston_service.py       # Calibration service
    └── heston_integration_examples.py  # Integration examples
```

## Quick Start

### Option 1: Live Options Feed (Recommended)

```python
from agents.fx_el_hawkes_agent import FXELAgent
from agents.heston_service import HestonService
from marketdata.http_fx_options import HTTPFXOptionProvider

# Load your config
cfg = {...}

# Create agent
agent = FXELAgent(cfg)

# Add Heston service with live options
agent.heston = HestonService(
    outdir="data/heston",
    provider=HTTPFXOptionProvider(
        url_template="https://YOUR_API/v1/fx/chain?symbol={symbol}",
        headers={"Authorization": "Bearer YOUR_TOKEN"},
        field_map={
            "K": "strike",
            "T": "tenor_years",
            "cp": "cp",
            "bid": "bid",
            "ask": "ask",
            "S0": "spot",
            "rd": "rd",
            "rf": "rf"
        }
    ),
    recalc_after_secs=18*3600  # recalibrate daily
)
```

### Option 2: Proxy Options (Fallback)

```python
from agents.fx_el_hawkes_agent import FXELAgent
from agents.heston_service import HestonService
from marketdata.proxy_provider import ProxyOptionProvider
import pandas as pd

# Callbacks to fetch your spot data
def get_close_series(symbol_root: str) -> pd.Series:
    df = pd.read_csv(f"data/fx_minis/{symbol_root}.csv")
    return df["close"]

def get_spot(symbol_root: str) -> float:
    return float(get_close_series(symbol_root).iloc[-1])

# Create agent
agent = FXELAgent(cfg)

# Add Heston service with proxy options
agent.heston = HestonService(
    outdir="data/heston",
    provider=ProxyOptionProvider(
        get_close=get_close_series,
        get_s0=get_spot,
        rd=0.05,  # USD rate
        rf=0.03   # foreign rate (adjust per pair)
    ),
    recalc_after_secs=6*3600
)
```

## Using Heston in Your Agent

### 1. Volatility Regime Guard

Add to your `score_symbol()` or `decisions()`:

```python
def score_symbol(self, df: pd.DataFrame, symbol: str) -> tuple[float, dict]:
    # ... existing scoring logic ...
    score, diagnostics = super().score_symbol(df, symbol)
    
    # Adjust for volatility regime
    if hasattr(self, 'heston'):
        regime = self.heston.get_vol_regime(symbol)
        if regime == 'high':
            score *= 0.5  # reduce size in expensive vol
            diagnostics['vol_regime'] = regime
    
    return score, diagnostics
```

### 2. Dynamic Position Sizing

```python
def act(self, equity: float, market_data: dict, **kwargs):
    # ... existing logic ...
    
    for decision in decisions:
        base_size = equity * target_pct
        
        # Scale by implied vol
        if hasattr(self, 'heston'):
            iv = self.heston.get_implied_vol_guard(decision.symbol)
            if iv is not None:
                # Scale inversely: high IV → smaller size
                vol_scaler = 0.10 / max(iv, 0.05)
                base_size *= min(vol_scaler, 2.0)
        
        # Send order...
```

### 3. Cost Gate Enhancement

```python
def act(self, equity: float, market_data: dict, **kwargs):
    # ... in your decision loop ...
    
    if hasattr(self, 'heston'):
        iv = self.heston.get_implied_vol_guard(symbol)
        if iv is not None:
            # Expected move = 1σ daily move
            exp_move = iv / math.sqrt(252)
            
            # Compare to cost
            if exp_move < 3.0 * cost_fraction:
                logger.info(f"{symbol}: skipped - exp_move too small vs cost")
                continue
```

## API Reference

### HTTPFXOptionProvider

Fetches live option chains via REST API.

```python
HTTPFXOptionProvider(
    url_template: str,      # URL with {symbol} placeholder
    headers: dict = None,   # HTTP headers (auth, etc.)
    field_map: dict = None, # Map API fields to standard names
    timeout: float = 10.0   # Request timeout
)
```

**Expected API Response:**

```json
{
  "meta": {
    "S0": 1.0850,
    "rd": 0.05,
    "rf": 0.03
  },
  "rows": [
    {"strike": 1.08, "tenor_years": 0.0833, "cp": "C", "bid": 0.0045, "ask": 0.0047},
    {"strike": 1.09, "tenor_years": 0.0833, "cp": "P", "bid": 0.0032, "ask": 0.0034}
  ]
}
```

### ProxyOptionProvider

Builds synthetic options from spot closes.

```python
ProxyOptionProvider(
    get_close: Callable[[str], pd.Series],  # Returns close prices
    get_s0: Callable[[str], float],         # Returns current spot
    rd: float = 0.0,                        # Domestic rate
    rf: float = 0.0                         # Foreign rate
)
```

### HestonService

Manages calibration lifecycle.

```python
HestonService(
    outdir: str,                    # Cache directory
    provider: OptionProvider,       # HTTP or Proxy
    recalc_after_secs: float = 18*3600  # Recalibration interval
)

# Methods:
.get_params(symbol: str) -> Optional[HestonParams]
.get_implied_vol_guard(symbol: str) -> Optional[float]
.get_vol_regime(symbol: str) -> str  # 'low', 'normal', 'high', 'unknown'
```

## Cached Data Format

Heston parameters are cached as JSON in `{outdir}/{symbol}_heston.json`:

```json
{
  "v0": 0.0121,
  "theta": 0.0121,
  "kappa": 2.0,
  "sigma": 0.3,
  "rho": -0.7,
  "timestamp": 1729123456.789,
  "symbol": "EURUSD"
}
```

## Live vs Proxy: When to Use Each

### Use Live Options (HTTP) When:
- ✅ You have access to options market data
- ✅ You want true risk-neutral calibration
- ✅ You need accurate volatility smile
- ✅ Trading institutional-grade signals

### Use Proxy Options When:
- ✅ No options feed available
- ✅ Need basic vol regime detection
- ✅ Secondary guard/scaler only
- ⚠️ **Not** for primary entry signals

## Performance Notes

- **HTTP Provider**: ~200-500ms per calibration (network + compute)
- **Proxy Provider**: ~50-100ms per calibration (local compute only)
- **Cache Hit**: <1ms (memory) or ~5ms (disk)
- **Recommended**: Calibrate every 12-24 hours (options move slowly)

## Troubleshooting

### "Provider payload missing S0 (or F)"

Your API response needs either `S0` (spot) or `F` (forward). Add to field_map:

```python
field_map={"S0": "spot_price", "F": "forward_price", ...}
```

### "No valid ATM options found for calibration"

Check that your chain includes strikes near the forward price. For proxy mode, ensure you have enough historical closes (>30 bars).

### Calibration takes too long

- Use cached params (check `recalc_after_secs`)
- Reduce chain size (filter to ATM ±10%)
- For proxy mode: reduce history lookback

## Next Steps

1. **Test with dummy data**: Use proxy mode with CSV data
2. **Wire live feed**: Get API credentials, configure HTTP provider
3. **Monitor cache**: Check `data/heston/*.json` updates
4. **Tune guards**: Adjust vol regime thresholds for your strategy
5. **Backtest**: Compare P&L with/without Heston guards

## Support

- Full examples: `src/agents/heston_integration_examples.py`
- Pricing utils: `src/quant/iv.py`
- Provider implementations: `src/marketdata/`

---

**Remember**: Options add a **secondary** layer of intelligence. Your core EL momentum + regime logic remains primary. Use Heston for sizing, guards, and regime awareness—not for generating signals.
