using System.Globalization;
using System.Text.Json.Nodes;
using Dapper;
using Microsoft.Data.Sqlite;
using TradingAgent.Dashboard.Core.Contracts;
using TradingAgent.Dashboard.Core.Models;
using TradingAgent.Dashboard.Infrastructure.Configuration;

namespace TradingAgent.Dashboard.Infrastructure.Services;

public sealed class RuntimeDbReader : IRuntimeDbReader
{
    private readonly string _dbPath;

    public RuntimeDbReader(DashboardOptions options)
    {
        _dbPath = string.IsNullOrWhiteSpace(options.RuntimeDbPath)
            ? "data/state/runtime_v2.db"
            : options.RuntimeDbPath;
    }

    public async Task<DbReaderStatus> GetStatusAsync(CancellationToken cancellationToken)
    {
        if (!File.Exists(_dbPath))
        {
            return new DbReaderStatus(false, "api-only", $"SQLite DB missing at {_dbPath}", DateTimeOffset.UtcNow);
        }

        try
        {
            await using var conn = OpenReadOnly();
            await conn.OpenAsync(cancellationToken);
            var scalar = await conn.ExecuteScalarAsync<int>("SELECT 1");
            if (scalar == 1)
            {
                return new DbReaderStatus(true, "api+sqlite", "SQLite mirror connected", DateTimeOffset.UtcNow);
            }

            return new DbReaderStatus(false, "api-only", "SQLite mirror health check failed", DateTimeOffset.UtcNow);
        }
        catch (Exception exc)
        {
            return new DbReaderStatus(false, "api-only", $"SQLite unavailable: {exc.Message}", DateTimeOffset.UtcNow);
        }
    }

    public async Task<IReadOnlyList<AccountSnapshotRow>> ReadAccountSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
    {
        const string sql = @"
SELECT ts, equity, margin, freemargin, leverage, source, raw_json
FROM account_snapshots
WHERE ts > @SinceTs
ORDER BY ts ASC
";

        try
        {
            await using var conn = OpenReadOnly();
            await conn.OpenAsync(cancellationToken);
            var rows = await conn.QueryAsync(sql, new { SinceTs = ToEpochSeconds(sinceUtc) });
            var outRows = new List<AccountSnapshotRow>();

            foreach (var row in rows)
            {
                var ts = ParseEpoch(row.ts);
                outRows.Add(new AccountSnapshotRow(
                    TimeUtc: ts,
                    Equity: ParseDouble(row.equity),
                    Margin: ParseDouble(row.margin),
                    FreeMargin: ParseDouble(row.freemargin),
                    Leverage: ParseDouble(row.leverage),
                    Source: Convert.ToString(row.source, CultureInfo.InvariantCulture) ?? "unknown",
                    Payload: ParseObject(Convert.ToString(row.raw_json, CultureInfo.InvariantCulture))
                ));
            }

            return outRows;
        }
        catch
        {
            return Array.Empty<AccountSnapshotRow>();
        }
    }

    public async Task<IReadOnlyList<PositionSnapshotRow>> ReadPositionSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
    {
        const string sql = @"
SELECT ts, source, positions_json
FROM position_snapshots
WHERE ts > @SinceTs
ORDER BY ts ASC
";

        try
        {
            await using var conn = OpenReadOnly();
            await conn.OpenAsync(cancellationToken);
            var rows = await conn.QueryAsync(sql, new { SinceTs = ToEpochSeconds(sinceUtc) });
            var outRows = new List<PositionSnapshotRow>();

            foreach (var row in rows)
            {
                var positionsJson = ParseArray(Convert.ToString(row.positions_json, CultureInfo.InvariantCulture));
                var positions = positionsJson
                    .OfType<JsonObject>()
                    .Select(ParsePosition)
                    .ToArray();

                outRows.Add(new PositionSnapshotRow(
                    TimeUtc: ParseEpoch(row.ts),
                    Source: Convert.ToString(row.source, CultureInfo.InvariantCulture) ?? "unknown",
                    Positions: positions
                ));
            }

            return outRows;
        }
        catch
        {
            return Array.Empty<PositionSnapshotRow>();
        }
    }

    public async Task<IReadOnlyList<DecisionSnapshotRow>> ReadDecisionSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
    {
        const string sql = @"
SELECT ts, vol, diagnostics_json, rejection_json, attribution_json
FROM decision_snapshots
WHERE ts > @SinceTs
ORDER BY ts ASC
";

        try
        {
            await using var conn = OpenReadOnly();
            await conn.OpenAsync(cancellationToken);
            var rows = await conn.QueryAsync(sql, new { SinceTs = ToEpochSeconds(sinceUtc) });
            var outRows = new List<DecisionSnapshotRow>();

            foreach (var row in rows)
            {
                outRows.Add(new DecisionSnapshotRow(
                    TimeUtc: ParseEpoch(row.ts),
                    Vol: ParseDouble(row.vol),
                    Diagnostics: ParseObject(Convert.ToString(row.diagnostics_json, CultureInfo.InvariantCulture)),
                    Rejection: ParseObject(Convert.ToString(row.rejection_json, CultureInfo.InvariantCulture)),
                    Attribution: ParseObject(Convert.ToString(row.attribution_json, CultureInfo.InvariantCulture))
                ));
            }

            return outRows;
        }
        catch
        {
            return Array.Empty<DecisionSnapshotRow>();
        }
    }

    public async Task<IReadOnlyList<GovernanceEventRow>> ReadGovernanceEventsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken)
    {
        const string sql = @"
SELECT ts, event_type, reason, payload_json
FROM governance_events
WHERE ts > @SinceTs
ORDER BY ts ASC
";

        try
        {
            await using var conn = OpenReadOnly();
            await conn.OpenAsync(cancellationToken);
            var rows = await conn.QueryAsync(sql, new { SinceTs = ToEpochSeconds(sinceUtc) });
            var outRows = new List<GovernanceEventRow>();

            foreach (var row in rows)
            {
                outRows.Add(new GovernanceEventRow(
                    TimeUtc: ParseEpoch(row.ts),
                    EventType: Convert.ToString(row.event_type, CultureInfo.InvariantCulture) ?? "state_update",
                    Reason: Convert.ToString(row.reason, CultureInfo.InvariantCulture) ?? string.Empty,
                    Payload: ParseObject(Convert.ToString(row.payload_json, CultureInfo.InvariantCulture))
                ));
            }

            return outRows;
        }
        catch
        {
            return Array.Empty<GovernanceEventRow>();
        }
    }

    private SqliteConnection OpenReadOnly()
    {
        var cs = new SqliteConnectionStringBuilder
        {
            DataSource = _dbPath,
            Mode = SqliteOpenMode.ReadOnly,
            Cache = SqliteCacheMode.Shared,
        };
        return new SqliteConnection(cs.ConnectionString);
    }

    private static DateTimeOffset ParseEpoch(object raw)
    {
        var seconds = ParseDouble(raw);
        return DateTimeOffset.FromUnixTimeMilliseconds((long)Math.Round(seconds * 1000.0));
    }

    private static PositionSnapshot ParsePosition(JsonObject obj)
    {
        return new PositionSnapshot(
            Symbol: ReadString(obj, "symbol", string.Empty),
            Side: ReadString(obj, "side", InferSide(obj)),
            Lots: ReadDouble(obj, "lots", 0.0),
            Profit: ReadDouble(obj, "profit", 0.0),
            OpenPrice: ReadDouble(obj, "open_price", 0.0),
            OpenTimeUtc: ParseOptionalTimestamp(obj["open_time"])
        );
    }

    private static string InferSide(JsonObject obj)
    {
        var t = ReadInt(obj, "type", -1);
        return t == 0 ? "BUY" : t == 1 ? "SELL" : "UNKNOWN";
    }

    private static DateTimeOffset? ParseOptionalTimestamp(JsonNode? node)
    {
        if (node is null)
        {
            return null;
        }

        if (node.TryGetValue<double>(out var d))
        {
            return DateTimeOffset.FromUnixTimeMilliseconds((long)Math.Round(d * 1000.0));
        }

        if (node.TryGetValue<long>(out var l))
        {
            return DateTimeOffset.FromUnixTimeMilliseconds(l > 10_000_000_000 ? l : l * 1000);
        }

        if (DateTimeOffset.TryParse(node.ToString(), CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed))
        {
            return parsed.ToUniversalTime();
        }

        return null;
    }

    private static JsonObject ParseObject(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return new JsonObject();
        }

        try
        {
            return JsonNode.Parse(json) as JsonObject ?? new JsonObject();
        }
        catch
        {
            return new JsonObject();
        }
    }

    private static JsonArray ParseArray(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return new JsonArray();
        }

        try
        {
            return JsonNode.Parse(json) as JsonArray ?? new JsonArray();
        }
        catch
        {
            return new JsonArray();
        }
    }

    private static double ParseDouble(object raw)
    {
        if (raw is null)
        {
            return 0.0;
        }

        if (raw is double d)
        {
            return d;
        }

        if (raw is float f)
        {
            return f;
        }

        if (raw is int i)
        {
            return i;
        }

        if (raw is long l)
        {
            return l;
        }

        var s = Convert.ToString(raw, CultureInfo.InvariantCulture);
        return double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : 0.0;
    }

    private static long ToEpochSeconds(DateTimeOffset ts)
    {
        return ts.ToUnixTimeMilliseconds() / 1000;
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

        var val = node.ToString();
        return string.IsNullOrWhiteSpace(val) ? fallback : val;
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
}
