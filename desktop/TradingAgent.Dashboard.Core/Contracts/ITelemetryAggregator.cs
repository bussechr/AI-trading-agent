using TradingAgent.Dashboard.Core.Models;

namespace TradingAgent.Dashboard.Core.Contracts;

public interface ITelemetryAggregator
{
    UnifiedTelemetryState Merge(
        UnifiedTelemetryState current,
        ApiTelemetrySnapshot? apiSnapshot,
        DbTelemetrySnapshot? dbSnapshot,
        DateTimeOffset nowUtc
    );
}
