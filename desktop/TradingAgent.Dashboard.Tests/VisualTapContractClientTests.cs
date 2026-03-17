using System.Net;
using System.Net.Http;
using System.Text;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class VisualTapContractClientTests
{
    [Fact]
    public async Task BridgeClientUsesNonConsumingVisualTapEndpoint()
    {
        string? requestedPath = null;

        using var http = new HttpClient(new StubHandler(req =>
        {
            requestedPath = req.RequestUri?.AbsolutePath;
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("[]", Encoding.UTF8, "application/json"),
            };
        }));

        var sut = new BridgeTelemetryClient(http, new DashboardOptions());
        await sut.GetVisualTapAsync("EURUSD", 20, CancellationToken.None);

        Assert.Equal("/v2/visuals/tap", requestedPath);
    }

    private sealed class StubHandler : HttpMessageHandler
    {
        private readonly Func<HttpRequestMessage, HttpResponseMessage> _responder;

        public StubHandler(Func<HttpRequestMessage, HttpResponseMessage> responder)
        {
            _responder = responder;
        }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            return Task.FromResult(_responder(request));
        }
    }
}
