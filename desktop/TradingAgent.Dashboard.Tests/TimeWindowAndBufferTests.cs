using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Core.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class TimeWindowAndBufferTests
{
    [Fact]
    public void MergeCapsEquityCurveToBoundedSize()
    {
        var aggregator = new TelemetryAggregator();
        var now = DateTimeOffset.UtcNow;

        var points = Enumerable.Range(0, 30000)
            .Select(i => new AccountEquityPoint(now.AddSeconds(-i), 10000 + i))
            .ToArray();

        var merged = aggregator.Merge(
            UnifiedTelemetryState.Empty with { EquityCurve24h = points },
            null,
            null,
            now
        );

        Assert.True(merged.EquityCurve24h.Count <= 20000);
    }

    [Fact]
    public void MergeDeduplicatesCommandEventsByIdentity()
    {
        var aggregator = new TelemetryAggregator();
        var now = DateTimeOffset.UtcNow;

        var a = aggregator.Merge(
            UnifiedTelemetryState.Empty,
            new ApiTelemetrySnapshot
            {
                CapturedAtUtc = now,
                CommandEvents =
                [
                    new CommandLifecycleRow("cmd-1", "queued", string.Empty, now, new()),
                    new CommandLifecycleRow("cmd-1", "queued", string.Empty, now, new()),
                ],
            },
            null,
            now
        );

        Assert.Single(a.CommandEvents);
    }
}
