using TradingAgent.Dashboard.Core.Models;

namespace TradingAgent.Dashboard.Core.Contracts;

public interface IRuntimeDbReader
{
    Task<DbReaderStatus> GetStatusAsync(CancellationToken cancellationToken);
    Task<IReadOnlyList<AccountSnapshotRow>> ReadAccountSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken);
    Task<IReadOnlyList<PositionSnapshotRow>> ReadPositionSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken);
    Task<IReadOnlyList<DecisionSnapshotRow>> ReadDecisionSnapshotsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken);
    Task<IReadOnlyList<GovernanceEventRow>> ReadGovernanceEventsAsync(DateTimeOffset sinceUtc, CancellationToken cancellationToken);
}
