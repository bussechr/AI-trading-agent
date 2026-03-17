using Polly;
using Polly.Wrap;

namespace TradingAgent.Dashboard.Infrastructure.Utilities;

public static class ResiliencePolicies
{
    public static AsyncPolicyWrap<HttpResponseMessage> BuildHttpPolicy()
    {
        var retry = Policy<HttpResponseMessage>
            .Handle<HttpRequestException>()
            .Or<TaskCanceledException>()
            .OrResult(resp => (int)resp.StatusCode >= 500)
            .WaitAndRetryAsync(
                3,
                attempt => TimeSpan.FromMilliseconds(150 * Math.Pow(2, attempt - 1))
            );

        var breaker = Policy<HttpResponseMessage>
            .Handle<HttpRequestException>()
            .Or<TaskCanceledException>()
            .OrResult(resp => (int)resp.StatusCode >= 500)
            .CircuitBreakerAsync(5, TimeSpan.FromSeconds(10));

        return Policy.WrapAsync(retry, breaker);
    }
}
