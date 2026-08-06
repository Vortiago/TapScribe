using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests the "Test connection" probe against an in-process fake Recorder serving
/// /health and /tap, covering the three outcomes the settings dialog reports:
/// reachable + token accepted, unreachable, and reachable + token rejected.
/// </summary>
public class ConnectionTesterTests
{
    [Fact]
    public async Task Test_ReachableAndTokenAccepted_IsOk()
    {
        await using FakeRecorder server = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Token = "tok-abc" };

        ConnectionTestResult result = await ConnectionTester.TestAsync(options, http, cts.Token);

        Assert.True(result.Reachable);
        Assert.True(result.TokenChecked);
        Assert.True(result.TokenAccepted);
        Assert.True(result.Ok);
    }

    [Fact]
    public async Task Test_Unreachable_ReportsNotReachable_AndSkipsTokenProbe()
    {
        await using FakeRecorder server = await FakeRecorder.StartAsync(healthStatus: 500);
        using var http = new HttpClient();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Token = "tok-abc" };

        ConnectionTestResult result = await ConnectionTester.TestAsync(options, http, cts.Token);

        Assert.False(result.Reachable);
        Assert.False(result.TokenChecked); // no point probing the token if unreachable
        Assert.False(result.Ok);
        Assert.NotNull(result.ReachError);
    }

    [Fact]
    public async Task Test_ReachableButTokenRejected_IsNotOk()
    {
        await using FakeRecorder server = await FakeRecorder.StartAsync(rejectTap: true);
        using var http = new HttpClient();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Token = "wrong-token" };

        ConnectionTestResult result = await ConnectionTester.TestAsync(options, http, cts.Token);

        Assert.True(result.Reachable);
        Assert.True(result.TokenChecked);
        Assert.False(result.TokenAccepted);
        Assert.False(result.Ok);
        Assert.NotNull(result.TokenError);
    }

    [Fact]
    public async Task Test_ServerAcceptsThenCloses_ReportsTokenRejected()
    {
        // Guards against a false "accepted": the Recorder denies a bad token
        // before accepting today, but a server that accepts then closes 4401 must
        // still read as rejected (this is the case the bare ConnectAsync-succeeds
        // check would get wrong).
        await using FakeRecorder server = await FakeRecorder.StartAsync(acceptThenClose: true);
        using var http = new HttpClient();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Token = "tok" };

        ConnectionTestResult result = await ConnectionTester.TestAsync(options, http, cts.Token);

        Assert.True(result.Reachable);
        Assert.True(result.TokenChecked);
        Assert.False(result.TokenAccepted);
        Assert.False(result.Ok);
    }

    /// <summary>In-process Recorder stub: GET /health (status configurable) and a /tap WS that accepts or rejects.</summary>
    private sealed class FakeRecorder : IAsyncDisposable
    {
        private readonly WebApplication _app;

        public int Port { get; }

        private FakeRecorder(WebApplication app, int port)
        {
            _app = app;
            Port = port;
        }

        public static async Task<FakeRecorder> StartAsync(int healthStatus = 200, bool rejectTap = false, bool acceptThenClose = false)
        {
            WebApplicationBuilder builder = WebApplication.CreateBuilder();
            builder.WebHost.UseUrls("http://127.0.0.1:0");
            WebApplication app = builder.Build();
            app.UseWebSockets();

            app.MapGet("/health", (HttpContext context) =>
            {
                context.Response.StatusCode = healthStatus;
                return context.Response.WriteAsync("{\"status\":\"ok\"}");
            });

            app.Map("/tap", async (HttpContext context) =>
            {
                if (!context.WebSockets.IsWebSocketRequest)
                {
                    context.Response.StatusCode = StatusCodes.Status400BadRequest;
                    return;
                }
                if (rejectTap)
                {
                    context.Response.StatusCode = StatusCodes.Status401Unauthorized;
                    return;
                }

                string? chosen = context.WebSockets.WebSocketRequestedProtocols
                    .FirstOrDefault(p => p.StartsWith(TapWire.SubprotocolPrefix, StringComparison.Ordinal));
                using WebSocket ws = chosen is null
                    ? await context.WebSockets.AcceptWebSocketAsync()
                    : await context.WebSockets.AcceptWebSocketAsync(chosen);

                if (acceptThenClose)
                {
                    // Accept the upgrade, then immediately reject with 4401 — the
                    // "harder" rejection shape the probe must also detect.
                    await ws.CloseAsync((WebSocketCloseStatus)4401, "rejected after accept", CancellationToken.None);
                    return;
                }

                var buffer = new byte[1024];
                try
                {
                    while (ws.State == WebSocketState.Open)
                    {
                        WebSocketReceiveResult result = await ws.ReceiveAsync(buffer, CancellationToken.None);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "ok", CancellationToken.None);
                            break;
                        }
                    }
                }
                catch (WebSocketException)
                {
                    // Probe aborted the socket after a successful handshake; fine.
                }
            });

            await app.StartAsync();
            string address = app.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!.Addresses.First();
            return new FakeRecorder(app, new Uri(address).Port);
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }
}
