using System.Buffers.Binary;
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
/// Integration tests for the mic -> /tap pipeline (capture -> resampler ->
/// chunker -> WebSocket), driven by a fake <see cref="IAudioCapture"/> feeding
/// synthetic PCM against an in-process Kestrel /tap server. No real microphone or
/// Windows audio stack — so this whole pipeline is now covered cross-platform.
/// </summary>
public class TapSessionTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    [Fact]
    public async Task Pipeline_ResamplesAndStreams_640ByteFramesToTheServer()
    {
        // 1.0 s of 48 kHz stereo float -> 16 kHz mono int16 = 16000 samples =
        // 32000 bytes = exactly 50 frames of 640 bytes.
        await using RecordingTapServer server = await RecordingTapServer.StartAsync();
        var capture = new FakeAudioCapture(new AudioFormat(48_000, 2, SampleKind.Float32));
        var connected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Identity = "x" };

        TapSession session = TapSession.Begin(
            capture, options,
            onConnected: () => connected.TrySetResult(),
            onFailed: ex => connected.TrySetException(ex));

        await connected.Task.WaitAsync(Wait);
        capture.Emit(OneSecondOf48kStereoFloat());
        await session.DisposeAsync();              // drains the buffered frames, then closes
        await server.Completed.WaitAsync(Wait);    // server saw the close; all frames collected

        Assert.Equal(50, server.ReceivedFrames.Count);
        Assert.All(server.ReceivedFrames, f => Assert.Equal(TapWire.FrameBytes, f.Length));
    }

    [Fact]
    public async Task Pipeline_OnConnect_StartsCapture_AndNegotiatesSubprotocol()
    {
        await using RecordingTapServer server = await RecordingTapServer.StartAsync();
        var capture = new FakeAudioCapture(new AudioFormat(16_000, 1, SampleKind.Int16));
        var connected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Identity = "x", Token = "tok-abc" };

        TapSession session = TapSession.Begin(
            capture, options,
            onConnected: () => connected.TrySetResult(),
            onFailed: ex => connected.TrySetException(ex));

        await connected.Task.WaitAsync(Wait);
        Assert.True(capture.Started); // the pump starts capture only after the WS connects

        await session.DisposeAsync();
        await server.Completed.WaitAsync(Wait);
        Assert.Equal("tapscribe.v1.tap.tok-abc", server.NegotiatedSubProtocol);
    }

    [Fact]
    public async Task Pipeline_WhenUpgradeRejected_InvokesOnFailed_AndDoesNotStartCapture()
    {
        await using RecordingTapServer server = await RecordingTapServer.StartAsync(reject: true);
        var capture = new FakeAudioCapture(new AudioFormat(16_000, 1, SampleKind.Int16));
        var failed = new TaskCompletionSource<Exception>(TaskCreationOptions.RunContinuationsAsynchronously);
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Identity = "x", Token = "tok" };

        TapSession session = TapSession.Begin(
            capture, options,
            onConnected: () => failed.TrySetException(new InvalidOperationException("should not connect")),
            onFailed: ex => failed.TrySetResult(ex));

        Exception error = await failed.Task.WaitAsync(Wait);

        Assert.IsType<WebSocketException>(error);
        Assert.False(capture.Started); // connect failed before capture could start
        await session.DisposeAsync();
    }

    [Fact]
    public async Task Pipeline_Dispose_StopsAndDisposesCapture()
    {
        await using RecordingTapServer server = await RecordingTapServer.StartAsync();
        var capture = new FakeAudioCapture(new AudioFormat(16_000, 1, SampleKind.Int16));
        var connected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var options = new TapConnectionOptions { Host = "127.0.0.1", Port = server.Port, Identity = "x" };

        TapSession session = TapSession.Begin(
            capture, options,
            onConnected: () => connected.TrySetResult(),
            onFailed: ex => connected.TrySetException(ex));

        await connected.Task.WaitAsync(Wait);
        await session.DisposeAsync();

        Assert.True(capture.Stopped);  // started capture is stopped on teardown
        Assert.True(capture.Disposed);
    }

    // --- helpers -----------------------------------------------------------

    private static byte[] OneSecondOf48kStereoFloat()
    {
        const int frames = 48_000;
        var bytes = new byte[frames * 2 * 4];
        int offset = 0;
        for (int f = 0; f < frames; f++)
        {
            float sample = 0.5f * (float)Math.Sin(2 * Math.PI * 440 * f / 48_000);
            BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(offset, 4), sample);
            BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(offset + 4, 4), sample);
            offset += 8;
        }
        return bytes;
    }

    /// <summary>A scripted capture: raises DataAvailable on demand via Emit().</summary>
    private sealed class FakeAudioCapture(AudioFormat format) : IAudioCapture
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
    /// In-process /tap server that records every binary frame it receives until
    /// the client closes. With <c>reject: true</c> it refuses the upgrade (401),
    /// mirroring a rejected tap token.
    /// </summary>
    private sealed class RecordingTapServer : IAsyncDisposable
    {
        private readonly WebApplication _app;
        private readonly List<byte[]> _frames = [];
        private readonly TaskCompletionSource _completed = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public int Port { get; }
        public IReadOnlyList<byte[]> ReceivedFrames => _frames;
        public string? NegotiatedSubProtocol { get; private set; }
        public Task Completed => _completed.Task;

        private RecordingTapServer(WebApplication app, int port)
        {
            _app = app;
            Port = port;
        }

        public static async Task<RecordingTapServer> StartAsync(bool reject = false)
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
                if (reject)
                {
                    context.Response.StatusCode = StatusCodes.Status401Unauthorized; // refuse the upgrade
                    return;
                }

                string? chosen = context.WebSockets.WebSocketRequestedProtocols
                    .FirstOrDefault(p => p.StartsWith(TapWire.SubprotocolPrefix, StringComparison.Ordinal));
                using WebSocket ws = chosen is null
                    ? await context.WebSockets.AcceptWebSocketAsync()
                    : await context.WebSockets.AcceptWebSocketAsync(chosen);
                self!.NegotiatedSubProtocol = ws.SubProtocol;

                var buffer = new byte[4096];
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
                        if (result.MessageType == WebSocketMessageType.Binary && result.Count > 0)
                            self._frames.Add(buffer[..result.Count]);
                    }
                }
                catch (WebSocketException)
                {
                    // Client aborted the socket; stop collecting. The frames seen
                    // so far are still asserted by the test.
                }
                finally
                {
                    self._completed.TrySetResult();
                }
            });

            await app.StartAsync();
            string address = app.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!.Addresses.First();
            self = new RecordingTapServer(app, new Uri(address).Port);
            return self;
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }
}
