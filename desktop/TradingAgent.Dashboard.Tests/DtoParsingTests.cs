using System.Net;
using System.Net.Http;
using System.Text;
using TradingAgent.Dashboard.Infrastructure.Configuration;
using TradingAgent.Dashboard.Infrastructure.Services;
using Xunit;

namespace TradingAgent.Dashboard.Tests;

public sealed class DtoParsingTests
{
    [Fact]
    public async Task BridgeClient_ParsesStateWithMissingFieldsWithoutThrowing()
    {
        using var http = new HttpClient(new StubHandler(req =>
        {
            if (req.RequestUri?.AbsolutePath == "/v2/state")
            {
                return JsonResponse("""
                {
                  "system_status": "connected",
                  "equity": "10000.5",
                  "positions": [{"symbol": "EURUSD"}],
                  "agent_decisions": [{"symbol": "EURUSD", "side": "BUY", "score": "0.51"}],
                  "unexpected": {"nested": 1}
                }
                """);
            }

            if (req.RequestUri?.AbsolutePath == "/v2/health")
            {
                return JsonResponse("""{"status":"healthy"}""");
            }

            return JsonResponse("{}", HttpStatusCode.NotFound);
        }));

        var sut = new BridgeTelemetryClient(http, new DashboardOptions());
        var state = await sut.GetStateAsync(CancellationToken.None);

        Assert.NotNull(state);
        Assert.Equal("connected", state!.SystemStatus);
        Assert.Equal(10000.5, state.Equity);
        Assert.Single(state.Positions);
        Assert.Equal("EURUSD", state.Positions[0].Symbol);
        Assert.Equal("BUY", state.AgentDecisions[0].Side);
    }

    [Fact]
    public async Task BridgeClient_ParsesVisualTapPayloadWithPartialRows()
    {
        using var http = new HttpClient(new StubHandler(req =>
        {
            if (req.RequestUri?.AbsolutePath == "/v2/visuals/tap")
            {
                return JsonResponse("""
                [
                  {"symbol":"EURUSD","type":"arrow","side":"BUY","price":1.10001,"time":1710000000},
                  {"symbol":"EURUSD","type":"label","text":"Signal ready"}
                ]
                """);
            }

            return JsonResponse("{}", HttpStatusCode.NotFound);
        }));

        var sut = new BridgeTelemetryClient(http, new DashboardOptions());
        var rows = await sut.GetVisualTapAsync("EURUSD", 20, CancellationToken.None);

        Assert.Equal(2, rows.Count);
        Assert.Equal("arrow", rows[0].Type);
        Assert.Equal("BUY", rows[0].Side);
        Assert.Equal("label", rows[1].Type);
        Assert.Equal("Signal ready", rows[1].Text);
    }

    private static HttpResponseMessage JsonResponse(string json, HttpStatusCode statusCode = HttpStatusCode.OK)
    {
        return new HttpResponseMessage(statusCode)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
        };
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
