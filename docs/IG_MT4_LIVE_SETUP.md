# IG MT4 Live Trading Setup

## Account Details
- **Account Number**: 96940
- **Account Type**: BXAWM (CFD Account)
- **Server**: IG-LIVE2 (mt4a2.ig.com:443)
- **Demo Server**: IG-DEMO (demo-mt4.ig.com:443)

## MT4 Configuration

### 1. Install MT4 on Linux VPS (Wine)

\`\`\`bash
export WINEPREFIX=$HOME/.mt4
export WINEARCH=win32
wineboot --init
winetricks -q corefonts

# Run IG MT4 installer
xvfb-run -a wine IGMarkets-MT4-Setup.exe
\`\`\`

### 2. Configure MT4

1. **Login**: Use account 96940 with your password
2. **Server**: Select IG-LIVE2 for live trading
3. **Tools → Options → Expert Advisors**:
   - ✅ Allow automated trading
   - ✅ Allow WebRequest for listed URL: `http://127.0.0.1:58710`
   - ✅ Allow DLL imports (if needed)

### 3. Install Bridge EA

1. Copy `MQL4/Experts/BridgeEA.mq4` to MT4's Experts folder
2. Compile in MetaEditor
3. Attach to any chart (e.g., EURUSD H1)
4. Enable AutoTrading (green button)

## IG Mini Contracts

IG uses **standard symbol names** (e.g., EURUSD, GBPUSD) for all contracts.

**Mini contracts are differentiated by lot size**:
- Standard: 1.0 lot = $10/pip
- **Mini: 0.10 lot = $1/pip** ← We trade these
- Micro: 0.01 lot = $0.10/pip

### Available IG FX Pairs (All have Mini contracts)

**Majors**: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, EURGBP, EURJPY, GBPJPY, EURCHF, USDHKD

**Minors**: CADCHF, CADJPY, CHFJPY, EURCAD, EURSGD, EURZAR, GBPCAD, GBPCHF, GBPSGD, GBPZAR, SGDJPY, USDSGD, USDZAR

**Australasian**: AUDCAD, AUDCHF, AUDEUR, AUDGBP, AUDJPY, AUDNZD, AUDSGD, EURAUD, EURNZD, GBPAUD, GBPNZD, NZDAUD, NZDCHF, NZDEUR, NZDGBP, NZDJPY, NZDUSD, NZDCAD

**Scandinavian**: CADNOK, CHFNOK, EURDKK, EURNOK, EURSEK, GBPDKK, GBPNOK, GBPSEK, NOKSEK, USDDKK, USDNOK, USDSEK

**Exotics**: CHFHUF, EURCZK, EURHUF, EURILS, EURMXN, EURPLN, EURTRY, GBPCZK, GBPHUF, GBPILS, GBPMXN, GBPPLN, GBPTRY, MXNJPY, NOKJPY, PLNJPY, SEKJPY, TRYJPY, USDCZK, USDHUF, USDILS, USDMXN, USDPLN, USDTRY

## Bridge EA Behavior

### Exact Lot Enforcement
Python must send a finite, positive lot amount that is already executable under
the broker's live `MODE_MINLOT`, `MODE_MAXLOT`, and `MODE_LOTSTEP` contract. The
EA never replaces zero with a minimum lot and never rounds an approved amount up.
For an account with a 0.10 minimum and 0.10 step, configure
`FXSTACK_MIN_ORDER_LOTS=0.10` and `FXSTACK_ORDER_LOT_STEP=0.10` before starting
the runtime.

### Mandatory Entry Protection
Every BUY/SELL command must include absolute `sl` and `tp_price` values. A BUY
requires SL below bid and TP above ask; a SELL requires SL above ask and TP below
bid. Missing, non-finite, wrongly directed, or broker-stop-level-invalid values
are rejected before `OrderSend`; `tp_cash` is not used as a fallback.

### Cycle Management
1. **Cycle Start**: First trade after flat → store `cycle_start_equity`
2. **Cycle Target**: When `AccountEquity() >= cycle_start_equity * 1.01` → close ALL positions
3. **Reset**: After close, immediately accept new signals

## Running the System

### 1. Start Bridge API

\`\`\`bash
cd ~/mt4-bridge
python3 bridge.py
# Runs on http://127.0.0.1:58710
\`\`\`

### 2. Start MT4

\`\`\`bash
xvfb-run -a wine "$WINEPREFIX/drive_c/Program Files/MetaTrader 4 Terminal/terminal.exe"
\`\`\`

### 3. Start Trading Agent

\`\`\`bash
cd ~/ai-hedge-fund
python -m src.trader.cli runtime run --equity 10000 --sleep 10
\`\`\`

## Monitoring

### Bridge Logs
\`\`\`bash
journalctl -u mt4-bridge@$USER -f
\`\`\`

### Agent Logs
\`\`\`bash
journalctl -u agent-fx@$USER -f
\`\`\`

### Dashboard
Open http://localhost:3000 to view live trading dashboard

## Safety Controls

### Kill Switch
Close all positions immediately:
\`\`\`bash
curl -X POST http://127.0.0.1:58710/v2/commands -H "Content-Type: application/json" -d '{"cmd":"CLOSE_ALL"}'
\`\`\`

### Spread Filter
EA rejects orders if spread > configured maximum

### Margin Check
EA verifies sufficient margin before opening positions

### Hard Stops
All positions have hard stop-loss orders at the broker

## Testing Checklist

- [ ] Demo account login successful
- [ ] Bridge EA attached and AutoTrading enabled
- [ ] WebRequest permission granted for 127.0.0.1:58710
- [ ] Bridge API responding to /v2/commands/poll and /v2/commands
- [ ] Agent sending signals to bridge
- [ ] Python lot settings match the broker minimum/step and the EA opens exactly the approved 0.10 lot size
- [ ] Every entry arrives with a directionally valid absolute SL and TP visible on the broker ticket
- [ ] Cycle exit triggering at +1% equity
- [ ] Positions reopening after cycle close
- [ ] Dashboard showing live data

## Go-Live Procedure

1. **Test on demo** for at least 1 week
2. **Verify** all cycle exits and reopens work correctly
3. **Switch to live** account (same configuration)
4. **Start with minimum** equity ($1,000-$2,000)
5. **Monitor closely** for first 24 hours
6. **Scale gradually** as confidence builds

## Troubleshooting

### EA not polling
- Check WebRequest permission in MT4
- Verify bridge API is running: `curl http://127.0.0.1:58710/v2/health`

### Orders rejected
- Check MT4 Experts log for error codes
- Verify symbol exists: Tools → Market Watch
- Check margin availability
- For `entry_lots_not_exactly_executable`, align `FXSTACK_MIN_ORDER_LOTS` and `FXSTACK_ORDER_LOT_STEP` with the live broker contract; do not rely on EA rounding
- For `entry_protection_invalid`, verify both absolute SL and TP are present, directional, and outside the broker stop level

### Cycle not closing
- Check equity calculation in EA
- Verify cycle_start_equity is set correctly
- Look for floating point precision issues
