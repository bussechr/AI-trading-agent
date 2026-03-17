using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Core.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class StalenessAndReconnectTests
{
    [Fact]
    public void ApiBecomesStaleAfterThreshold()
    {
        var aggregator = new TelemetryAggregator();
        var t0 = DateTimeOffset.UtcNow;

        var state = aggregator.Merge(
            UnifiedTelemetryState.Empty,
            new ApiTelemetrySnapshot
            {
                CapturedAtUtc = t0,
                Health = new BridgeHealthSnapshot(t0, true, "healthy", string.Empty),
            },
            null,
            t0
        );

        var stale = aggregator.Merge(state, null, null, t0.AddSeconds(6));
        Assert.True(stale.ApiStale);
    }

    [Fact]
    public void ReconnectClearsStaleAndPreservesLifecycleHistoryWithoutDuplicates()
    {
        var aggregator = new TelemetryAggregator();
        var t0 = DateTimeOffset.UtcNow;

        var first = aggregator.Merge(
            UnifiedTelemetryState.Empty,
            new ApiTelemetrySnapshot
            {
                CapturedAtUtc = t0,
                Health = new BridgeHealthSnapshot(t0, true, "healthy", string.Empty),
                CommandEvents =
                [
                    new CommandLifecycleRow("cmd-1", "queued", string.Empty, t0, new()),
                ],
            },
            null,
            t0
        );

        var stale = aggregator.Merge(first, null, null, t0.AddSeconds(7));

        var recovered = aggregator.Merge(
            stale,
            new ApiTelemetrySnapshot
            {
                CapturedAtUtc = t0.AddSeconds(8),
                Health = new BridgeHealthSnapshot(t0.AddSeconds(8), true, "healthy", string.Empty),
                CommandEvents =
                [
                    new CommandLifecycleRow("cmd-1", "queued", string.Empty, t0, new()),
                    new CommandLifecycleRow("cmd-2", "queued", string.Empty, t0.AddSeconds(8), new()),
                ],
            },
            null,
            t0.AddSeconds(8)
        );

        Assert.True(stale.ApiStale);
        Assert.False(recovered.ApiStale);
        Assert.Equal(2, recovered.CommandEvents.Count);
    }
}
