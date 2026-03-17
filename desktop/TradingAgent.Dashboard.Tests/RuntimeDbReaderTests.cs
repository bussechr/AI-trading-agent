using Microsoft.Data.Sqlite;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class RuntimeDbReaderTests
{
    [Fact]
    public async Task MissingDbPathReturnsUnavailableStatus()
    {
        var options = new DashboardOptions
        {
            RuntimeDbPath = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"), "missing.db"),
        };

        var sut = new RuntimeDbReader(options);
        var status = await sut.GetStatusAsync(CancellationToken.None);

        Assert.False(status.IsAvailable);
        Assert.Equal("api-only", status.Mode);
    }

    [Fact]
    public async Task ReadsAccountSnapshotsFromSqlite()
    {
        var dbPath = Path.Combine(Path.GetTempPath(), $"runtime-{Guid.NewGuid():N}.db");
        await CreateRuntimeDbAsync(dbPath);

        var sut = new RuntimeDbReader(new DashboardOptions { RuntimeDbPath = dbPath });
        var status = await sut.GetStatusAsync(CancellationToken.None);
        var rows = await sut.ReadAccountSnapshotsAsync(DateTimeOffset.UtcNow.AddHours(-2), CancellationToken.None);

        Assert.True(status.IsAvailable);
        Assert.Single(rows);
        Assert.Equal(12345.67, rows[0].Equity, 2);

        File.Delete(dbPath);
    }

    private static async Task CreateRuntimeDbAsync(string path)
    {
        await using var conn = new SqliteConnection($"Data Source={path}");
        await conn.OpenAsync();

        var cmd = conn.CreateCommand();
        cmd.CommandText = """
CREATE TABLE account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  equity REAL,
  margin REAL,
  freemargin REAL,
  leverage REAL,
  source TEXT,
  raw_json TEXT
);
INSERT INTO account_snapshots(ts, equity, margin, freemargin, leverage, source, raw_json)
VALUES(strftime('%s','now') - 300, 12345.67, 100.0, 12245.67, 200.0, 'heartbeat', '{}');
""";
        await cmd.ExecuteNonQueryAsync();
    }
}
