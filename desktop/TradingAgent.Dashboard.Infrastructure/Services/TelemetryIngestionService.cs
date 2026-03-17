using TradingAgent.Dashboard.Core.Contracts;
using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Infrastructure.Configuration;

namespace TradingAgent.Dashboard.Infrastructure.Services;

public sealed class TelemetryIngestionService : IAsyncDisposable
{
    private readonly IBridgeTelemetryClient _bridgeClient;
    private readonly IRuntimeDbReader _dbReader;
    private readonly ITelemetryAggregator _aggregator;
    private readonly DashboardOptions _options;

    private readonly object _sync = new();

    private CancellationTokenSource? _cts;
    private Task? _runTask;
    private UnifiedTelemetryState _state = UnifiedTelemetryState.Empty;
    private DateTimeOffset _dbCursorUtc = DateTimeOffset.UtcNow - TimeSpan.FromHours(24);
    private DateTimeOffset _lastBarsFetchUtc = DateTimeOffset.MinValue;
    private DateTimeOffset _lastDbFetchUtc = DateTimeOffset.MinValue;

    public event EventHandler<UnifiedTelemetryState>? StateUpdated;

    public UnifiedTelemetryState CurrentState
    {
        get
        {
            lock (_sync)
            {
                return _state;
            }
        }
    }

    public TelemetryIngestionService(
        IBridgeTelemetryClient bridgeClient,
        IRuntimeDbReader dbReader,
        ITelemetryAggregator aggregator,
        DashboardOptions options
    )
    {
        _bridgeClient = bridgeClient;
        _dbReader = dbReader;
        _aggregator = aggregator;
        _options = options;
    }

    public Task StartAsync(CancellationToken cancellationToken = default)
    {
        if (_runTask is not null)
        {
            return Task.CompletedTask;
        }

        _cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _runTask = Task.Run(() => RunLoopAsync(_cts.Token), _cts.Token);
        return Task.CompletedTask;
    }

    public async Task StopAsync()
    {
        if (_cts is null)
        {
            return;
        }

        _cts.Cancel();
        if (_runTask is not null)
        {
            try
            {
                await _runTask.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
        }

        _cts.Dispose();
        _cts = null;
        _runTask = null;
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
    }

    private async Task RunLoopAsync(CancellationToken cancellationToken)
    {
        var cycle = 0;

        while (!cancellationToken.IsCancellationRequested)
        {
            cycle++;
            var started = DateTimeOffset.UtcNow;
            var api = await PollApiAsync(cycle, started, cancellationToken).ConfigureAwait(false);
            var db = await PollDbIfDueAsync(started, cancellationToken).ConfigureAwait(false);

            UnifiedTelemetryState updated;
            lock (_sync)
            {
                _state = _aggregator.Merge(_state, api, db, started);
                updated = _state;
            }

            StateUpdated?.Invoke(this, updated);

            var elapsed = DateTimeOffset.UtcNow - started;
            var wait = _options.FastPollInterval - elapsed;
            if (wait > TimeSpan.Zero)
            {
                await Task.Delay(wait, cancellationToken).ConfigureAwait(false);
            }
        }
    }

    private async Task<ApiTelemetrySnapshot> PollApiAsync(int cycle, DateTimeOffset nowUtc, CancellationToken cancellationToken)
    {
        var healthTask = SafeCallAsync(
            ct => _bridgeClient.GetHealthAsync(ct),
            new BridgeHealthSnapshot(nowUtc, false, "unreachable", "health poll failed"),
            cancellationToken
        );
        var stateTask = SafeCallAsync(
            ct => _bridgeClient.GetStateAsync(ct),
            default(StateSnapshot?),
            cancellationToken
        );
        var ticksTask = SafeCallAsync(
            ct => _bridgeClient.GetTicksAsync(ct),
            (IReadOnlyDictionary<string, TickSnapshot>)new Dictionary<string, TickSnapshot>(),
            cancellationToken
        );
        var visualsTask = SafeCallAsync(
            ct => _bridgeClient.GetVisualTapAsync(_options.Symbol, _options.VisualTapLimit, ct),
            (IReadOnlyList<IndicatorVisualEvent>)Array.Empty<IndicatorVisualEvent>(),
            cancellationToken
        );

        await Task.WhenAll(healthTask, stateTask, ticksTask, visualsTask).ConfigureAwait(false);

        MetricsSnapshot? metrics = null;
        MonitorSnapshot? monitor = null;
        IReadOnlyList<CommandLifecycleRow> events = Array.Empty<CommandLifecycleRow>();

        if (cycle % Math.Max(1, (int)Math.Round(_options.MediumPollInterval.TotalSeconds)) == 0)
        {
            var metricsTask = SafeCallAsync(ct => _bridgeClient.GetMetricsAsync(ct), default(MetricsSnapshot?), cancellationToken);
            var monitorTask = SafeCallAsync(ct => _bridgeClient.GetMonitorAsync(ct), default(MonitorSnapshot?), cancellationToken);
            var eventsTask = SafeCallAsync(
                ct => _bridgeClient.GetCommandEventsAsync(_options.EventsLimit, null, ct),
                (IReadOnlyList<CommandLifecycleRow>)Array.Empty<CommandLifecycleRow>(),
                cancellationToken
            );

            await Task.WhenAll(metricsTask, monitorTask, eventsTask).ConfigureAwait(false);

            metrics = metricsTask.Result;
            monitor = monitorTask.Result;
            events = eventsTask.Result;
        }

        IReadOnlyList<ReportRow> reports = Array.Empty<ReportRow>();
        IReadOnlyList<BridgeCommandRow> commands = Array.Empty<BridgeCommandRow>();
        IReadOnlyList<GovernanceEventRow> governance = Array.Empty<GovernanceEventRow>();

        if (cycle % Math.Max(1, (int)Math.Round(_options.SlowPollInterval.TotalSeconds)) == 0)
        {
            var reportsTask = SafeCallAsync(
                ct => _bridgeClient.GetReportsAsync(_options.ReportsLimit, ct),
                (IReadOnlyList<ReportRow>)Array.Empty<ReportRow>(),
                cancellationToken
            );
            var commandsTask = SafeCallAsync(
                ct => _bridgeClient.GetCommandsAsync(_options.CommandsLimit, ct),
                (IReadOnlyList<BridgeCommandRow>)Array.Empty<BridgeCommandRow>(),
                cancellationToken
            );
            var governanceTask = SafeCallAsync(
                ct => _bridgeClient.GetGovernanceEventsAsync(_options.GovernanceLimit, ct),
                (IReadOnlyList<GovernanceEventRow>)Array.Empty<GovernanceEventRow>(),
                cancellationToken
            );

            await Task.WhenAll(reportsTask, commandsTask, governanceTask).ConfigureAwait(false);

            reports = reportsTask.Result;
            commands = commandsTask.Result;
            governance = governanceTask.Result;
        }

        IReadOnlyList<MarketBar> bars = Array.Empty<MarketBar>();
        if ((nowUtc - _lastBarsFetchUtc) >= _options.BarsPollInterval)
        {
            bars = await SafeCallAsync(
                ct => _bridgeClient.GetBarsAsync(_options.Symbol, _options.BarsLimit, ct),
                (IReadOnlyList<MarketBar>)Array.Empty<MarketBar>(),
                cancellationToken
            ).ConfigureAwait(false);
            _lastBarsFetchUtc = nowUtc;
        }

        return new ApiTelemetrySnapshot
        {
            CapturedAtUtc = nowUtc,
            Health = healthTask.Result,
            State = stateTask.Result,
            Metrics = metrics,
            Monitor = monitor,
            Ticks = ticksTask.Result,
            VisualTapEvents = visualsTask.Result,
            CommandEvents = events,
            Reports = reports,
            Commands = commands,
            GovernanceEvents = governance,
            Bars = bars,
        };
    }

    private async Task<DbTelemetrySnapshot?> PollDbIfDueAsync(DateTimeOffset nowUtc, CancellationToken cancellationToken)
    {
        if ((nowUtc - _lastDbFetchUtc) < _options.DbPollInterval)
        {
            return null;
        }

        _lastDbFetchUtc = nowUtc;

        var status = await _dbReader.GetStatusAsync(cancellationToken).ConfigureAwait(false);
        if (!status.IsAvailable)
        {
            return new DbTelemetrySnapshot
            {
                CapturedAtUtc = nowUtc,
                Status = status,
            };
        }

        var accountTask = _dbReader.ReadAccountSnapshotsAsync(_dbCursorUtc, cancellationToken);
        var positionsTask = _dbReader.ReadPositionSnapshotsAsync(_dbCursorUtc, cancellationToken);
        var decisionsTask = _dbReader.ReadDecisionSnapshotsAsync(_dbCursorUtc, cancellationToken);
        var governanceTask = _dbReader.ReadGovernanceEventsAsync(_dbCursorUtc, cancellationToken);

        await Task.WhenAll(accountTask, positionsTask, decisionsTask, governanceTask).ConfigureAwait(false);

        var maxTs = new[]
        {
            accountTask.Result.LastOrDefault()?.TimeUtc,
            positionsTask.Result.LastOrDefault()?.TimeUtc,
            decisionsTask.Result.LastOrDefault()?.TimeUtc,
            governanceTask.Result.LastOrDefault()?.TimeUtc,
        }
        .Where(ts => ts.HasValue)
        .Select(ts => ts!.Value)
        .DefaultIfEmpty(_dbCursorUtc)
        .Max();

        _dbCursorUtc = maxTs;

        return new DbTelemetrySnapshot
        {
            CapturedAtUtc = nowUtc,
            Status = status,
            AccountSnapshots = accountTask.Result,
            PositionSnapshots = positionsTask.Result,
            DecisionSnapshots = decisionsTask.Result,
            GovernanceEvents = governanceTask.Result,
        };
    }

    private static async Task<T> SafeCallAsync<T>(Func<CancellationToken, Task<T>> action, T fallback, CancellationToken cancellationToken)
    {
        try
        {
            return await action(cancellationToken).ConfigureAwait(false);
        }
        catch
        {
            return fallback;
        }
    }
}
