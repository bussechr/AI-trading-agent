using System.Text.Json.Nodes;
using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Core.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class TelemetryAggregatorTests
{
    private readonly TelemetryAggregator _sut = new();

    [Fact]
    public void Merge_PrioritizesApiLiveState_AndKeepsDbHistory()
    {
        var t0 = DateTimeOffset.UtcNow;
        var api = new ApiTelemetrySnapshot
        {
            CapturedAtUtc = t0,
            Health = new BridgeHealthSnapshot(t0, true, "healthy", string.Empty),
            State = new StateSnapshot(
                CapturedAtUtc: t0,
                SystemStatus: "connected",
                Equity: 10000,
                Margin: 100,
                FreeMargin: 9900,
                Leverage: 200,
                Positions: Array.Empty<PositionSnapshot>(),
                AgentDecisions: Array.Empty<LiveDecisionRow>(),
                LastHeartbeatUtc: t0,
                Raw: new JsonObject()
            ),
            Metrics = new MetricsSnapshot(
                CapturedAtUtc: t0,
                PendingCount: 2,
                AckTimeoutRate5m: 0.01,
                QueueToTerminalP95Ms: 350,
                InteropErrorBudget: new Dictionary<string, int>(),
                Raw: new JsonObject())
        };

        var db = new DbTelemetrySnapshot
        {
            CapturedAtUtc = t0,
            Status = new DbReaderStatus(true, "api+sqlite", "ok", t0),
            AccountSnapshots =
            [
                new AccountSnapshotRow(t0, 9950, 90, 9860, 200, "heartbeat", new JsonObject()),
            ],
        };

        var merged = _sut.Merge(UnifiedTelemetryState.Empty, api, db, t0);

        Assert.True(merged.ApiConnected);
        Assert.Equal("api+sqlite", merged.DataMode);
        Assert.NotNull(merged.State);
        Assert.Equal(10000, merged.State!.Equity);
        Assert.Single(merged.EquityCurve24h);
        Assert.Equal(9950, merged.EquityCurve24h[0].Equity);
    }

    [Fact]
    public void Merge_SetsStale_WhenApiIsSilent_ThenClearsOnReconnect()
    {
        var t0 = DateTimeOffset.UtcNow;
        var connected = _sut.Merge(
            UnifiedTelemetryState.Empty,
            new ApiTelemetrySnapshot
            {
                CapturedAtUtc = t0,
                Health = new BridgeHealthSnapshot(t0, true, "healthy", string.Empty),
            },
            null,
            t0
        );

        var stale = _sut.Merge(connected, null, null, t0.AddSeconds(8));
        var recovered = _sut.Merge(
            stale,
            new ApiTelemetrySnapshot
            {
                CapturedAtUtc = t0.AddSeconds(9),
                Health = new BridgeHealthSnapshot(t0.AddSeconds(9), true, "healthy", string.Empty),
            },
            null,
            t0.AddSeconds(9)
        );

        Assert.False(connected.ApiStale);
        Assert.True(stale.ApiStale);
        Assert.False(recovered.ApiStale);
    }

    [Fact]
    public void Merge_PrunesDataTo24HourWindow()
    {
        var now = DateTimeOffset.UtcNow;
        var oldTs = now.AddHours(-30);
        var freshTs = now.AddHours(-1);

        var current = UnifiedTelemetryState.Empty with
        {
            EquityCurve24h =
            [
                new AccountEquityPoint(oldTs, 9000),
                new AccountEquityPoint(freshTs, 10000),
            ],
            Reports =
            [
                new ReportRow(oldTs, "old", new JsonObject()),
                new ReportRow(freshTs, "new", new JsonObject()),
            ],
        };

        var merged = _sut.Merge(current, null, null, now);

        Assert.Single(merged.EquityCurve24h);
        Assert.Equal(freshTs, merged.EquityCurve24h[0].TimeUtc);
        Assert.Single(merged.Reports);
        Assert.Equal("new", merged.Reports[0].Message);
    }
}
