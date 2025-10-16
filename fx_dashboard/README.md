# FX Trading Dashboard

Real-time monitoring dashboard for the IG MT4 FX trading system.

## Features

- **Real-time System Status** - Connection health, heartbeat monitoring
- **Equity Tracking** - Live account equity with P&L
- **Active Decisions** - Current trading signals from the agent
- **Performance Chart** - Equity curve visualization
- **Trading Cycle** - Progress towards basket target (+1%)
- **Activity Log** - Recent EA reports and events
- **Signal Metrics** - Signals sent vs executed

## Quick Start

### Install Dependencies

\`\`\`bash
cd fx_dashboard
npm install
\`\`\`

### Run Development Server

\`\`\`bash
npm run dev
\`\`\`

Dashboard will be available at: http://localhost:3000

### Build for Production

\`\`\`bash
npm run build
npm run preview
\`\`\`

## Architecture

The dashboard connects to the bridge API (http://127.0.0.1:5000) and polls for updates every 2 seconds.

\`\`\`
┌──────────────┐
│  Dashboard   │  (React + Vite)
│  Port 3000   │
└──────┬───────┘
       │ HTTP Polling (2s)
       ▼
┌──────────────┐
│  Bridge API  │  (Flask + CORS)
│  Port 5000   │
└──────┬───────┘
       │
       ├─ Python Agent (posts decisions)
       └─ MT4 EA (posts reports)
\`\`\`

## API Endpoints

The dashboard consumes:

- `GET /state` - Current trading state
- `GET /reports` - Recent activity log
- `GET /health` - System health check

## Components

### SystemStatus
- Connection indicator
- Heartbeat timestamp
- Key metrics (signals, trades, decisions)

### EquityCard
- Current account equity
- Session P&L (if cycle active)
- Visual trend indicator

### ActiveDecisions
- Grid of current trading signals
- Symbol, side (BUY/SELL), score, price, target
- Color-coded by direction

### PerformanceChart
- Real-time equity curve
- Last 50 data points
- Recharts line chart

### RecentSignals
- Total signals sent
- Trades executed
- Success rate
- Last signal details

### ActivityLog
- Scrollable log of recent events
- Color-coded messages (errors, success, cycles)
- Timestamps

## Customization

### Update Interval

Change polling frequency in `src/App.jsx`:

\`\`\`javascript
const interval = setInterval(async () => {
  // ...
}, 2000) // Change this value (in ms)
\`\`\`

### Color Scheme

Modify Tailwind classes in components or update `tailwind.config.js`.

### Chart Settings

Adjust chart in `src/components/PerformanceChart.jsx`:

\`\`\`javascript
.slice(-50) // Number of data points to show
\`\`\`

## Troubleshooting

### Dashboard Not Connecting

1. Ensure bridge server is running: `python bridge_api/bridge.py`
2. Check CORS is enabled in bridge
3. Verify API_BASE in `App.jsx` matches bridge URL

### No Data Showing

1. Start the Python agent: `poetry run python src/run_fx.py --equity 10000`
2. Attach BridgeEA to MT4 chart
3. Wait for first heartbeat (1 second)

### Chart Not Updating

- Check that MT4 EA is sending HEARTBEAT messages
- View browser console for errors
- Verify reports are accumulating: http://localhost:5000/reports

## Dependencies

- **React 18** - UI framework
- **Vite** - Build tool & dev server
- **Tailwind CSS** - Styling
- **Recharts** - Charts
- **Lucide React** - Icons

## License

Same as parent project (MIT)
