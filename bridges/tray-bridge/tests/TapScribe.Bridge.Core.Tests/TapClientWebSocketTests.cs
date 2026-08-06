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
/// Exercises the real wire path — subprotocol negotiation + a binary frame
/// round-trip — against an in-process Kestrel `/tap` server. This is the test
/// behind the "works against both --no-auth and a tokened Recorder via the
/// subprotocol" acceptance criterion, without needing the Python Recorder.
/// Kestrel (not HttpListener) so it runs identically on the ubuntu CI job.
/// </summary>
public class TapClientWebSocketTests
{
    [Fact]
    public async Task TokenedConnect_NegotiatesSubprotocol_AndRoundTripsAFrame()
    {
        const string token = "AaLDg9xmHNNoi-Ug";
        await using TapEchoServer server = await TapEchoServer.StartAsync();
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Identity = "alice", Token = token };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));

        await using var client = new TapClient(options);
        await client.ConnectAsync(cts.Token);

        Assert.Equal($"tapscribe.v1.tap.{token}", client.NegotiatedSubProtocol);

        byte[] frame = Pattern(TapWire.FrameBytes);
        await client.SendFrameAsync(frame, cts.Token);

        TapEchoServer.ReceivedFrame received = await server.FirstFrame.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(WebSocketMessageType.Binary, received.Type);
        Assert.Equal(frame, received.Data);
        Assert.Equal($"tapscribe.v1.tap.{token}", server.ServerSubProtocol);
    }

    [Fact]
    public async Task NoAuthConnect_OffersNoSubprotocol_AndRoundTripsAFrame()
    {
        await using TapEchoServer server = await TapEchoServer.StartAsync();
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Identity = "bob", Token = "" };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));

        await using var client = new TapClient(options);
        await client.ConnectAsync(cts.Token);

        Assert.Null(client.NegotiatedSubProtocol);

        byte[] frame = Pattern(TapWire.FrameBytes);
        await client.SendFrameAsync(frame, cts.Token);

        TapEchoServer.ReceivedFrame received = await server.FirstFrame.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(WebSocketMessageType.Binary, received.Type);
        Assert.Equal(frame, received.Data);
        Assert.Null(server.ServerSubProtocol);
    }

    private static byte[] Pattern(int length)
    {
        var bytes = new byte[length];
        for (int i = 0; i < length; i++)
            bytes[i] = (byte)(i % 256);
        return bytes;
    }

    /// <summary>Minimal in-process Recorder-like `/tap` endpoint for one connection.</summary>
    private sealed class TapEchoServer : IAsyncDisposable
    {
        private readonly WebApplication _app;
        private readonly TaskCompletionSource<ReceivedFrame> _firstFrame =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public int Port { get; }
        public string? ServerSubProtocol { get; private set; }
        public Task<ReceivedFrame> FirstFrame => _firstFrame.Task;

        public sealed record ReceivedFrame(byte[] Data, WebSocketMessageType Type);

        private TapEchoServer(WebApplication app, int port)
        {
            _app = app;
            Port = port;
        }

        public static async Task<TapEchoServer> StartAsync()
        {
            WebApplicationBuilder builder = WebApplication.CreateBuilder();
            builder.WebHost.UseUrls("http://127.0.0.1:0"); // ephemeral port
            WebApplication app = builder.Build();
            app.UseWebSockets();

            TapEchoServer? self = null;
            app.Map("/tap", async (HttpContext context) =>
            {
                if (!context.WebSockets.IsWebSocketRequest)
                {
                    context.Response.StatusCode = StatusCodes.Status400BadRequest;
                    return;
                }

                string? chosen = context.WebSockets.WebSocketRequestedProtocols
                    .FirstOrDefault(p => p.StartsWith(TapWire.SubprotocolPrefix, StringComparison.Ordinal));
                using WebSocket ws = chosen is null
                    ? await context.WebSockets.AcceptWebSocketAsync()
                    : await context.WebSockets.AcceptWebSocketAsync(chosen);

                self!.ServerSubProtocol = ws.SubProtocol;

                var buffer = new byte[2048];
                WebSocketReceiveResult result = await ws.ReceiveAsync(buffer, CancellationToken.None);
                self._firstFrame.TrySetResult(new ReceivedFrame(buffer[..result.Count], result.MessageType));

                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "ok", CancellationToken.None);
            });

            await app.StartAsync();
            string address = app.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!.Addresses.First();
            self = new TapEchoServer(app, new Uri(address).Port);
            return self;
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }
}
