# IG MT4 Setup Guide

Complete setup instructions for running the FX EL trading agent with IG's MT4 platform.

## Account Information

- **IG Account Reference:** BXAWM
- **MT4 Login (Account Number):** 96940
- **MT4 Password:** (Required - see below)
- **Server:** 
  - Live: `IG-LIVE2` (mt4a2.ig.com:443)
  - Demo: `IG-DEMO` (demo-mt4.ig.com:443)

## Prerequisites

### 1. Get Your MT4 Password

You need your MT4 password (not your My IG password). Get it from:

1. Email from IG when MT4 account was created, OR
2. Reset in **My IG → Settings → MT4** for account BXAWM

### 2. Install MT4

1. Download MT4 from IG's website
2. Install and launch MT4
3. Go to **File → Login to Trade Account**
4. Enter:
   - **Login:** 96940
   - **Password:** [Your MT4 password]
   - **Server:** IG-LIVE2 (for live) or IG-DEMO (for demo)
5. Verify connection: green indicator bottom-right + ping showing

### 3. Enable Symbol Visibility

1. Open **Market Watch** (Ctrl+M or View → Market Watch)
2. Right-click → **Show All**
3. Verify IG FX symbols are visible (EURUSD, GBPUSD, etc.)

## Python Setup

### 1. Install Dependencies

\`\`\`bash
# From the repository root
poetry install

# Optional: install HMM support for advanced regime detection
poetry install -E hmm
\`\`\`

### 2. Configure Data Directory

Export H1 (1-hour) historical data from MT4 for the symbols you want to trade:

\`\`\`bash
# Create data directory
mkdir -p data/fx_minis

# In MT4:
# Tools → History Center
# Select symbol (e.g., EURUSD)
# Select H1 timeframe
# Export to CSV
# Save as: data/fx_minis/EURUSD.csv

# Expected CSV format:
# time,open,high,low,close
\`\`\`

## MT4 EA Setup

### 1. Install EA Files

Copy the MT4 files to your MT4 data folder:

\`\`\`bash
# Find your MT4 data folder:
# In MT4: File → Open Data Folder

# Copy files:
# MQL4/Include/BridgeUtils.mqh → [MT4 Data]/MQL4/Include/
# MQL4/Experts/BridgeEA.mq4 → [MT4 Data]/MQL4/Experts/
# MQL4/Experts/SymbolScanner.mq4 → [MT4 Data]/MQL4/Experts/
\`\`\`

### 2. Compile EAs

1. In MT4, open **MetaEditor** (F4 or Tools → MetaQuotes Language Editor)
2. Open `BridgeEA.mq4`
3. Click **Compile** (F7)
4. Repeat for `SymbolScanner.mq4`
5. Close MetaEditor

### 3. Enable WebRequest

**Critical:** MT4 blocks web requests by default.

1. **Tools → Options → Expert Advisors**
2. Check **Allow WebRequest for listed URL**
3. Add: `http://127.0.0.1:58710`
4. Check **Allow DLL imports** (if needed)
5. Click **OK**

### 4. Run Symbol Scanner (Optional)

Discover available IG symbols:

1. Open any chart (e.g., EURUSD H1)
2. Drag **SymbolScanner** from Navigator → Expert Advisors onto chart
3. Check **Experts** tab for output
4. Save the list of symbols found

## Running the System

### 1. Start the Bridge API

The EA communicates with Python via HTTP. Start the bridge server:

\`\`\`bash
python -m src.trader.cli bridge serve --host 127.0.0.1 --port 58710
\`\`\`

**Note:** The v2 bridge exposes:
- Listens on http://127.0.0.1:58710
- Has `/v2/commands/poll` endpoint (GET) - returns pending commands
- Has `/v2/commands` endpoint (POST) - receives trade commands
- Has `/v2/reports` endpoint (POST) - receives EA status

### 2. Start the FX Trading Agent

\`\`\`bash
# Run the runtime with your current equity
python -m src.trader.cli runtime run --equity 10000 --sleep 10
\`\`\`

### 3. Attach EA to MT4

1. Open a chart for any IG FX pair (e.g., EURUSD, H1 timeframe)
2. Enable **AutoTrading** (toolbar button or Alt+A)
3. Drag **BridgeEA** from Navigator → Expert Advisors onto chart
4. Configure EA parameters:
   - **ApiBase:** http://127.0.0.1:58710 (default)
   - **Magic:** 246810 (default)
   - **UseIGMinis:** true (enforces 0.10 lot size)
5. Click **OK**
6. Verify EA is running: smiley face in top-right of chart

### 4. Monitor Operation

Watch the **Experts** tab in Terminal (Ctrl+T) for:
- `HEARTBEAT` messages (EA is alive)
- `CYCLE_START` (new trading basket started)
- `OK BUY/SELL` messages (trades executed)
- `CYCLE_TARGET_HIT` (basket closed at +1% profit)

## IG Mini Contract Specifications

On IG MT4, mini contracts are **not separate symbols**. You trade standard symbols with specific lot sizes:

| Contract Type | Lot Size | Example |
|--------------|----------|---------|
| **Standard** | 1.0      | 100,000 units |
| **Mini** | 0.10     | 10,000 units |
| **Micro** | 0.01     | 1,000 units |

The system uses **0.10 lot** for all trades (mini contracts).

### Available IG FX Mini Pairs

**Majors:** AUDUSD, EURCHF, EURGBP, EURJPY, EURUSD, GBPEUR, GBPJPY, GBPUSD, USDCAD, USDCHF, USDJPY, USDHKD

**Minors:** CADCHF, CADJPY, CHFJPY, EURCAD, EURSGD, EURZAR, GBPCAD, GBPCHF, GBPSGD, GBPZAR, SGDJPY, USDSGD, USDZAR

**Australasian:** AUDCAD, AUDCHF, AUDEUR, AUDGBP, AUDJPY, AUDNZD, AUDSGD, EURAUD, EURNZD, GBPAUD, GBPNZD, NZDAUD, NZDCHF, NZDEUR, NZDGBP, NZDJPY, NZDUSD, NZDCAD

**Scandinavian:** CADNOK, CHFNOK, EURDKK, EURNOK, EURSEK, GBPDKK, GBPNOK, GBPSEK, NOKSEK, USDDKK, USDNOK, USDSEK

**Exotics:** CHFHUF, EURCZK, EURHUF, EURILS, EURMXN, EURPLN, EURTRY, GBPCZK, GBPHUF, GBPILS, GBPMXN, GBPPLN, GBPTRY, MXNJPY, NOKJPY, PLNJPY, SEKJPY, TRYJPY, USDCZK, USDHUF, USDILS, USDMXN, USDPLN, USDTRY

## Trading Strategy Summary

The system implements an **EL momentum + regime filter** strategy:

### Algorithm
1. **EL Generalized Momentum:** Z-scored returns with EMA smoothing
2. **Regime Tilt:** Proxy filter for trend probability (optional HMM upgrade)
3. **Score:** `score = momentum × regime_tilt`
4. **Correlation Filter:** Max 4 concurrent positions with correlation ≤ 0.70
5. **Cost Gate:** Expected move must exceed 3× transaction costs

### Execution
- **Entry:** Mini contracts (0.10 lot) at market
- **Per-Trade TP:** 1% of equity (volatility-scaled if enabled)
- **Basket TP:** Close all when total basket profit reaches +1%
- **Update Frequency:** Every 55 seconds (configurable)

## Troubleshooting

### EA Not Connecting
- Check WebRequest is enabled for `http://127.0.0.1:58710`
- Verify bridge server is running: `curl http://127.0.0.1:58710/v2/health`
- Check firewall isn't blocking localhost:58710

### No Trades Executing
- Verify AutoTrading is enabled (Alt+A)
- Check account has sufficient margin
- Verify symbols are in Market Watch
- Check Experts tab for error messages
- Ensure market is open for at least one pair

### "Invalid Account" Error
- Wrong server selected (IG-LIVE2 vs IG-DEMO)
- Wrong password (MT4 password, not My IG password)
- Account number typo (should be 96940)

### Symbols Not Found
- Market Watch → right-click → Show All
- Some exotic pairs may not be available on your account type
- Use SymbolScanner EA to discover available symbols

### EA Closes Trades Immediately
- Check if cycle target (1%) was already hit
- Verify TP prices are reasonable (check Experts log)
- Ensure lot size is valid (0.10 for minis)

## Testing Strategy

### Demo Account Testing (Recommended)
1. Use IG-DEMO server instead of IG-LIVE2
2. Run system for several days to validate
3. Monitor:
   - Trade execution (correct symbols, lot sizes)
   - TP/SL levels (reasonable prices)
   - Basket closure (at +1% as expected)
   - No excessive trades (correlation filter working)

### Strategy Tester (Backtesting)
1. MT4 → View → Strategy Tester
2. Select **BridgeEA**
3. Symbol: any IG FX pair
4. Period: H1
5. Model: Every tick (most accurate)
6. Note: Limited without live bridge server running

## Configuration Tuning

Edit `src/config/fx_el_minis.yaml` to adjust:

\`\`\`yaml
max_concurrent: 4      # Max simultaneous positions
corr_max: 0.70         # Max correlation between positions
score_threshold: 0.40  # Minimum signal strength
target_base_pct: 0.010 # Target profit per trade (1%)
el_window: 48          # Momentum lookback (hours)
\`\`\`

## Support & Resources

- **IG MT4 Documentation:** https://www.ig.com/en/mt4
- **IG Product Details:** https://www.ig.com/en/help-and-support/cfds/fees-and-charges/what-are-igs-forex-cfd-product-details
- **IG MT4 Specifications:** https://www.ig.com/au/help-and-support/cfds/fees-and-charges/what-are-igs-forex-mt4-product-details

## License & Disclaimer

This system is for **educational and research purposes only**. 

- Not financial advice
- No guarantees of profitability
- Past performance ≠ future results
- Test thoroughly on demo before live trading
- Consult a financial advisor before trading with real money
- Creator assumes no liability for losses

By using this system, you agree to use it solely for learning purposes and accept all risks.
