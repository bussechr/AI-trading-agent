using System.Collections.ObjectModel;

namespace TradingAgent.Dashboard.App.ViewModels;

public static class CollectionSyncExtensions
{
    public static void ReplaceWith<T>(this ObservableCollection<T> target, IEnumerable<T> source)
    {
        target.Clear();
        foreach (var row in source)
        {
            target.Add(row);
        }
    }
}
