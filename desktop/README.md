# TradingAgent Desktop Dashboard (WPF)

## Status

Archived for rollback/reference only. Active UI/runtime path is the Next.js dashboard + `fx-quant-stack` bridge/runtime stack.

Production-core v1 desktop dashboard for agentic trading observability.

## What is implemented

- `TradingAgent.Dashboard.App` (WPF .NET 8, dockable workbench with AvalonDock)
- `TradingAgent.Dashboard.Core` (contracts, canonical models, state aggregation)
- `TradingAgent.Dashboard.Infrastructure` (bridge REST client, sqlite reader, ingestion loop)
- `TradingAgent.Dashboard.Tests` (unit and integration-style tests for aggregation, parsing, ingestion resilience)

## Runtime defaults

- Bridge URL: `http://127.0.0.1:58710`
- Runtime DB: `data/state/runtime_v2.db`
- Symbol panel: `EURUSD`

Override with environment variables:

- `TRADING_DASHBOARD_BRIDGE_URL`
- `TRADING_DASHBOARD_RUNTIME_DB`
- `TRADING_DASHBOARD_SYMBOL`

Optional file-based settings:

- `desktop/dashboard.settings.example.json` (copy to `dashboard.settings.json` next to app executable)

## Build / Run (Windows)

```powershell
cd desktop
 dotnet restore TradingAgent.Dashboard.sln
 dotnet build TradingAgent.Dashboard.sln -c Release
 dotnet run --project TradingAgent.Dashboard.App/TradingAgent.Dashboard.App.csproj
```

## Tests

```powershell
cd desktop
 dotnet test TradingAgent.Dashboard.Tests/TradingAgent.Dashboard.Tests.csproj
```

## Notes

- v1 is **monitor-only** (no command dispatch controls).
- Indicator mirror uses **`/v2/visuals/tap`** (non-consuming) to avoid interfering with MT4 indicator polling.
- During SQLite unavailability, app degrades to API-only mode with warnings.
