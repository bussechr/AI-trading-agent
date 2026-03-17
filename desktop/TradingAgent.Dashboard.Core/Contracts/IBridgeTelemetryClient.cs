using TradingAgent.Dashboard.Core.Models;

namespace TradingAgent.Dashboard.Core.Contracts;

public interface IBridgeTelemetryClient
{
    Task<BridgeHealthSnapshot> GetHealthAsync(CancellationToken cancellationToken);
    Task<StateSnapshot?> GetStateAsync(CancellationToken cancellationToken);
    Task<MetricsSnapshot?> GetMetricsAsync(CancellationToken cancellationToken);
    Task<IReadOnlyList<ReportRow>> GetReportsAsync(int limit, CancellationToken cancellationToken);
    Task<IReadOnlyList<BridgeCommandRow>> GetCommandsAsync(int limit, CancellationToken cancellationToken);
    Task<IReadOnlyList<CommandLifecycleRow>> GetCommandEventsAsync(int limit, string? commandId, CancellationToken cancellationToken);
    Task<IReadOnlyList<GovernanceEventRow>> GetGovernanceEventsAsync(int limit, CancellationToken cancellationToken);
    Task<MonitorSnapshot?> GetMonitorAsync(CancellationToken cancellationToken);
    Task<IReadOnlyDictionary<string, TickSnapshot>> GetTicksAsync(CancellationToken cancellationToken);
    Task<IReadOnlyList<MarketBar>> GetBarsAsync(string symbol, int limit, CancellationToken cancellationToken);
    Task<IReadOnlyList<IndicatorVisualEvent>> GetVisualTapAsync(string symbol, int limit, CancellationToken cancellationToken);
}
