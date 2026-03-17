namespace TradingAgent.Dashboard.Core.Models;

public sealed record ApiTelemetrySnapshot
{
    public DateTimeOffset CapturedAtUtc { get; init; } = DateTimeOffset.UtcNow;
    public BridgeHealthSnapshot? Health { get; init; }
    public StateSnapshot? State { get; init; }
    public MetricsSnapshot? Metrics { get; init; }
    public MonitorSnapshot? Monitor { get; init; }
    public IReadOnlyList<ReportRow> Reports { get; init; } = Array.Empty<ReportRow>();
    public IReadOnlyList<BridgeCommandRow> Commands { get; init; } = Array.Empty<BridgeCommandRow>();
    public IReadOnlyList<CommandLifecycleRow> CommandEvents { get; init; } = Array.Empty<CommandLifecycleRow>();
    public IReadOnlyList<GovernanceEventRow> GovernanceEvents { get; init; } = Array.Empty<GovernanceEventRow>();
    public IReadOnlyDictionary<string, TickSnapshot> Ticks { get; init; } = new Dictionary<string, TickSnapshot>();
    public IReadOnlyList<MarketBar> Bars { get; init; } = Array.Empty<MarketBar>();
    public IReadOnlyList<IndicatorVisualEvent> VisualTapEvents { get; init; } = Array.Empty<IndicatorVisualEvent>();
}

public sealed record DbTelemetrySnapshot
{
    public DateTimeOffset CapturedAtUtc { get; init; } = DateTimeOffset.UtcNow;
    public DbReaderStatus Status { get; init; } = new(false, "api-only", "DB not checked", DateTimeOffset.UtcNow);
    public IReadOnlyList<AccountSnapshotRow> AccountSnapshots { get; init; } = Array.Empty<AccountSnapshotRow>();
    public IReadOnlyList<PositionSnapshotRow> PositionSnapshots { get; init; } = Array.Empty<PositionSnapshotRow>();
    public IReadOnlyList<DecisionSnapshotRow> DecisionSnapshots { get; init; } = Array.Empty<DecisionSnapshotRow>();
    public IReadOnlyList<GovernanceEventRow> GovernanceEvents { get; init; } = Array.Empty<GovernanceEventRow>();
}

public sealed record UnifiedTelemetryState
{
    public static UnifiedTelemetryState Empty { get; } = new();

    public DateTimeOffset UpdatedAtUtc { get; init; } = DateTimeOffset.UtcNow;
    public DateTimeOffset? LastApiSuccessUtc { get; init; }
    public DateTimeOffset? LastDbSuccessUtc { get; init; }
    public bool ApiConnected { get; init; }
    public bool ApiStale { get; init; }
    public string BridgeStatus { get; init; } = "unknown";
    public string DataMode { get; init; } = "api-only";
    public string WarningMessage { get; init; } = string.Empty;

    public BridgeHealthSnapshot? Health { get; init; }
    public StateSnapshot? State { get; init; }
    public MetricsSnapshot? Metrics { get; init; }
    public MonitorSnapshot? Monitor { get; init; }

    public IReadOnlyDictionary<string, TickSnapshot> Ticks { get; init; } = new Dictionary<string, TickSnapshot>();
    public IReadOnlyList<MarketBar> Bars { get; init; } = Array.Empty<MarketBar>();

    public IReadOnlyList<FlowStageBadge> FlowStages { get; init; } = Array.Empty<FlowStageBadge>();
    public IReadOnlyList<ReportRow> Reports { get; init; } = Array.Empty<ReportRow>();
    public IReadOnlyList<BridgeCommandRow> Commands { get; init; } = Array.Empty<BridgeCommandRow>();
    public IReadOnlyList<CommandLifecycleRow> CommandEvents { get; init; } = Array.Empty<CommandLifecycleRow>();
    public IReadOnlyList<GovernanceEventRow> GovernanceEvents { get; init; } = Array.Empty<GovernanceEventRow>();
    public IReadOnlyList<IndicatorVisualEvent> VisualEvents { get; init; } = Array.Empty<IndicatorVisualEvent>();

    public IReadOnlyList<AccountEquityPoint> EquityCurve24h { get; init; } = Array.Empty<AccountEquityPoint>();
    public IReadOnlyList<DrawdownPoint> DrawdownCurve24h { get; init; } = Array.Empty<DrawdownPoint>();
    public DecisionPipelineSnapshot? DecisionPipeline { get; init; }
}
