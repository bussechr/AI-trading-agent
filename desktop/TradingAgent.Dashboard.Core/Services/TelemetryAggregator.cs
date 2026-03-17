using System.Text.Json.Nodes;
using TradingAgent.Dashboard.Core.Contracts;
using TradingAgent.Dashboard.Core.Models;

namespace TradingAgent.Dashboard.Core.Services;

public sealed class TelemetryAggregator : ITelemetryAggregator
{
    private static readonly TimeSpan DefaultWindow = TimeSpan.FromHours(24);
    private static readonly TimeSpan ApiStaleThreshold = TimeSpan.FromSeconds(5);

    public UnifiedTelemetryState Merge(
        UnifiedTelemetryState current,
        ApiTelemetrySnapshot? apiSnapshot,
        DbTelemetrySnapshot? dbSnapshot,
        DateTimeOffset nowUtc
    )
    {
        var next = current with
        {
            UpdatedAtUtc = nowUtc,
        };

        if (apiSnapshot is not null)
        {
            next = MergeApi(next, apiSnapshot);
        }

        if (dbSnapshot is not null)
        {
            next = MergeDb(next, dbSnapshot);
        }

        next = ApplyWindowAndDerivedViews(next, nowUtc);

        var apiStale = !next.LastApiSuccessUtc.HasValue || (nowUtc - next.LastApiSuccessUtc.Value) > ApiStaleThreshold;
        var warnings = BuildWarnings(next, apiStale);

        next = next with
        {
            ApiStale = apiStale,
            FlowStages = BuildFlowStages(next, nowUtc),
            WarningMessage = warnings,
        };

        return next;
    }

    private static UnifiedTelemetryState MergeApi(UnifiedTelemetryState current, ApiTelemetrySnapshot api)
    {
        var next = current;
        var apiConnected = current.ApiConnected;
        var bridgeStatus = current.BridgeStatus;
        DateTimeOffset? lastApiSuccessUtc = current.LastApiSuccessUtc;

        if (api.Health is not null)
        {
            apiConnected = api.Health.IsHealthy;
            bridgeStatus = string.IsNullOrWhiteSpace(api.Health.Status) ? bridgeStatus : api.Health.Status;
            if (api.Health.IsHealthy)
            {
                lastApiSuccessUtc = api.CapturedAtUtc;
            }
        }

        if (api.State is not null)
        {
            bridgeStatus = string.IsNullOrWhiteSpace(api.State.SystemStatus) ? bridgeStatus : api.State.SystemStatus;
            lastApiSuccessUtc = api.CapturedAtUtc;
            apiConnected = true;
        }

        if (api.Metrics is not null)
        {
            lastApiSuccessUtc = api.CapturedAtUtc;
        }

        next = next with
        {
            ApiConnected = apiConnected,
            BridgeStatus = bridgeStatus,
            LastApiSuccessUtc = lastApiSuccessUtc,
            Health = api.Health ?? current.Health,
            State = api.State ?? current.State,
            Metrics = api.Metrics ?? current.Metrics,
            Monitor = api.Monitor ?? current.Monitor,
            Ticks = api.Ticks.Count > 0 ? api.Ticks : current.Ticks,
            Bars = api.Bars.Count > 0 ? SortBars(api.Bars) : current.Bars,
            Reports = MergeDistinctBy(
                current.Reports,
                api.Reports,
                row => $"{row.TimeUtc.ToUnixTimeMilliseconds()}::{row.Message}",
                5000
            ),
            Commands = MergeDistinctBy(
                current.Commands,
                api.Commands,
                row => row.CommandId,
                3000,
                preferNew: true
            ),
            CommandEvents = MergeDistinctBy(
                current.CommandEvents,
                api.CommandEvents,
                row => $"{row.CommandId}:{row.Status}:{row.TimeUtc.ToUnixTimeMilliseconds()}",
                6000
            ),
            GovernanceEvents = MergeDistinctBy(
                current.GovernanceEvents,
                api.GovernanceEvents,
                row => $"{row.EventType}:{row.TimeUtc.ToUnixTimeMilliseconds()}:{row.Reason}",
                2000
            ),
            VisualEvents = MergeDistinctBy(
                current.VisualEvents,
                api.VisualTapEvents,
                row => $"{row.Symbol}:{row.Type}:{row.TimeUtc.ToUnixTimeMilliseconds()}:{row.Text}",
                2000
            ),
        };

        return next;
    }

    private static UnifiedTelemetryState MergeDb(UnifiedTelemetryState current, DbTelemetrySnapshot db)
    {
        var status = db.Status;
        var dataMode = current.DataMode;
        var warning = current.WarningMessage;
        DateTimeOffset? lastDbSuccessUtc = current.LastDbSuccessUtc;

        if (status.IsAvailable)
        {
            dataMode = "api+sqlite";
            lastDbSuccessUtc = db.CapturedAtUtc;
        }
        else if (!current.ApiConnected)
        {
            dataMode = "sqlite-fallback";
            warning = status.Message;
        }
        else
        {
            dataMode = "api-only";
            warning = status.Message;
        }

        var equityPointsFromDb = db.AccountSnapshots
            .Select(row => new AccountEquityPoint(row.TimeUtc, row.Equity))
            .ToArray();

        var mergedEquity = MergeDistinctBy(
            current.EquityCurve24h,
            equityPointsFromDb,
            row => row.TimeUtc.ToUnixTimeMilliseconds(),
            20000
        );

        var mergedGovernance = MergeDistinctBy(
            current.GovernanceEvents,
            db.GovernanceEvents,
            row => $"{row.EventType}:{row.TimeUtc.ToUnixTimeMilliseconds()}:{row.Reason}",
            4000
        );

        var decisionPipeline = BuildDecisionPipelineSnapshot(current.DecisionPipeline, current.Metrics, db.DecisionSnapshots);

        return current with
        {
            LastDbSuccessUtc = lastDbSuccessUtc,
            DataMode = dataMode,
            WarningMessage = warning,
            EquityCurve24h = mergedEquity,
            GovernanceEvents = mergedGovernance,
            DecisionPipeline = decisionPipeline,
        };
    }

    private static UnifiedTelemetryState ApplyWindowAndDerivedViews(UnifiedTelemetryState state, DateTimeOffset nowUtc)
    {
        var cutoff = nowUtc - DefaultWindow;

        var reports = state.Reports
            .Where(row => row.TimeUtc >= cutoff)
            .OrderBy(row => row.TimeUtc)
            .TakeLast(5000)
            .ToArray();

        var commandEvents = state.CommandEvents
            .Where(row => row.TimeUtc >= cutoff)
            .OrderBy(row => row.TimeUtc)
            .TakeLast(6000)
            .ToArray();

        var governanceEvents = state.GovernanceEvents
            .Where(row => row.TimeUtc >= cutoff)
            .OrderBy(row => row.TimeUtc)
            .TakeLast(4000)
            .ToArray();

        var visuals = state.VisualEvents
            .Where(row => row.TimeUtc >= cutoff)
            .OrderBy(row => row.TimeUtc)
            .TakeLast(2000)
            .ToArray();

        var commands = state.Commands
            .OrderByDescending(row => row.CreatedAtUtc)
            .Take(3000)
            .ToArray();

        var equity = state.EquityCurve24h
            .Where(row => row.TimeUtc >= cutoff)
            .OrderBy(row => row.TimeUtc)
            .TakeLast(20000)
            .ToArray();

        if (equity.Length == 0 && state.State is not null && state.State.Equity > 0)
        {
            equity =
            [
                new AccountEquityPoint(state.State.CapturedAtUtc, state.State.Equity),
            ];
        }

        var drawdown = BuildDrawdownCurve(equity);

        return state with
        {
            Reports = reports,
            CommandEvents = commandEvents,
            GovernanceEvents = governanceEvents,
            VisualEvents = visuals,
            Commands = commands,
            EquityCurve24h = equity,
            DrawdownCurve24h = drawdown,
        };
    }

    private static IReadOnlyList<DrawdownPoint> BuildDrawdownCurve(IReadOnlyList<AccountEquityPoint> points)
    {
        if (points.Count == 0)
        {
            return Array.Empty<DrawdownPoint>();
        }

        var ordered = points.OrderBy(row => row.TimeUtc).ToArray();
        var peak = ordered[0].Equity;
        var outRows = new List<DrawdownPoint>(ordered.Length);

        foreach (var row in ordered)
        {
            peak = Math.Max(peak, row.Equity);
            var dd = row.Equity - peak;
            outRows.Add(new DrawdownPoint(row.TimeUtc, dd));
        }

        return outRows;
    }

    private static DecisionPipelineSnapshot? BuildDecisionPipelineSnapshot(
        DecisionPipelineSnapshot? current,
        MetricsSnapshot? metrics,
        IReadOnlyList<DecisionSnapshotRow> snapshots
    )
    {
        if (snapshots.Count > 0)
        {
            var latest = snapshots.OrderBy(row => row.TimeUtc).Last();
            return new DecisionPipelineSnapshot(
                latest.TimeUtc,
                0,
                ToIntDictionary(latest.Rejection),
                latest.Attribution
            );
        }

        if (metrics is null)
        {
            return current;
        }

        var taxonomy = ToIntDictionary(metrics.Raw.GetObjectPath("decision_pipeline.rejection_taxonomy"));
        var stage = metrics.Raw.GetObjectPath("decision_pipeline.stage_attribution");
        var snapshots5m = metrics.Raw.GetIntPath("decision_pipeline.snapshots_5m", 0);

        return new DecisionPipelineSnapshot(metrics.CapturedAtUtc, snapshots5m, taxonomy, stage);
    }

    private static IReadOnlyDictionary<string, int> ToIntDictionary(JsonObject? source)
    {
        if (source is null)
        {
            return new Dictionary<string, int>();
        }

        var output = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        foreach (var pair in source)
        {
            if (pair.Value is null)
            {
                continue;
            }

            var value = 0;
            if (pair.Value.TryGetValue<int>(out var iv))
            {
                value = iv;
            }
            else if (pair.Value.TryGetValue<double>(out var dv))
            {
                value = (int)dv;
            }
            else
            {
                _ = int.TryParse(pair.Value.ToString(), out value);
            }

            output[pair.Key] = value;
        }

        return output;
    }

    private static IReadOnlyList<FlowStageBadge> BuildFlowStages(UnifiedTelemetryState state, DateTimeOffset nowUtc)
    {
        var metrics = state.Metrics;
        var hasRecentTicks = state.Ticks.Values.Any(row => (nowUtc - row.TimeUtc) <= TimeSpan.FromSeconds(10));
        var hasDecisions = state.State?.AgentDecisions.Count > 0;
        var pending = metrics?.PendingCount ?? 0;
        var queueP95 = metrics?.QueueToTerminalP95Ms ?? 0;

        var stages = new List<FlowStageBadge>
        {
            new("tick_ingest", hasRecentTicks ? "ok" : "stale", 0.0, hasRecentTicks ? "live ticks" : "no recent ticks"),
            new("decision_ready", hasDecisions ? "ok" : "idle", 0.0, hasDecisions ? "active candidates" : "no active candidates"),
            new("signal_post", pending > 0 ? "busy" : "ok", 0.0, $"pending={pending}"),
            new("bridge_queue", pending > 0 ? "busy" : "ok", queueP95, $"queue p95={queueP95:0}ms"),
            new("poll_delivery", queueP95 > 2500 ? "warn" : "ok", queueP95, $"queue->terminal p95={queueP95:0}ms"),
            new("ea_handle", queueP95 > 4000 ? "warn" : "ok", queueP95, "derived from lifecycle latency"),
            new("ack_finalize", state.ApiStale ? "stale" : "ok", queueP95, state.ApiStale ? "api stale" : "ack path healthy"),
        };

        return stages;
    }

    private static string BuildWarnings(UnifiedTelemetryState state, bool apiStale)
    {
        var parts = new List<string>();

        if (!state.ApiConnected)
        {
            parts.Add("Bridge API unreachable");
        }
        else if (apiStale)
        {
            parts.Add("Bridge API stale");
        }

        if (state.DataMode == "api-only")
        {
            parts.Add("SQLite mirror unavailable, running API-only");
        }

        if (!string.IsNullOrWhiteSpace(state.WarningMessage))
        {
            parts.Add(state.WarningMessage);
        }

        return string.Join(" | ", parts.Distinct(StringComparer.OrdinalIgnoreCase));
    }

    private static IReadOnlyList<MarketBar> SortBars(IReadOnlyList<MarketBar> bars)
    {
        return bars
            .OrderBy(row => row.TimeUtc)
            .TakeLast(400)
            .ToArray();
    }

    private static IReadOnlyList<T> MergeDistinctBy<T, TKey>(
        IReadOnlyList<T> current,
        IReadOnlyList<T> incoming,
        Func<T, TKey> keySelector,
        int maxCount,
        bool preferNew = false
    ) where TKey : notnull
    {
        if (incoming.Count == 0)
        {
            return current;
        }

        var map = new Dictionary<TKey, T>();

        foreach (var row in current)
        {
            map[keySelector(row)] = row;
        }

        foreach (var row in incoming)
        {
            var key = keySelector(row);
            if (preferNew || !map.ContainsKey(key))
            {
                map[key] = row;
            }
        }

        return map.Values.TakeLast(maxCount).ToArray();
    }
}
