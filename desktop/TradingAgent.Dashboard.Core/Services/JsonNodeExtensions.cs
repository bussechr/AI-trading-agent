using System.Globalization;
using System.Text.Json.Nodes;

namespace TradingAgent.Dashboard.Core.Services;

public static class JsonNodeExtensions
{
    public static JsonNode? GetPath(this JsonObject? obj, string path)
    {
        if (obj is null || string.IsNullOrWhiteSpace(path))
        {
            return null;
        }

        JsonNode? cursor = obj;
        foreach (var segment in path.Split('.', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            if (cursor is not JsonObject cursorObj || !cursorObj.TryGetPropertyValue(segment, out cursor))
            {
                return null;
            }
        }

        return cursor;
    }

    public static double GetDoublePath(this JsonObject? obj, string path, double fallback = 0.0)
    {
        var node = obj.GetPath(path);
        if (node is null)
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

        if (node.TryGetValue<int>(out var i))
        {
            return i;
        }

        var s = node.ToString();
        return double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : fallback;
    }

    public static int GetIntPath(this JsonObject? obj, string path, int fallback = 0)
    {
        var node = obj.GetPath(path);
        if (node is null)
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

        var s = node.ToString();
        return int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : fallback;
    }

    public static string GetStringPath(this JsonObject? obj, string path, string fallback = "")
    {
        var node = obj.GetPath(path);
        if (node is null)
        {
            return fallback;
        }

        if (node.TryGetValue<string>(out var s) && !string.IsNullOrWhiteSpace(s))
        {
            return s;
        }

        var outValue = node.ToString();
        return string.IsNullOrWhiteSpace(outValue) ? fallback : outValue;
    }

    public static JsonObject GetObjectPath(this JsonObject? obj, string path)
    {
        var node = obj.GetPath(path);
        return node as JsonObject ?? new JsonObject();
    }
}
