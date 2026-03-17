using System.Globalization;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json.Nodes;
using TradingAgent.Dashboard.Core.Contracts;
using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Utilities;

namespace TradingAgent.Dashboard.Infrastructure.Services;

public sealed class BridgeTelemetryClient : IBridgeTelemetryClient
{
    private readonly HttpClient _httpClient;
    private readonly Polly.Wrap.AsyncPolicyWrap<HttpResponseMessage> _policy;

    public BridgeTelemetryClient(HttpClient httpClient, DashboardOptions options)
    {
        _httpClient = httpClient;
        _httpClient.BaseAddress = new Uri(options.BridgeBaseUrl.TrimEnd('/') + "/", UriKind.Absolute);
        _httpClient.Timeout = TimeSpan.FromSeconds(2);
        _policy = ResiliencePolicies.BuildHttpPolicy();
    }

    public async Task<BridgeHealthSnapshot> GetHealthAsync(CancellationToken cancellationToken)
    {
        try
        {
            var payload = await GetObjectAsync("v2/health", cancellationToken);
            if (payload is null)
            {
                return new BridgeHealthSnapshot(DateTimeOffset.UtcNow, false, "unreachable", "Empty response");
            }

            var status = ReadString(payload, "status", "unknown");
            var healthy = string.Equals(status, "healthy", StringComparison.OrdinalIgnoreCase) ||
                          string.Equals(status, "success", StringComparison.OrdinalIgnoreCase);
            var error = ReadString(payload, "error", string.Empty);

            return new BridgeHealthSnapshot(DateTimeOffset.UtcNow, healthy, status, error);
        }
        catch (Exception exc)
        {
            return new BridgeHealthSnapshot(DateTimeOffset.UtcNow, false, "unreachable", exc.Message);
        }
    }

    public async Task<StateSnapshot?> GetStateAsync(CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync("v2/state", cancellationToken);
        if (payload is null)
        {
            return null;
        }

        var positions = new List<PositionSnapshot>();
        foreach (var row in ReadArray(payload, "positions"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            positions.Add(new PositionSnapshot(
                Symbol: ReadString(obj, "symbol", ""),
                Side: ParsePositionSide(obj),
                Lots: ReadDouble(obj, "lots", 0.0),
                Profit: ReadDouble(obj, "profit", 0.0),
                OpenPrice: ReadDouble(obj, "open_price", 0.0),
                OpenTimeUtc: ParseTimestamp(obj["open_time"]) 
            ));
        }

        var decisions = new List<LiveDecisionRow>();
        foreach (var row in ReadArray(payload, "agent_decisions"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            decisions.Add(new LiveDecisionRow(
                Symbol: ReadString(obj, "symbol", ""),
                Side: ReadString(obj, "side", "NONE"),
                Score: ReadDouble(obj, "score", 0.0),
                Confidence: ReadDouble(obj, "confidence", 0.0),
                Price: ReadDouble(obj, "price", 0.0),
                TargetPct: ReadDouble(obj, "target_pct", 0.0),
                Reason: ReadString(obj, "reason", ""),
                IsAdd: ReadBool(obj, "is_add", false)
            ));
        }

        return new StateSnapshot(
            CapturedAtUtc: DateTimeOffset.UtcNow,
            SystemStatus: ReadString(payload, "system_status", "unknown"),
            Equity: ReadDouble(payload, "equity", 0.0),
            Margin: ReadDouble(payload, "margin", 0.0),
            FreeMargin: ReadDouble(payload, "freemargin", 0.0),
            Leverage: ReadDouble(payload, "leverage", 0.0),
            Positions: positions,
            AgentDecisions: decisions,
            LastHeartbeatUtc: ParseTimestamp(payload["last_heartbeat"]),
            Raw: payload
        );
    }

    public async Task<MetricsSnapshot?> GetMetricsAsync(CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync("v2/metrics", cancellationToken);
        if (payload is null)
        {
            return null;
        }

        var pendingCount = ReadInt(ReadObject(payload, "pending"), "count", 0);
        var ackTimeoutRate = ReadDouble(ReadObject(payload, "timeouts"), "ack_timeout_rate_5m", 0.0);
        var lifecycle = ReadObject(payload, "lifecycle_latency_ms");
        var queueToTerminalP95 = ReadDouble(lifecycle["queue_to_terminal"] as JsonObject, "p95", 0.0);
        var errorBudget = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        foreach (var pair in ReadObject(ReadObject(payload, "interop"), "error_budget"))
        {
            if (pair.Value is null)
            {
                continue;
            }

            if (int.TryParse(pair.Value.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed))
            {
                errorBudget[pair.Key] = parsed;
            }
        }

        return new MetricsSnapshot(
            CapturedAtUtc: DateTimeOffset.UtcNow,
            PendingCount: pendingCount,
            AckTimeoutRate5m: ackTimeoutRate,
            QueueToTerminalP95Ms: queueToTerminalP95,
            InteropErrorBudget: errorBudget,
            Raw: payload
        );
    }

    public async Task<IReadOnlyList<ReportRow>> GetReportsAsync(int limit, CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync($"v2/reports?limit={Clamp(limit, 1, 2000)}", cancellationToken);
        if (payload is null)
        {
            return Array.Empty<ReportRow>();
        }

        var rows = new List<ReportRow>();
        foreach (var row in ReadArray(payload, "reports"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            rows.Add(new ReportRow(
                TimeUtc: ParseTimestamp(obj["time"]) ?? DateTimeOffset.UtcNow,
                Message: ReadString(obj, "message", string.Empty),
                Payload: obj["json"] as JsonObject ?? new JsonObject()
            ));
        }

        return rows;
    }

    public async Task<IReadOnlyList<BridgeCommandRow>> GetCommandsAsync(int limit, CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync($"v2/commands/history?limit={Clamp(limit, 1, 5000)}", cancellationToken);
        if (payload is null)
        {
            return Array.Empty<BridgeCommandRow>();
        }

        var rows = new List<BridgeCommandRow>();
        foreach (var row in ReadArray(payload, "commands"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            rows.Add(new BridgeCommandRow(
                CommandId: ReadString(obj, "command_id", string.Empty),
                Status: ReadString(obj, "status", "unknown"),
                Symbol: ReadString(obj, "symbol", string.Empty),
                Command: ReadString(obj, "cmd", string.Empty),
                Lots: ReadDouble(obj, "lots", 0.0),
                CreatedAtUtc: ParseTimestamp(obj["created_at"]) ?? DateTimeOffset.UtcNow,
                UpdatedAtUtc: ParseTimestamp(obj["updated_at"]) ?? DateTimeOffset.UtcNow,
                DeliveredCount: ReadInt(obj, "delivered_count", 0),
                Reason: ReadString(obj, "reason", string.Empty)
            ));
        }

        return rows;
    }

    public async Task<IReadOnlyList<CommandLifecycleRow>> GetCommandEventsAsync(int limit, string? commandId, CancellationToken cancellationToken)
    {
        var query = $"v2/commands/events?limit={Clamp(limit, 1, 10000)}";
        if (!string.IsNullOrWhiteSpace(commandId))
        {
            query += $"&command_id={Uri.EscapeDataString(commandId)}";
        }

        var payload = await GetObjectAsync(query, cancellationToken);
        if (payload is null)
        {
            return Array.Empty<CommandLifecycleRow>();
        }

        var rows = new List<CommandLifecycleRow>();
        foreach (var row in ReadArray(payload, "events"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            rows.Add(new CommandLifecycleRow(
                CommandId: ReadString(obj, "command_id", string.Empty),
                Status: ReadString(obj, "status", "unknown"),
                Reason: ReadString(obj, "reason", string.Empty),
                TimeUtc: ParseTimestamp(obj["time"]) ?? DateTimeOffset.UtcNow,
                Payload: obj["payload"] as JsonObject ?? new JsonObject()
            ));
        }

        return rows;
    }

    public async Task<IReadOnlyList<GovernanceEventRow>> GetGovernanceEventsAsync(int limit, CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync($"v2/governance/events?limit={Clamp(limit, 1, 2000)}", cancellationToken);
        if (payload is null)
        {
            return Array.Empty<GovernanceEventRow>();
        }

        var rows = new List<GovernanceEventRow>();
        foreach (var row in ReadArray(payload, "events"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            rows.Add(new GovernanceEventRow(
                TimeUtc: ParseTimestamp(obj["time"]) ?? DateTimeOffset.UtcNow,
                EventType: ReadString(obj, "event_type", "state_update"),
                Reason: ReadString(obj, "reason", string.Empty),
                Payload: obj["payload"] as JsonObject ?? new JsonObject()
            ));
        }

        return rows;
    }

    public async Task<MonitorSnapshot?> GetMonitorAsync(CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync("v2/monitor", cancellationToken);
        return payload is null ? null : new MonitorSnapshot(DateTimeOffset.UtcNow, payload);
    }

    public async Task<IReadOnlyDictionary<string, TickSnapshot>> GetTicksAsync(CancellationToken cancellationToken)
    {
        var payload = await GetObjectAsync("v2/market/ticks", cancellationToken);
        if (payload is null)
        {
            return new Dictionary<string, TickSnapshot>();
        }

        var rows = new Dictionary<string, TickSnapshot>(StringComparer.OrdinalIgnoreCase);
        foreach (var pair in payload)
        {
            if (pair.Value is not JsonObject obj)
            {
                continue;
            }

            rows[pair.Key] = new TickSnapshot(
                Symbol: pair.Key,
                Bid: ReadDouble(obj, "bid", 0.0),
                Ask: ReadDouble(obj, "ask", 0.0),
                Spread: ReadDouble(obj, "spread", 0.0),
                TimeUtc: ParseTimestamp(obj["time"]) ?? DateTimeOffset.UtcNow
            );
        }

        return rows;
    }

    public async Task<IReadOnlyList<MarketBar>> GetBarsAsync(string symbol, int limit, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(symbol))
        {
            return Array.Empty<MarketBar>();
        }

        var payload = await GetObjectAsync($"v2/market/bars?symbol={Uri.EscapeDataString(symbol)}&timeframe=H1&limit={Clamp(limit, 1, 2000)}", cancellationToken);
        if (payload is null)
        {
            return Array.Empty<MarketBar>();
        }

        var outRows = new List<MarketBar>();
        foreach (var row in ReadArray(payload, "bars"))
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            outRows.Add(new MarketBar(
                Symbol: symbol,
                TimeUtc: ParseTimestamp(obj["time"]) ?? DateTimeOffset.UtcNow,
                Open: ReadDouble(obj, "open", 0.0),
                High: ReadDouble(obj, "high", 0.0),
                Low: ReadDouble(obj, "low", 0.0),
                Close: ReadDouble(obj, "close", 0.0),
                Volume: ReadDouble(obj, "volume", 0.0)
            ));
        }

        return outRows;
    }

    public async Task<IReadOnlyList<IndicatorVisualEvent>> GetVisualTapAsync(string symbol, int limit, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(symbol))
        {
            return Array.Empty<IndicatorVisualEvent>();
        }

        var payload = await GetArrayAsync($"v2/visuals/tap?symbol={Uri.EscapeDataString(symbol)}&limit={Clamp(limit, 1, 200)}", cancellationToken);
        if (payload is null)
        {
            return Array.Empty<IndicatorVisualEvent>();
        }

        var outRows = new List<IndicatorVisualEvent>();
        foreach (var row in payload)
        {
            if (row is not JsonObject obj)
            {
                continue;
            }

            outRows.Add(new IndicatorVisualEvent(
                Symbol: ReadString(obj, "symbol", symbol),
                Type: ReadString(obj, "type", "unknown"),
                Side: ReadString(obj, "side", "NONE"),
                Price: ReadDouble(obj, "price", 0.0),
                TimeUtc: ParseTimestamp(obj["time"]) ?? DateTimeOffset.UtcNow,
                Text: ReadString(obj, "text", string.Empty),
                Color: ReadString(obj, "color", string.Empty),
                Payload: obj
            ));
        }

        return outRows;
    }

    private async Task<JsonObject?> GetObjectAsync(string relativePath, CancellationToken cancellationToken)
    {
        var response = await _policy.ExecuteAsync(
            ct => _httpClient.GetAsync(relativePath, ct),
            cancellationToken
        );

        if (!response.IsSuccessStatusCode)
        {
            if (response.StatusCode == HttpStatusCode.NotFound)
            {
                return null;
            }
            response.EnsureSuccessStatusCode();
        }

        var node = await response.Content.ReadFromJsonAsync<JsonNode>(cancellationToken: cancellationToken);
        return node as JsonObject;
    }

    private async Task<JsonArray?> GetArrayAsync(string relativePath, CancellationToken cancellationToken)
    {
        var response = await _policy.ExecuteAsync(
            ct => _httpClient.GetAsync(relativePath, ct),
            cancellationToken
        );

        if (!response.IsSuccessStatusCode)
        {
            if (response.StatusCode == HttpStatusCode.NotFound)
            {
                return null;
            }
            response.EnsureSuccessStatusCode();
        }

        var node = await response.Content.ReadFromJsonAsync<JsonNode>(cancellationToken: cancellationToken);
        return node as JsonArray;
    }

    private static JsonArray ReadArray(JsonObject? obj, string prop)
    {
        return obj?[prop] as JsonArray ?? new JsonArray();
    }

    private static JsonObject ReadObject(JsonObject? obj, string prop)
    {
        return obj?[prop] as JsonObject ?? new JsonObject();
    }

    private static string ReadString(JsonObject? obj, string prop, string fallback)
    {
        if (obj is null || !obj.TryGetPropertyValue(prop, out var node) || node is null)
        {
            return fallback;
        }

        if (node.TryGetValue<string>(out var s) && !string.IsNullOrWhiteSpace(s))
        {
            return s;
        }

        var asString = node.ToString();
        return string.IsNullOrWhiteSpace(asString) ? fallback : asString;
    }

    private static int ReadInt(JsonObject? obj, string prop, int fallback)
    {
        if (obj is null || !obj.TryGetPropertyValue(prop, out var node) || node is null)
        {
            return fallback;
        }

        if (node.TryGetValue<int>(out var i))
        {
            return i;
        }

        if (node.TryGetValue<long>(out var l))
        {
            return (int)l;
        }

        return int.TryParse(node.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : fallback;
    }

    private static double ReadDouble(JsonObject? obj, string prop, double fallback)
    {
        if (obj is null || !obj.TryGetPropertyValue(prop, out var node) || node is null)
        {
            return fallback;
        }

        if (node.TryGetValue<double>(out var d))
        {
            return d;
        }

        if (node.TryGetValue<long>(out var l))
        {
            return l;
        }

        return double.TryParse(node.ToString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : fallback;
    }

    private static bool ReadBool(JsonObject? obj, string prop, bool fallback)
    {
        if (obj is null || !obj.TryGetPropertyValue(prop, out var node) || node is null)
        {
            return fallback;
        }

        if (node.TryGetValue<bool>(out var b))
        {
            return b;
        }

        return bool.TryParse(node.ToString(), out var parsed) ? parsed : fallback;
    }

    private static string ParsePositionSide(JsonObject obj)
    {
        var sideRaw = ReadString(obj, "side", string.Empty);
        if (!string.IsNullOrWhiteSpace(sideRaw))
        {
            return sideRaw.ToUpperInvariant();
        }

        var type = ReadInt(obj, "type", -1);
        return type == 0 ? "BUY" : type == 1 ? "SELL" : "UNKNOWN";
    }

    private static DateTimeOffset? ParseTimestamp(JsonNode? node)
    {
        if (node is null)
        {
            return null;
        }

        if (node.TryGetValue<double>(out var d))
        {
            return ParseUnixLike(d);
        }

        if (node.TryGetValue<long>(out var l))
        {
            return ParseUnixLike(l);
        }

        if (!node.TryGetValue<string>(out var s) || string.IsNullOrWhiteSpace(s))
        {
            s = node.ToString();
        }

        if (double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var numeric))
        {
            return ParseUnixLike(numeric);
        }

        if (DateTimeOffset.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed))
        {
            return parsed.ToUniversalTime();
        }

        return null;
    }

    private static DateTimeOffset ParseUnixLike(double value)
    {
        var seconds = value > 10_000_000_000 ? value / 1000.0 : value;
        return DateTimeOffset.FromUnixTimeMilliseconds((long)Math.Round(seconds * 1000.0));
    }

    private static int Clamp(int value, int min, int max)
    {
        return Math.Min(Math.Max(value, min), max);
    }
}
