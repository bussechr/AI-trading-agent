# Local Trading Dashboard Setup

This guide shows you how to run the complete EL-Hawkes-Regime trading system locally with live MT4 data.

## Prerequisites

- Python 3.9+
- Node.js 18+
- MetaTrader 4 with IG Markets account
- Windows (for MT4) or Wine on Linux/Mac

## Architecture

\`\`\`
┌─────────────────┐      ┌──────────────┐      ┌─────────────┐
│   Next.js       │◄────►│   Python     │◄────►│   MT4 EA    │
│   Dashboard     │ HTTP │   Bridge     │ HTTP │  (BridgeEA) │
│  localhost:3000 │      │  :5000       │      │             │
└─────────────────┘      └──────────────┘      └─────────────┘
                                │
                                ▼
                         ┌──────────────┐
                         │  FX Agent    │
                         │ (EL-Hawkes)  │
                         └──────────────┘
\`\`\`

## Step-by-Step Setup

### 1. Install Python Dependencies

\`\`\`bash
# Using poetry (recommended)
poetry install

# Or using pip
pip install -r requirements.txt
\`\`\`

### 2. Configure IG MT4

**Account Details:**
- Account: BXAWMMT4
- Login: 96940
- Server: IG-LIVE2

**MT4 Setup:**
1. Copy `MQL4/Experts/BridgeEA.mq4` to your MT4 `Experts` folder
2. Compile in MetaEditor
3. Add `http://127.0.0.1:5000` to Tools → Options → Expert Advisors → WebRequest whitelist
4. Attach BridgeEA to any chart (H1 recommended)

### 3. Start the Python Bridge

\`\`\`bash
python bridge_api/bridge.py
\`\`\`

You should see:
\`\`\`
MT4 Bridge Server
Listening on: http://127.0.0.1:5000
\`\`\`

### 4. Start the Trading Agent

\`\`\`bash
python -m src.agents.fx_el_hawkes_agent --config src/config/fx_el_minis.yaml
\`\`\`

The agent will:
- Load H1 CSV data for IG mini pairs
- Calculate EL momentum scores
- Apply regime filter (Markov-switching)
- Run Hawkes microstructure model
- Check LPPLS crash guard
- Send signals to bridge

### 5. Install Dashboard Dependencies

\`\`\`bash
npm install
\`\`\`

### 6. Start the Dashboard

\`\`\`bash
npm run dev
\`\`\`

Open http://localhost:3000 in your browser.

## Verification

You should see:
- ✅ Bridge status: Connected
- ✅ MT4 heartbeat updating every 5 seconds
- ✅ Account equity from your IG account
- ✅ Agent decisions appearing in real-time
- ✅ Trades executing when signals meet criteria

## Troubleshooting

### Bridge Connection Failed
- Check `python bridge_api/bridge.py` is running
- Verify no firewall blocking port 5000
- Check MT4 WebRequest whitelist includes `http://127.0.0.1:5000`

### No Trades Executing
- Verify BridgeEA is attached and shows "Connected" in chart corner
- Check agent is running and generating signals
- Review `bridge_api/bridge.py` logs for signal queue

### Dashboard Shows "Disconnected"
- Ensure you're running on `localhost:3000`, not v0 preview
- Check browser console for CORS errors
- Verify bridge is returning JSON at http://127.0.0.1:5000/state

## Production Deployment

To deploy with HTTPS:
1. Deploy bridge to a VPS with SSL certificate
2. Update `BRIDGE_URL` environment variable
3. Deploy Next.js dashboard to Vercel
4. Configure MT4 to connect to HTTPS bridge URL

## Configuration

Edit `src/config/fx_el_minis.yaml` to adjust:
- `target_pct`: Cash target per trade (default 1%)
- `min_score`: Minimum EL score threshold
- `use_heston_guard`: Enable/disable volatility guard
- `regime_lookback`: Regime filter window
- `hawkes_decay`: Microstructure decay rate

## IG Mini Pairs

The agent automatically handles IG's mini contract naming:
- EURUSD → EURUSDm
- GBPUSD → GBPUSDm
- USDJPY → USDJPYm
- etc.

Minimum lot size: 0.10 (IG minis)
