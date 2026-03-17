using System.Text.Json.Nodes;

namespace TradingAgent.Dashboard.Core.Models;

public sealed record PositionSnapshot(
    string Symbol,
    string Side,
    double Lots,
    double Profit,
    double OpenPrice,
    DateTimeOffset? OpenTimeUtc
);

public sealed record LiveDecisionRow(
    string Symbol,
    string Side,
    double Score,
    double Confidence,
    double Price,
    double TargetPct,
    string Reason,
    bool IsAdd
);

public sealed record StateSnapshot(
    DateTimeOffset CapturedAtUtc,
    string SystemStatus,
    double Equity,
    double Margin,
    double FreeMargin,
    double Leverage,
    IReadOnlyList<PositionSnapshot> Positions,
    IReadOnlyList<LiveDecisionRow> AgentDecisions,
    DateTimeOffset? LastHeartbeatUtc,
    JsonObject Raw
);

public sealed record MetricsSnapshot(
    DateTimeOffset CapturedAtUtc,
    int PendingCount,
    double AckTimeoutRate5m,
    double QueueToTerminalP95Ms,
    IReadOnlyDictionary<string, int> InteropErrorBudget,
    JsonObject Raw
);

public sealed record MonitorSnapshot(
    DateTimeOffset CapturedAtUtc,
    JsonObject Payload
);

public sealed record ReportRow(
    DateTimeOffset TimeUtc,
    string Message,
    JsonObject Payload
);

public sealed record BridgeCommandRow(
    string CommandId,
    string Status,
    string Symbol,
    string Command,
    double Lots,
    DateTimeOffset CreatedAtUtc,
    DateTimeOffset UpdatedAtUtc,
    int DeliveredCount,
    string Reason
);

public sealed record CommandLifecycleRow(
    string CommandId,
    string Status,
    string Reason,
    DateTimeOffset TimeUtc,
    JsonObject Payload
);

public sealed record GovernanceEventRow(
    DateTimeOffset TimeUtc,
    string EventType,
    string Reason,
    JsonObject Payload
);

public sealed record TickSnapshot(
    string Symbol,
    double Bid,
    double Ask,
    double Spread,
    DateTimeOffset TimeUtc
);

public sealed record MarketBar(
    string Symbol,
    DateTimeOffset TimeUtc,
    double Open,
    double High,
    double Low,
    double Close,
    double Volume
);

public sealed record IndicatorVisualEvent(
    string Symbol,
    string Type,
    string Side,
    double Price,
    DateTimeOffset TimeUtc,
    string Text,
    string Color,
    JsonObject Payload
);

public sealed record BridgeHealthSnapshot(
    DateTimeOffset CheckedAtUtc,
    bool IsHealthy,
    string Status,
    string Error
);

public sealed record AccountSnapshotRow(
    DateTimeOffset TimeUtc,
    double Equity,
    double Margin,
    double FreeMargin,
    double Leverage,
    string Source,
    JsonObject Payload
);

public sealed record PositionSnapshotRow(
    DateTimeOffset TimeUtc,
    string Source,
    IReadOnlyList<PositionSnapshot> Positions
);

public sealed record DecisionSnapshotRow(
    DateTimeOffset TimeUtc,
    double Vol,
    JsonObject Diagnostics,
    JsonObject Rejection,
    JsonObject Attribution
);

public sealed record AccountEquityPoint(
    DateTimeOffset TimeUtc,
    double Equity
);

public sealed record DrawdownPoint(
    DateTimeOffset TimeUtc,
    double Drawdown
);

public sealed record DecisionPipelineSnapshot(
    DateTimeOffset TimeUtc,
    int Snapshots5m,
    IReadOnlyDictionary<string, int> RejectionTaxonomy,
    JsonObject StageAttribution
);

public sealed record FlowStageBadge(
    string Stage,
    string Status,
    double LatencyMs,
    string Detail
);

public sealed record DbReaderStatus(
    bool IsAvailable,
    string Mode,
    string Message,
    DateTimeOffset CheckedAtUtc
);
