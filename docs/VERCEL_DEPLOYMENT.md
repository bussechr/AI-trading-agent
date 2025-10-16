# Vercel Deployment Guide

## Architecture Overview

Your trading system has two parts:

1. **Next.js Dashboard** (deployed to Vercel) - Displays live trading data via HTTPS
2. **Python Trading Stack** (runs locally) - Connects to MT4 and posts data to Vercel

\`\`\`
┌─────────────────────────────────────────────────────────┐
│ Your Local Machine                                      │
│                                                          │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐ │
│  │   MT4    │◄───│ Bridge API   │◄───│  FX Agent    │ │
│  │ (IG Live)│    │ (Flask:5000) │    │ (EL-Hawkes)  │ │
│  └──────────┘    └──────┬───────┘    └──────────────┘ │
│                         │                               │
│                         │ POST /api/trading/update      │
│                         │ (HTTPS)                       │
└─────────────────────────┼───────────────────────────────┘
                          │
                          ▼
                 ┌─────────────────┐
                 │ Vercel Dashboard│
                 │   (HTTPS)       │
                 │                 │
                 │ - Receives data │
                 │ - Stores state  │
                 │ - Displays UI   │
                 └─────────────────┘
\`\`\`

## Step 1: Deploy Dashboard to Vercel

### Option A: Deploy from v0 (Easiest)

1. Click the **"Publish"** button in the top-right of v0
2. Follow the prompts to deploy to Vercel
3. Copy your deployment URL (e.g., `https://trading-agent-xyz.vercel.app`)

### Option B: Deploy from GitHub

1. Push code to GitHub (use the GitHub button in v0)
2. Go to [vercel.com](https://vercel.com)
3. Click "New Project" → Import your GitHub repo
4. Deploy with default settings

## Step 2: Configure Python Bridge

On your local machine where MT4 runs:

1. **Set the dashboard URL** in your `.env` file:
   \`\`\`bash
   DASHBOARD_URL=https://your-trading-agent.vercel.app
   \`\`\`

2. **The bridge will automatically POST updates** to your Vercel dashboard at:
   - `https://your-trading-agent.vercel.app/api/trading/update`

## Step 3: Start the Trading Stack

On your local machine:

\`\`\`bash
# Terminal 1: Start the bridge
cd bridge_api
python bridge.py

# Terminal 2: Start MT4 with BridgeEA
# (Open MT4, attach BridgeEA to any chart)

# Terminal 3: Run the trading agent
python -m src.agents.fx_el_hawkes_agent
\`\`\`

## Step 4: View Live Dashboard

Open your Vercel URL in a browser:
- `https://your-trading-agent.vercel.app`

You should see:
- ✅ Live equity updates from MT4
- ✅ Active trading signals
- ✅ Cycle progress
- ✅ Agent decisions

## Data Flow

1. **MT4 EA** sends heartbeats/reports → **Bridge** (local)
2. **FX Agent** sends signals → **Bridge** (local)
3. **Bridge** forwards signals → **MT4 EA** (local)
4. **Bridge** POSTs state → **Vercel Dashboard** (HTTPS)
5. **Dashboard** displays live data from in-memory state

## Troubleshooting

### Dashboard shows "Waiting for data"

- Check that `DASHBOARD_URL` is set in your `.env` file
- Verify the bridge is running: `curl http://localhost:5000/health`
- Check bridge logs for POST errors to Vercel

### Bridge can't reach Vercel

- Ensure your machine has internet access
- Check firewall settings
- Verify the Vercel URL is correct (include `https://`)

### MT4 not connecting

- Add `http://127.0.0.1:5000` to MT4 WebRequest whitelist
- Check MT4 Expert Advisors are enabled
- Verify BridgeEA is attached and running

## Production Considerations

### Use Redis for State Storage

The current setup uses in-memory state (resets on Vercel redeployment). For production:

1. Add Upstash Redis integration in Vercel
2. Update `/api/trading/update/route.ts` to store in Redis
3. Update `/api/trading/state/route.ts` to read from Redis

### Secure the Update Endpoint

Add authentication to prevent unauthorized updates:

\`\`\`ts
// app/api/trading/update/route.ts
const API_KEY = process.env.BRIDGE_API_KEY

export async function POST(request: Request) {
  const authHeader = request.headers.get('authorization')
  if (authHeader !== `Bearer ${API_KEY}`) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }
  // ... rest of code
}
\`\`\`

Then update bridge to send the key:

\`\`\`python
# bridge_api/bridge.py
headers = {"Authorization": f"Bearer {os.getenv('BRIDGE_API_KEY')}"}
requests.post(f"{DASHBOARD_URL}/api/trading/update", json=trading_state, headers=headers)
\`\`\`

## IG MT4 Account Details

Your live account configuration:
- **Account**: BXAWMMT4
- **Login**: 96940
- **Server**: IG-LIVE2

Make sure these are configured in MT4 before starting the bridge.
