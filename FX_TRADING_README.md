# FX EL Trading System for IG MT4

This system implements an **EL momentum + regime filtering** trading strategy specifically designed for **IG's MT4 platform** using **mini FX contracts** (0.10 lot size).

## Quick Start

### 1. Install Dependencies

\`\`\`bash
# Install Python dependencies
poetry install

# Optional: Install HMM support for advanced regime detection
poetry install -E hmm

# Install bridge API dependencies
pip install -r bridge_api/requirements.txt
\`\`\`

### 2. Configure IG MT4 Account

**Account Details:**
- MT4 Login: **96940**
- Account Reference: **BXAWM**
- Server: **IG-LIVE2** (live) or **IG-DEMO** (demo)

See [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md) for complete MT4 setup instructions.

### 3. Install MT4 Expert Advisors

Copy files to your MT4 data folder (File → Open Data Folder):

\`\`\`bash
# Copy EA files
cp MQL4/Include/BridgeUtils.mqh [MT4_DATA]/MQL4/Include/
cp MQL4/Experts/BridgeEA.mq4 [MT4_DATA]/MQL4/Experts/
cp MQL4/Experts/SymbolScanner.mq4 [MT4_DATA]/MQL4/Experts/

# Compile in MetaEditor (F4 in MT4)
\`\`\`

**Critical:** Enable WebRequest in MT4:
- Tools → Options → Expert Advisors
- Check "Allow WebRequest for listed URL"
- Add: `http://127.0.0.1:5000`

### 4. Prepare Market Data

Export H1 (1-hour) data from MT4 for symbols you want to trade:

\`\`\`bash
mkdir -p data/fx_minis

# In MT4:
# Tools → History Center
# Select symbol (e.g., EURUSD), timeframe H1
# Export → save as data/fx_minis/EURUSD.csv
\`\`\`

CSV format:
\`\`\`
time,open,high,low,close
2024-01-01 00:00:00,1.1050,1.1055,1.1048,1.1052
...
\`\`\`

### 5. Run the System

**Terminal 1 - Bridge Server:**
\`\`\`bash
python bridge_api/bridge.py
\`\`\`

**Terminal 2 - Trading Agent:**
\`\`\`bash
poetry run python src/run_fx.py --equity 10000
\`\`\`

**MT4:**
1. Open any FX chart (H1 timeframe)
2. Enable AutoTrading (Alt+A)
3. Drag **BridgeEA** onto chart
4. Verify EA is running (smiley face icon)

## System Architecture

\`\`\`
┌─────────────────┐
│  Python Agent   │  Analyzes H1 data, generates signals
│  (run_fx.py)    │  EL momentum + regime filter
└────────┬────────┘
         │ HTTP POST
         ▼
┌─────────────────┐
│  Bridge Server  │  Queue signals for MT4
│  (bridge.py)    │  Port 5000
└────────┬────────┘
         │ HTTP GET (poll)
         ▼
┌─────────────────┐
│   MT4 EA        │  Execute trades, manage positions
│  (BridgeEA)     │  0.10 lot (IG mini contracts)
└─────────────────┘
\`\`\`

## Trading Strategy

### Algorithm Components

1. **EL Generalized Momentum (Display Variant)**
   - Z-scored log returns with EMA smoothing
   - Window: 48 bars (configurable)
   - EMA span: 10 bars
   
2. **Regime Tilt Filter**
   - Trend probability proxy: tanh(2 × z-score of rolling mean)
   - Output: [-1, 1] where +1 = strong uptrend, -1 = strong downtrend
   - Can be upgraded to 2-state HMM with `hmmlearn`

3. **Combined Score**
   \`\`\`
   score = momentum × regime_tilt
   \`\`\`
   - Only trade if |score| ≥ threshold (default: 0.40)
   - Side: BUY if score > 0, SELL if score < 0

4. **Correlation Filter**
   - Max concurrent positions: 4 (configurable)
   - Pairwise correlation limit: 0.70
   - Picks highest-scoring uncorrelated pairs

5. **Cost Gate**
   - Expected move must exceed 3× transaction cost
   - Cost estimate: spread × pip_value × lot_size / equity
   - Filters out low-probability setups

6. **Dynamic Target Sizing (Optional)**
   - Base target: 1% per trade
   - Volatility-scaled: `1% × √(current_vol / reference_vol)`
   - Adapts to market regimes

### Execution Rules

- **Entry:** Market orders, 0.10 lot (IG mini contracts)
- **Per-Trade TP:** Cash-based TP (~1% of equity, converted to price by EA)
- **Basket TP:** EA closes all positions when total profit ≥ +1% of cycle start equity
- **SL:** Optional (not used by default)
- **Magic Number:** 246810 (filters orders belonging to this EA)
- **Update Frequency:** 55 seconds (configurable)

## Configuration

Edit `src/config/fx_el_minis.yaml`:

\`\`\`yaml
# Symbol universe (all IG FX pairs with mini contracts)
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

# Data
data_dir: "data/fx_minis"
lookback_bars: 400
\`\`\`

## IG Mini Contracts

### Available Pairs

**Majors (12):** AUDUSD, EURCHF, EURGBP, EURJPY, EURUSD, GBPEUR, GBPJPY, GBPUSD, USDCAD, USDCHF, USDJPY, USDHKD

**Minors (13):** CADCHF, CADJPY, CHFJPY, EURCAD, EURSGD, EURZAR, GBPCAD, GBPCHF, GBPSGD, GBPZAR, SGDJPY, USDSGD, USDZAR

**Australasian (18):** AUDCAD, AUDCHF, AUDEUR, AUDGBP, AUDJPY, AUDNZD, AUDSGD, EURAUD, EURNZD, GBPAUD, GBPNZD, NZDAUD, NZDCHF, NZDEUR, NZDGBP, NZDJPY, NZDUSD, NZDCAD

**Scandinavian (12):** CADNOK, CHFNOK, EURDKK, EURNOK, EURSEK, GBPDKK, GBPNOK, GBPSEK, NOKSEK, USDDKK, USDNOK, USDSEK

**Exotics (24):** CHFHUF, EURCZK, EURHUF, EURILS, EURMXN, EURPLN, EURTRY, GBPCZK, GBPHUF, GBPILS, GBPMXN, GBPPLN, GBPTRY, MXNJPY, NOKJPY, PLNJPY, SEKJPY, TRYJPY, USDCZK, USDHUF, USDILS, USDMXN, USDPLN, USDTRY

**Total: 79 pairs**

### Contract Sizing on IG

| Type | Lot Size | Units | Example Position Value |
|------|----------|-------|----------------------|
| **Mini** (used) | 0.10 | 10,000 | €10,000 for EURUSD |
| Standard | 1.0 | 100,000 | €100,000 |
| Micro | 0.01 | 1,000 | €1,000 |

**Important:** On IG MT4, mini contracts are **not separate symbols**. You trade standard symbols (e.g., EURUSD) with lot size = 0.10.

## Monitoring

### Bridge Server Endpoints

\`\`\`bash
# Check server health
curl http://127.0.0.1:5000/health

# View recent EA reports
curl http://127.0.0.1:5000/reports
\`\`\`

### MT4 Experts Tab

Watch for:
- `HEARTBEAT eq=10000.00` - EA alive, reports equity
- `CYCLE_START eq=10000.00 target=100.00` - New basket started
- `OK BUY EURUSD ticket=123456 lots=0.10 tp_cash=100.00` - Trade executed
- `CYCLE_TARGET_HIT eq=10100.00 profit=100.00` - Basket closed at +1%

### Log Files

- **Bridge:** Console output shows all signals and reports
- **Python Agent:** Console output shows decisions
- **MT4 EA:** Experts tab in Terminal window (Ctrl+T)

## File Structure

\`\`\`
ai-hedge-fund/
├── src/
│   ├── agents/
│   │   ├── fx_el_hawkes_agent.py   # Main strategy agent
│   │   └── risk_utils.py            # EL momentum, regime filter, etc.
│   ├── execution/
│   │   └── mt4_bridge_client.py     # HTTP client for bridge
│   ├── config/
│   │   └── fx_el_minis.yaml         # IG mini contracts config
│   └── run_fx.py                    # Main runner script
├── bridge_api/
│   ├── bridge.py                    # HTTP bridge server
│   └── requirements.txt
├── MQL4/
│   ├── Experts/
│   │   ├── BridgeEA.mq4            # Trade executor
│   │   └── SymbolScanner.mq4       # Symbol discovery
│   └── Include/
│       └── BridgeUtils.mqh         # Helper functions
├── data/
│   └── fx_minis/                    # H1 CSV files (EURUSD.csv, etc.)
└── docs/
    └── IG_MT4_SETUP.md              # Detailed MT4 setup guide
\`\`\`

## Troubleshooting

### No Signals Generated

**Check:**
- CSV files in `data/fx_minis/` with sufficient history (400+ bars)
- Symbol names match config (EURUSD, GBPUSD, etc.)
- Score threshold not too high (try lowering from 0.40 to 0.30)
- Volatility gate not filtering everything (check console output)

### EA Not Receiving Signals

**Check:**
- Bridge server running: `curl http://127.0.0.1:5000/health`
- WebRequest enabled in MT4 for `http://127.0.0.1:5000`
- AutoTrading enabled (green button in MT4 toolbar)
- EA attached to chart and running (smiley face icon)

### Trades Not Executing

**Check:**
- Account has sufficient margin (min ~$100 for 0.10 lot EURUSD)
- Market is open (FX trades 24/5, closed weekends)
- Symbol exists in Market Watch (right-click → Show All)
- No connection issues (green indicator bottom-right)

### Wrong Lot Sizes

**Check:**
- `UseIGMinis = true` in EA settings
- `ig_mini_lot_size: 0.10` in YAML config
- Broker allows 0.10 lot size (check contract specs)

### Basket Not Closing at +1%

**Check:**
- EA's cycle tracking (look for `CYCLE_START` in Experts log)
- Equity calculation (may include floating P&L)
- Multiple EAs running (only one should manage cycle)

## Performance Considerations

### Latency
- Bridge polling: 1 second (EA timer)
- Agent update: 55 seconds (configurable via `--sleep`)
- **This is NOT a high-frequency system** - designed for H1 timeframe

### Resource Usage
- Python agent: minimal (CSV reads + numpy calculations)
- Bridge server: negligible (Flask + simple queue)
- MT4 EA: minimal (timer-based polling)

### Scalability
- Universe: 79 IG FX pairs (can handle all simultaneously)
- Max concurrent: 4 positions (configurable, RAM not a constraint)
- Data: 400 bars × 79 pairs = ~32K rows (trivial for pandas)

## Testing Recommendations

### 1. Demo Account First
- Use IG-DEMO server, not IG-LIVE2
- Run for at least 1 week
- Verify all components work correctly

### 2. Symbol Scanner
- Run `SymbolScanner.mq4` to discover available symbols
- Compare with expected universe (79 pairs)
- Check contract specs (minlot, tickvalue, spread)

### 3. Paper Trade Verification
- Export H1 data for 1-2 months
- Run agent with `--equity 10000`
- Compare decisions vs manual analysis

### 4. Gradual Rollout
- Start with 1-2 major pairs (EURUSD, GBPUSD)
- Increase max_concurrent gradually (1 → 2 → 4)
- Monitor correlation filter effectiveness

## Upgrading to HMM Regime Detection

The default regime filter is a simple proxy. For better performance:

\`\`\`bash
# Install HMM dependencies
poetry install -E hmm
\`\`\`

Then edit `src/agents/risk_utils.py`:

\`\`\`python
from hmmlearn.hmm import GaussianHMM

def regime_tilt_hmm(ret: pd.Series, n_states: int = 2) -> pd.Series:
    """2-state Gaussian HMM for regime detection."""
    X = ret.values.reshape(-1, 1)
    model = GaussianHMM(n_components=n_states, covariance_type="full", n_iter=100)
    model.fit(X)
    states = model.predict(X)
    # Map states to [-1, 1]: assume higher mean = trend state
    means = model.means_.flatten()
    trend_state = np.argmax(means)
    tilt = np.where(states == trend_state, 1.0, -1.0)
    return pd.Series(tilt, index=ret.index)
\`\`\`

Replace `regime_tilt(r)` call in `fx_el_hawkes_agent.py` with `regime_tilt_hmm(r)`.

## License & Disclaimer

**Educational use only. Not financial advice.**

- No guarantees of profitability
- Past performance ≠ future results
- Test thoroughly on demo before live trading
- Creator assumes no liability for losses
- Consult a licensed financial advisor

By using this system, you acknowledge:
1. You understand the risks of leveraged FX trading
2. You will test extensively on demo accounts
3. You accept full responsibility for any losses
4. You will comply with all applicable regulations

## Support & Resources

- **Documentation:** [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md)
- **IG MT4 Details:** https://www.ig.com/en/mt4
- **IG FX Specs:** https://www.ig.com/en/help-and-support/cfds/fees-and-charges/what-are-igs-forex-cfd-product-details

## Contributing

This system is part of the ai-hedge-fund repository. Contributions welcome:

1. Fork the repository
2. Create a feature branch
3. Test thoroughly on demo account
4. Submit a pull request with detailed description

**Focus areas:**
- Additional regime detection methods (HMM, Markov-switching, etc.)
- Alternative momentum indicators (EL variants, Hawkes processes)
- Risk management enhancements (adaptive sizing, drawdown limits)
- Execution improvements (order splitting, TWAP, etc.)

---

**Happy Trading! 🚀**

*(On demo accounts, of course 😉)*
