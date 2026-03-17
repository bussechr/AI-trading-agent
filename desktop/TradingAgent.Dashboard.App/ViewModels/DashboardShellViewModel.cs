using System.Collections.ObjectModel;
using System.Windows;
using CommunityToolkit.Mvvm.ComponentModel;
using LiveChartsCore;
using LiveChartsCore.SkiaSharpView;
using LiveChartsCore.SkiaSharpView.Painting;
using SkiaSharp;
using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Core.Services;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Services;

namespace TradingAgent.Dashboard.App.ViewModels;

public sealed class DashboardShellViewModel : ObservableObject, IAsyncDisposable
{
    private readonly TelemetryIngestionService _ingestion;

    private readonly ObservableCollection<double> _equityValues = new();
    private readonly ObservableCollection<double> _drawdownValues = new();
    private readonly ObservableCollection<string> _timeLabels = new();

    private string _bridgeStatusLine = "Bridge: initializing";
    private string _dataModeLine = "Mode: api-only";
    private string _lastUpdateLine = "Updated: --";
    private string _warningMessage = string.Empty;
    private int _pendingCount;
    private double _ackTimeoutRate;
    private double _queueP95Ms;
    private string _riskRegime = "unknown";
    private double _softDdPct;
    private double _hardDdPct;
    private double _dailyBreakerPct;

    public DashboardShellViewModel(TelemetryIngestionService ingestion, DashboardOptions options)
    {
        _ingestion = ingestion;

        SelectedSymbol = options.Symbol;

        EquitySeries =
        [
            new LineSeries<double>
            {
                Values = _equityValues,
                GeometrySize = 0,
                Stroke = new SolidColorPaint(new SKColor(47, 184, 255), 2),
            },
        ];

        DrawdownSeries =
        [
            new LineSeries<double>
            {
                Values = _drawdownValues,
                GeometrySize = 0,
                Stroke = new SolidColorPaint(new SKColor(255, 106, 106), 2),
            },
        ];

        TimeAxis =
        [
            new Axis
            {
                Labels = _timeLabels,
                LabelsRotation = 0,
                TextSize = 10,
                LabelsPaint = new SolidColorPaint(new SKColor(147, 162, 180)),
            },
        ];

        ValueAxis =
        [
            new Axis
            {
                TextSize = 10,
                LabelsPaint = new SolidColorPaint(new SKColor(147, 162, 180)),
            },
        ];

        _ingestion.StateUpdated += OnStateUpdated;
    }

    public string SelectedSymbol { get; }

    public string BridgeStatusLine
    {
        get => _bridgeStatusLine;
        private set => SetProperty(ref _bridgeStatusLine, value);
    }

    public string DataModeLine
    {
        get => _dataModeLine;
        private set => SetProperty(ref _dataModeLine, value);
    }

    public string LastUpdateLine
    {
        get => _lastUpdateLine;
        private set => SetProperty(ref _lastUpdateLine, value);
    }

    public string WarningMessage
    {
        get => _warningMessage;
        private set => SetProperty(ref _warningMessage, value);
    }

    public int PendingCount
    {
        get => _pendingCount;
        private set => SetProperty(ref _pendingCount, value);
    }

    public double AckTimeoutRate
    {
        get => _ackTimeoutRate;
        private set => SetProperty(ref _ackTimeoutRate, value);
    }

    public double QueueP95Ms
    {
        get => _queueP95Ms;
        private set => SetProperty(ref _queueP95Ms, value);
    }

    public string RiskRegime
    {
        get => _riskRegime;
        private set => SetProperty(ref _riskRegime, value);
    }

    public double SoftDdPct
    {
        get => _softDdPct;
        private set => SetProperty(ref _softDdPct, value);
    }

    public double HardDdPct
    {
        get => _hardDdPct;
        private set => SetProperty(ref _hardDdPct, value);
    }

    public double DailyBreakerPct
    {
        get => _dailyBreakerPct;
        private set => SetProperty(ref _dailyBreakerPct, value);
    }

    public ObservableCollection<FlowStageBadge> FlowStages { get; } = new();
    public ObservableCollection<LiveDecisionRow> LiveDecisions { get; } = new();
    public ObservableCollection<PositionSnapshot> Positions { get; } = new();
    public ObservableCollection<CommandLifecycleRow> CommandEvents { get; } = new();
    public ObservableCollection<GovernanceEventRow> GovernanceEvents { get; } = new();
    public ObservableCollection<IndicatorVisualEvent> VisualEvents { get; } = new();
    public ObservableCollection<MarketBar> SymbolBars { get; } = new();
    public ObservableCollection<KeyValueMetricRow> ErrorBudgetRows { get; } = new();

    public ObservableCollection<ISeries> EquitySeries { get; }
    public ObservableCollection<ISeries> DrawdownSeries { get; }
    public ObservableCollection<Axis> TimeAxis { get; }
    public ObservableCollection<Axis> ValueAxis { get; }

    public Task StartAsync()
    {
        return _ingestion.StartAsync();
    }

    public async ValueTask DisposeAsync()
    {
        _ingestion.StateUpdated -= OnStateUpdated;
        await _ingestion.StopAsync();
    }

    private void OnStateUpdated(object? sender, UnifiedTelemetryState state)
    {
        _ = Application.Current.Dispatcher.InvokeAsync(() => ApplyState(state));
    }

    private void ApplyState(UnifiedTelemetryState state)
    {
        BridgeStatusLine = $"Bridge: {state.BridgeStatus} {(state.ApiStale ? "(stale)" : "")}".Trim();
        DataModeLine = $"Mode: {state.DataMode}";
        LastUpdateLine = $"Updated: {state.UpdatedAtUtc.ToLocalTime():HH:mm:ss}";
        WarningMessage = state.WarningMessage;

        PendingCount = state.Metrics?.PendingCount ?? 0;
        AckTimeoutRate = state.Metrics?.AckTimeoutRate5m ?? 0.0;
        QueueP95Ms = state.Metrics?.QueueToTerminalP95Ms ?? 0.0;

        var risk = state.Metrics?.Raw.GetObjectPath("risk_envelope");
        RiskRegime = risk.GetStringPath("regime", "unknown");
        SoftDdPct = risk.GetDoublePath("soft_dd_pct", 0.0);
        HardDdPct = risk.GetDoublePath("hard_dd_pct", 0.0);
        DailyBreakerPct = risk.GetDoublePath("daily_breaker_pct", 0.0);

        FlowStages.ReplaceWith(state.FlowStages);
        LiveDecisions.ReplaceWith((state.State?.AgentDecisions ?? Array.Empty<LiveDecisionRow>()).Take(200));
        Positions.ReplaceWith((state.State?.Positions ?? Array.Empty<PositionSnapshot>()).Take(200));
        CommandEvents.ReplaceWith(state.CommandEvents.OrderByDescending(row => row.TimeUtc).Take(500));
        GovernanceEvents.ReplaceWith(state.GovernanceEvents.OrderByDescending(row => row.TimeUtc).Take(500));
        VisualEvents.ReplaceWith(state.VisualEvents.OrderByDescending(row => row.TimeUtc).Take(500));
        SymbolBars.ReplaceWith(state.Bars.Where(row => string.Equals(row.Symbol, SelectedSymbol, StringComparison.OrdinalIgnoreCase)).TakeLast(400));
        ErrorBudgetRows.ReplaceWith((state.Metrics?.InteropErrorBudget ?? new Dictionary<string, int>())
            .OrderByDescending(pair => pair.Value)
            .Select(pair => new KeyValueMetricRow(pair.Key, pair.Value))
            .Take(100));

        UpdateCharts(state.EquityCurve24h, state.DrawdownCurve24h);
    }

    private void UpdateCharts(IReadOnlyList<AccountEquityPoint> equity, IReadOnlyList<DrawdownPoint> drawdown)
    {
        var reducedEquity = ReducePoints(equity, 240);
        var reducedDrawdown = ReducePoints(drawdown, 240);

        _equityValues.Clear();
        _drawdownValues.Clear();
        _timeLabels.Clear();

        foreach (var row in reducedEquity)
        {
            _equityValues.Add(row.Equity);
            _timeLabels.Add(row.TimeUtc.ToLocalTime().ToString("HH:mm"));
        }

        foreach (var row in reducedDrawdown)
        {
            _drawdownValues.Add(row.Drawdown);
        }
    }

    private static IReadOnlyList<AccountEquityPoint> ReducePoints(IReadOnlyList<AccountEquityPoint> rows, int maxPoints)
    {
        if (rows.Count <= maxPoints)
        {
            return rows;
        }

        var step = Math.Max(1, rows.Count / maxPoints);
        return rows.Where((_, idx) => idx % step == 0).TakeLast(maxPoints).ToArray();
    }

    private static IReadOnlyList<DrawdownPoint> ReducePoints(IReadOnlyList<DrawdownPoint> rows, int maxPoints)
    {
        if (rows.Count <= maxPoints)
        {
            return rows;
        }

        var step = Math.Max(1, rows.Count / maxPoints);
        return rows.Where((_, idx) => idx % step == 0).TakeLast(maxPoints).ToArray();
    }
}

public sealed record KeyValueMetricRow(string Key, int Value);
