using System.Buffers.Binary;
using System.Diagnostics;
using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>Shared spin-wait used across the async tests (and the server's
/// WaitFor* helpers) instead of a per-file copy.</summary>
internal static class Poll
{
    public static async Task UntilAsync(Func<bool> predicate, TimeSpan timeout, string what)
    {
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < timeout)
        {
            if (predicate())
                return;
            await Task.Delay(10);
        }
        throw new TimeoutException($"timed out waiting for {what}");
    }
}

/// <summary>A scripted capture: raises <see cref="DataAvailable"/> on demand via
/// <see cref="Emit"/>, so a test feeds synthetic PCM with no real audio device.</summary>
internal sealed class FakeAudioCapture(AudioFormat format) : IAudioCapture
{
    public AudioFormat Format { get; } = format;
    public bool Started { get; private set; }
    public bool Stopped { get; private set; }
    public bool Disposed { get; private set; }

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    public void Start() => Started = true;
    public void Stop() => Stopped = true;
    public void Dispose() => Disposed = true;

    public void Emit(byte[] pcm) => DataAvailable?.Invoke(this, new AudioCapturedEventArgs(pcm));
}

/// <summary>
/// Hands out <see cref="FakeTapConnection"/>s gated by one switch: <see cref="Up"/>
/// false makes every connect and every active send throw (a blip / outage), with
/// no real socket — so <see cref="TapStream"/>'s reconnect/buffer/drain logic is
/// exercised with frame-perfect determinism, which a send-only client's blip
/// timing against a real socket can't provide.
/// </summary>
internal sealed class FakeTapTransport
{
    private readonly object _lock = new();
    private readonly List<FakeTapConnection> _conns = [];

    public volatile bool Up = true;

    public IReadOnlyList<FakeTapConnection> Connections
    {
        get { lock (_lock) return _conns.ToList(); }
    }

    public int SentCount(int connIndex)
    {
        lock (_lock)
            return connIndex < _conns.Count ? _conns[connIndex].SentCount : 0;
    }

    public ITapConnection Create(TapConnectionOptions options)
    {
        var conn = new FakeTapConnection(this, options);
        lock (_lock)
            _conns.Add(conn);
        return conn;
    }
}

internal sealed class FakeTapConnection(FakeTapTransport transport, TapConnectionOptions options) : ITapConnection
{
    private readonly object _lock = new();

    public string? UtteranceId { get; } = options.UtteranceId;
    public List<int> Sent { get; } = [];
    public bool Closed { get; private set; }
    public bool Disposed { get; private set; }

    public int SentCount
    {
        get { lock (_lock) return Sent.Count; }
    }

    public Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!transport.Up)
            throw new WebSocketException(WebSocketError.Faulted, "transport down");
        return Task.CompletedTask;
    }

    public Task SendFrameAsync(ReadOnlyMemory<byte> frame, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!transport.Up)
            throw new WebSocketException(WebSocketError.ConnectionClosedPrematurely, "blip");
        int index = BinaryPrimitives.ReadInt32LittleEndian(frame.Span);
        lock (_lock)
            Sent.Add(index);
        return Task.CompletedTask;
    }

    public Task CloseAsync(CancellationToken cancellationToken = default)
    {
        Closed = true;
        return Task.CompletedTask;
    }

    public ValueTask DisposeAsync()
    {
        Disposed = true;
        return ValueTask.CompletedTask;
    }
}

/// <summary>
/// In-process Kestrel /tap server that records every connection it accepts — the
/// negotiated subprotocol, the binary frame payloads' leading int32 index, and
/// whether the client closed cleanly. Used by the real-socket happy-path tests.
/// </summary>
internal sealed class RecordingTapServer : IAsyncDisposable
{
    public sealed class Conn
    {
        public string? UtteranceId { get; init; }
        public string? SubProtocol { get; set; }
        public List<int> Indices { get; } = [];
        public bool ClosedNormally { get; set; }
    }

    private readonly WebApplication _app;
    private readonly List<Conn> _conns = [];
    private readonly object _lock = new();

    public int Port { get; }

    public IReadOnlyList<Conn> Connections
    {
        get { lock (_lock) return _conns.ToList(); }
    }

    private RecordingTapServer(WebApplication app, int port)
    {
        _app = app;
        Port = port;
    }

    public static async Task<RecordingTapServer> StartAsync()
    {
        WebApplicationBuilder builder = WebApplication.CreateBuilder();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        WebApplication app = builder.Build();
        app.UseWebSockets();

        RecordingTapServer? self = null;
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

            var conn = new Conn { UtteranceId = context.Request.Query["utterance_id"], SubProtocol = ws.SubProtocol };
            lock (self!._lock)
                self._conns.Add(conn);

            var buffer = new byte[4096];
            try
            {
                while (ws.State == WebSocketState.Open)
                {
                    WebSocketReceiveResult result = await ws.ReceiveAsync(buffer, CancellationToken.None);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "ok", CancellationToken.None);
                        conn.ClosedNormally = true;
                        break;
                    }
                    if (result.MessageType == WebSocketMessageType.Binary && result.Count >= 4)
                    {
                        int index = BinaryPrimitives.ReadInt32LittleEndian(buffer);
                        lock (self._lock)
                            conn.Indices.Add(index);
                    }
                }
            }
            catch (WebSocketException)
            {
                // Client reset under us; the frames collected so far stand.
            }
        });

        await app.StartAsync();
        string address = app.Services.GetRequiredService<IServer>()
            .Features.Get<IServerAddressesFeature>()!.Addresses.First();
        self = new RecordingTapServer(app, new Uri(address).Port);
        return self;
    }

    public int TotalFrames
    {
        get { lock (_lock) return _conns.Sum(c => c.Indices.Count); }
    }

    public Task WaitForFramesAsync(int total, TimeSpan timeout) =>
        Poll.UntilAsync(() => TotalFrames >= total, timeout, $"the server to receive {total} frames");

    public Task WaitForConnectionsAsync(int n, TimeSpan timeout) =>
        Poll.UntilAsync(() => Connections.Count >= n, timeout, $"the server to accept {n} connections");

    public async ValueTask DisposeAsync()
    {
        await _app.StopAsync();
        await _app.DisposeAsync();
    }
}
