using System.Text.Json;

namespace TradingAgent.Dashboard.Infrastructure.Configuration;

public static class DashboardOptionsLoader
{
    public static DashboardOptions Load(string? configPath = null)
    {
        var options = new DashboardOptions();

        var filePath = string.IsNullOrWhiteSpace(configPath)
            ? Path.Combine(AppContext.BaseDirectory, "dashboard.settings.json")
            : configPath;

        if (File.Exists(filePath))
        {
            try
            {
                var text = File.ReadAllText(filePath);
                var loaded = JsonSerializer.Deserialize<DashboardOptions>(text, new JsonSerializerOptions
                {
                    PropertyNameCaseInsensitive = true,
                });
                if (loaded is not null)
                {
                    options = loaded;
                }
            }
            catch
            {
                // Keep defaults; runtime warning is surfaced by app logging.
            }
        }

        var bridgeOverride = Environment.GetEnvironmentVariable("TRADING_DASHBOARD_BRIDGE_URL");
        if (!string.IsNullOrWhiteSpace(bridgeOverride))
        {
            options.BridgeBaseUrl = bridgeOverride.Trim();
        }

        var dbOverride = Environment.GetEnvironmentVariable("TRADING_DASHBOARD_RUNTIME_DB");
        if (!string.IsNullOrWhiteSpace(dbOverride))
        {
            options.RuntimeDbPath = dbOverride.Trim();
        }

        var symbolOverride = Environment.GetEnvironmentVariable("TRADING_DASHBOARD_SYMBOL");
        if (!string.IsNullOrWhiteSpace(symbolOverride))
        {
            options.Symbol = symbolOverride.Trim().ToUpperInvariant();
        }

        return options;
    }
}
