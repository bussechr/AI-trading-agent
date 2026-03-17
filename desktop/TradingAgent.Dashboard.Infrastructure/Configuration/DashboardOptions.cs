namespace TradingAgent.Dashboard.Infrastructure.Configuration;

public sealed class DashboardOptions
{
    public string BridgeBaseUrl { get; set; } = "http://127.0.0.1:58710";
    public string RuntimeDbPath { get; set; } = "data/state/runtime_v2.db";
    public string Symbol { get; set; } = "EURUSD";
    public int ReportsLimit { get; set; } = 800;
    public int CommandsLimit { get; set; } = 1200;
    public int EventsLimit { get; set; } = 1200;
    public int GovernanceLimit { get; set; } = 1200;
    public int VisualTapLimit { get; set; } = 200;
    public int BarsLimit { get; set; } = 400;

    public TimeSpan FastPollInterval { get; set; } = TimeSpan.FromSeconds(1);
    public TimeSpan MediumPollInterval { get; set; } = TimeSpan.FromSeconds(2);
    public TimeSpan SlowPollInterval { get; set; } = TimeSpan.FromSeconds(5);
    public TimeSpan BarsPollInterval { get; set; } = TimeSpan.FromSeconds(60);
    public TimeSpan DbPollInterval { get; set; } = TimeSpan.FromSeconds(5);
}
