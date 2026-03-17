using System.Text.Json.Nodes;
using TradingAgent.Dashboard.Core.Contracts;
using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Core.Services;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class TelemetryIngestionServiceTests
{
    [Fact]
    public async Task FlakyBridgeDoesNotCrashIngestionAndFallsBackGracefully()
    {
        var bridge = new ScriptedBridgeClient
        {
            HealthProvider = _ => throw new HttpRequestException("bridge down"),
            StateProvider = _ => throw new HttpRequestException("bridge down"),
        };

        var db = new FakeRuntimeDbReader(
            new DbReaderStatus(false, "api-only", "db unavailable", DateTimeOffset.UtcNow)
        );

        var sut = CreateService(bridge, db);
        UnifiedTelemetryState? latest = null;
        sut.StateUpdated += (_, state) => latest = state;

        await sut.StartAsync();
        try
        {
            await WaitUntilAsync(() => latest is not null, TimeSpan.FromSeconds(2));
            Assert.NotNull(latest);
            Assert.False(latest!.ApiConnected);
            Assert.Equal("api-only", latest.DataMode);
        }
        finally
        {
            await sut.StopAsync();
        }
    }

    [Fact]
    public async Task DbUnavailableStartsInApiOnlyMode()
    {
        var bridge = new ScriptedBridgeClient();
        var db = new FakeRuntimeDbReader(new DbReaderStatus(false, "api-only", "db missing", DateTimeOffset.UtcNow));

        var sut = CreateService(bridge, db);
        UnifiedTelemetryState? latest = null;
        sut.StateUpdated += (_, state) => latest = state;

        await sut.StartAsync();
        try
        {
            await WaitUntilAsync(() => latest is not null, TimeSpan.FromSeconds(2));
            Assert.NotNull(latest);
            Assert.Equal("api-only", latest!.DataMode);
            Assert.Contains("SQLite", latest.WarningMessage, StringComparison.OrdinalIgnoreCase);
        }
        finally
        {
            await sut.StopAsync();
        }
    }

    [Fact]
    public async Task ReconnectDeduplicatesLifecycleEvents()
    {
        var cycle = 0;
        var bridge = new ScriptedBridgeClient
        {
            HealthProvider = _ =>
            {
                cycle++;
                return cycle switch
                {
                    2 => throw new HttpRequestException("restart"),
                    _ => new BridgeHealthSnapshot(DateTimeOffset.UtcNow, true, "healthy", string.Empty),
                };
            },
            CommandEventsProvider = _ =>
            {
                if (cycle <= 1)
                {
                    return
                    [
                        new CommandLifecycleRow("cmd-1", "queued", string.Empty, DateTimeOffset.UtcNow.AddSeconds(-1), new JsonObject()),
                    ];
                }

                if (cycle == 2)
                {
                    throw new HttpRequestException("bridge restarting");
                }

                return
                [
                    new CommandLifecycleRow("cmd-1", "queued", string.Empty, DateTimeOffset.UtcNow.AddSeconds(-1), new JsonObject()),
                    new CommandLifecycleRow("cmd-2", "queued", string.Empty, DateTimeOffset.UtcNow, new JsonObject()),
                ];
            },
        };

        var db = new FakeRuntimeDbReader(new DbReaderStatus(true, "api+sqlite", "ok", DateTimeOffset.UtcNow));
        var sut = CreateService(bridge, db);
        UnifiedTelemetryState? latest = null;
        sut.StateUpdated += (_, state) => latest = state;

        await sut.StartAsync();
        try
        {
            await WaitUntilAsync(() => latest?.CommandEvents.Count >= 2, TimeSpan.FromSeconds(3));
            Assert.NotNull(latest);
            Assert.Equal(2, latest!.CommandEvents.Count);
        }
        finally
        {
            await sut.StopAsync();
        }
    }

    [Fact]
    public async Task VisualTapEventsFlowIntoUnifiedState()
    {
        var bridge = new ScriptedBridgeClient
        {
            VisualTapProvider = _ =>
            [
                new IndicatorVisualEvent(
                    "EURUSD",
                    "arrow",
                    "BUY",
                    1.1000,
                    DateTimeOffset.UtcNow,
                    "Entry",
                    "Green",
                    new JsonObject())
            ],
        };

        var db = new FakeRuntimeDbReader(new DbReaderStatus(true, "api+sqlite", "ok", DateTimeOffset.UtcNow));
        var sut = CreateService(bridge, db);
        UnifiedTelemetryState? latest = null;
        sut.StateUpdated += (_, state) => latest = state;

        await sut.StartAsync();
        try
        {
            await WaitUntilAsync(() => latest?.VisualEvents.Count >= 1, TimeSpan.FromSeconds(2));
            Assert.NotNull(latest);
            Assert.Equal("arrow", latest!.VisualEvents[0].Type);
        }
        finally
        {
            await sut.StopAsync();
        }
    }

    private static TelemetryIngestionService CreateService(IBridgeTelemetryClient bridge, IRuntimeDbReader db)
    {
        var options = new DashboardOptions
        {
            Symbol = "EURUSD",
            FastPollInterval = TimeSpan.FromMilliseconds(100),
            MediumPollInterval = TimeSpan.FromMilliseconds(100),
            SlowPollInterval = TimeSpan.FromMilliseconds(100),
            BarsPollInterval = TimeSpan.FromMilliseconds(250),
            DbPollInterval = TimeSpan.FromMilliseconds(100),
        };

        return new TelemetryIngestionService(bridge, db, new TelemetryAggregator(), options);
    }

    private static async Task WaitUntilAsync(Func<bool> predicate, TimeSpan timeout)
    {
        var start = DateTimeOffset.UtcNow;
        while ((DateTimeOffset.UtcNow - start) <= timeout)
        {
            if (predicate())
            {
                return;
            }

            await Task.Delay(50);
        }

        throw new TimeoutException("Condition was not met before timeout.");
    }

    private sealed class FakeRuntimeDbReader : IRuntimeDbReader
    {
        private readonly DbReaderStatus _status;

        public FakeRuntimeDbReader(DbReaderStatus status)
        {
            _status = status;
        }

        public Task<DbReaderStatus> GetStatusAsync(CancellationToken cancellationToken)
            => Task.FromResult(_status);

        public Task<IReadOnlyList<AccountSnapshotRow>> ReadAccountSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
            => Task.FromResult((IReadOnlyList<AccountSnapshotRow>)Array.Empty<AccountSnapshotRow>());

        public Task<IReadOnlyList<PositionSnapshotRow>> ReadPositionSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
            => Task.FromResult((IReadOnlyList<PositionSnapshotRow>)Array.Empty<PositionSnapshotRow>());

        public Task<IReadOnlyList<DecisionSnapshotRow>> ReadDecisionSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
            => Task.FromResult((IReadOnlyList<DecisionSnapshotRow>)Array.Empty<DecisionSnapshotRow>());

        public Task<IReadOnlyList<GovernanceEventRow>> ReadGovernanceEventsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
            => Task.FromResult((IReadOnlyList<GovernanceEventRow>)Array.Empty<GovernanceEventRow>());
    }

    private sealed class ScriptedBridgeClient : IBridgeTelemetryClient
    {
        public Func<CancellationToken, BridgeHealthSnapshot>? HealthProvider { get; init; }
        public Func<CancellationToken, StateSnapshot?>? StateProvider { get; init; }
        public Func<CancellationToken, MetricsSnapshot?>? MetricsProvider { get; init; }
        public Func<CancellationToken, IReadOnlyList<ReportRow>>? ReportsProvider { get; init; }
        public Func<CancellationToken, IReadOnlyList<BridgeCommandRow>>? CommandsProvider { get; init; }
        public Func<CancellationToken, IReadOnlyList<CommandLifecycleRow>>? CommandEventsProvider { get; init; }
        public Func<CancellationToken, IReadOnlyList<GovernanceEventRow>>? GovernanceProvider { get; init; }
        public Func<CancellationToken, MonitorSnapshot?>? MonitorProvider { get; init; }
        public Func<CancellationToken, IReadOnlyDictionary<string, TickSnapshot>>? TicksProvider { get; init; }
        public Func<CancellationToken, IReadOnlyList<MarketBar>>? BarsProvider { get; init; }
        public Func<CancellationToken, IReadOnlyList<IndicatorVisualEvent>>? VisualTapProvider { get; init; }

        public Task<BridgeHealthSnapshot> GetHealthAsync(CancellationToken cancellationToken)
            => Task.FromResult(HealthProvider?.Invoke(cancellationToken)
                               ?? new BridgeHealthSnapshot(DateTimeOffset.UtcNow, true, "healthy", string.Empty));

        public Task<StateSnapshot?> GetStateAsync(CancellationToken cancellationToken)
            => Task.FromResult(StateProvider?.Invoke(cancellationToken)
                               ?? new StateSnapshot(
                                   DateTimeOffset.UtcNow,
                                   "connected",
                                   10000,
                                   0,
                                   10000,
                                   200,
                                   Array.Empty<PositionSnapshot>(),
                                   Array.Empty<LiveDecisionRow>(),
                                   DateTimeOffset.UtcNow,
                                   new JsonObject()));

        public Task<MetricsSnapshot?> GetMetricsAsync(CancellationToken cancellationToken)
            => Task.FromResult(MetricsProvider?.Invoke(cancellationToken)
                               ?? new MetricsSnapshot(
                                   DateTimeOffset.UtcNow,
                                   0,
                                   0.0,
                                   0.0,
                                   new Dictionary<string, int>(),
                                   new JsonObject()));

        public Task<IReadOnlyList<ReportRow>> GetReportsAsync(int limit, CancellationToken cancellationToken)
            => Task.FromResult(ReportsProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyList<ReportRow>)Array.Empty<ReportRow>());

        public Task<IReadOnlyList<BridgeCommandRow>> GetCommandsAsync(int limit, CancellationToken cancellationToken)
            => Task.FromResult(CommandsProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyList<BridgeCommandRow>)Array.Empty<BridgeCommandRow>());

        public Task<IReadOnlyList<CommandLifecycleRow>> GetCommandEventsAsync(int limit, string? commandId, CancellationToken cancellationToken)
            => Task.FromResult(CommandEventsProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyList<CommandLifecycleRow>)Array.Empty<CommandLifecycleRow>());

        public Task<IReadOnlyList<GovernanceEventRow>> GetGovernanceEventsAsync(int limit, CancellationToken cancellationToken)
            => Task.FromResult(GovernanceProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyList<GovernanceEventRow>)Array.Empty<GovernanceEventRow>());

        public Task<MonitorSnapshot?> GetMonitorAsync(CancellationToken cancellationToken)
            => Task.FromResult(MonitorProvider?.Invoke(cancellationToken)
                               ?? new MonitorSnapshot(DateTimeOffset.UtcNow, new JsonObject()));

        public Task<IReadOnlyDictionary<string, TickSnapshot>> GetTicksAsync(CancellationToken cancellationToken)
            => Task.FromResult(TicksProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyDictionary<string, TickSnapshot>)new Dictionary<string, TickSnapshot>());

        public Task<IReadOnlyList<MarketBar>> GetBarsAsync(string symbol, int limit, CancellationToken cancellationToken)
            => Task.FromResult(BarsProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyList<MarketBar>)Array.Empty<MarketBar>());

        public Task<IReadOnlyList<IndicatorVisualEvent>> GetVisualTapAsync(string symbol, int limit, CancellationToken cancellationToken)
            => Task.FromResult(VisualTapProvider?.Invoke(cancellationToken)
                               ?? (IReadOnlyList<IndicatorVisualEvent>)Array.Empty<IndicatorVisualEvent>());
    }
}
