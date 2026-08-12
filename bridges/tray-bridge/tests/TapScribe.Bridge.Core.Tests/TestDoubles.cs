using System.Buffers.Binary;
using System.Diagnostics;
using System.Net.WebSockets;
using System.Text;
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

/// <summary>
/// Synthetic-audio fixtures shared by the gated-pipeline tests (TapSession +
/// CaptureOrchestrator): the recorder wire format, fast gate/stream tunings so tests
/// don't wait real hangovers/backoffs, and DC-level PCM builders. Hoisted here next to
/// the shared <see cref="Poll"/> / fakes instead of a per-class copy. 16 kHz mono int16,
/// so the resampler is a near-identity and the gate sees the level emitted: value 8000
/// -> RMS 0.24 (opens the gate); 0 -> silent.
/// </summary>
internal static class Fixtures
{
    public static AudioFormat RecorderFormat => new(16_000, 1, SampleKind.Int16);

    public static TapStreamOptions FastStream() => new()
    {
        Backoff = [TimeSpan.FromMilliseconds(10), TimeSpan.FromMilliseconds(20)],
        BackoffCap = TimeSpan.FromMilliseconds(40),
        BackoffJitter = 0,
        DrainBudget = TimeSpan.FromMilliseconds(500),
        PollInterval = TimeSpan.FromMilliseconds(10),
    };

    // Opens easily, closes after a short hangover, so tests run fast.
    public static GateOptions FastGate() => new()
    {
        OpenThreshold = 0.02,
        Hangover = TimeSpan.FromMilliseconds(60), // 3 silent frames
        PreRoll = TimeSpan.Zero,
    };

    // The opposite of FastGate: an open threshold (0.3) above even a loud frame's RMS
    // (0.244), so nothing at the test levels opens the gate. The starting point for a
    // live-retune test that then drops the gate to a sensitive tuning.
    public static GateOptions DeafGate() => new()
    {
        OpenThreshold = 0.3,
        Hangover = TimeSpan.FromMilliseconds(60),
        PreRoll = TimeSpan.Zero,
    };

    public static byte[] Pcm(short value, int frames)
    {
        var bytes = new byte[frames * TapWire.FrameBytes];
        for (int i = 0; i < frames * TapWire.FrameSamples; i++)
            BinaryPrimitives.WriteInt16LittleEndian(bytes.AsSpan(i * 2, 2), value);
        return bytes;
    }

    public static byte[] Loud(int frames) => Pcm(8000, frames);
    public static byte[] Silence(int frames) => Pcm(0, frames);

    /// <summary>One pipeline to start: a capture under an identity, with the display name
    /// defaulting to that identity and no per-device gate. Every orchestrator test needs this
    /// and each had grown its own copy; the two that need more (a distinct display name, a
    /// per-device gate) pass them.</summary>
    public static PipelineSpec Spec(
        IAudioCapture capture, string identity, string? name = null, GateOptions? gate = null) =>
        new(capture, new TapConnectionOptions { Identity = identity, Name = name ?? identity }, gate);
}

/// <summary>A scripted capture: raises <see cref="DataAvailable"/> on demand via
/// <see cref="Emit"/>, so a test feeds synthetic PCM with no real audio device.</summary>
internal sealed class FakeAudioCapture(AudioFormat format) : IAudioCapture
{
    public AudioFormat Format { get; } = format;
    public bool Started { get; private set; }
    public bool Stopped { get; private set; }
    public bool Disposed { get; private set; }

    /// <summary>When set, <see cref="Start"/> throws — a device that fails to open (in use,
    /// invalidated, unsupported format). Records disposal (via <see cref="Disposed"/>) and
    /// still supports real <see cref="Failed"/> subscription, so a test can drive both the
    /// orchestrator's failed-capture cleanup and TapSession's ctor-catch unwind, then assert
    /// a late Failed reaches nobody.</summary>
    public bool ThrowOnStart { get; init; }

    /// <summary>When set, <see cref="Stop"/> throws — the endpoint was invalidated while
    /// the meeting ran (unplugged / disabled / default-device switch), which is what
    /// AUDCLNT_E_DEVICE_INVALIDATED does to a WASAPI client at teardown. The seam does not
    /// promise a throw-free <see cref="IAudioCapture.Stop"/> (only Dispose is
    /// contract-bound), so the core must survive a backend that doesn't swallow it.</summary>
    public bool ThrowOnStop { get; init; }

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    public bool IsMuted { get; private set; }
    public event EventHandler? MuteChanged;

    public event EventHandler<Exception?>? Failed;

    public void Start()
    {
        if (ThrowOnStart)
            throw new InvalidOperationException("device open failed");
        Started = true;
    }

    public void Stop()
    {
        Stopped = true;
        if (ThrowOnStop)
            throw new InvalidOperationException("endpoint invalidated");
    }
    public void Dispose() => Disposed = true;

    public void Emit(byte[] pcm) => DataAvailable?.Invoke(this, new AudioCapturedEventArgs(pcm));

    /// <summary>Raise <see cref="Failed"/> — the synthetic stand-in for the OS
    /// invalidating the endpoint mid-capture (unplugged/disabled). A null
    /// <paramref name="error"/> models a clean stop, which is NOT a failure.</summary>
    public void RaiseFailed(Exception? error) => Failed?.Invoke(this, error);

    /// <summary>Flip the reported mute state and raise <see cref="MuteChanged"/> — the
    /// synthetic stand-in for the OS muting/unmuting the mic, so a test drives the
    /// mic-mute path with no real endpoint. A real endpoint still delivers frames while
    /// muted, so tests <see cref="Emit"/> audio after <c>SetMuted(true)</c> to model the
    /// residual that the level gate would otherwise tap.</summary>
    public void SetMuted(bool muted)
    {
        IsMuted = muted;
        MuteChanged?.Invoke(this, EventArgs.Empty);
    }
}

/// <summary>
/// A scripted device enumerator: returns a fixed <see cref="CaptureDevice"/> list
/// and hands out a per-device <see cref="FakeAudioCapture"/> from <see cref="Open"/>,
/// so the orchestration is driven with synthetic PCM and no real audio hardware.
/// The capture-side analog of <see cref="FakeTapTransport"/>.
/// </summary>
internal sealed class FakeAudioDeviceEnumerator : IAudioDeviceEnumerator
{
    private readonly List<CaptureDevice> _devices = [];
    private readonly Dictionary<string, FakeAudioCapture> _captures = new(StringComparer.Ordinal);

    /// <summary>Register a device and the format its <see cref="Open"/>ed capture
    /// reports; returns that capture so the test can <c>Emit</c> PCM into it.</summary>
    public FakeAudioCapture Add(CaptureDevice device, AudioFormat format)
    {
        var capture = new FakeAudioCapture(format);
        _devices.Add(device);
        _captures[device.Id] = capture;
        return capture;
    }

    public IReadOnlyList<CaptureDevice> List() => _devices.ToList();

    public IAudioCapture Open(CaptureDevice device) =>
        _captures.TryGetValue(device.Id, out FakeAudioCapture? capture)
            ? capture
            : throw new ArgumentException($"unknown device id '{device.Id}'", nameof(device));
}

/// <summary>
/// A portable stand-in for a platform's at-rest token translation (DPAPI on Windows, the
/// Keychain on macOS): "protects" by base64-ing, which is enough for a test to prove the
/// plaintext never reaches the file without needing a real secret store. Models the two
/// contract points every real implementation shares — an empty token has no at-rest value,
/// and a value the platform can't read degrades to "" rather than throwing.
/// </summary>
internal sealed class FakeTapTokenStore : ITapTokenStore
{
    public string? Write(string token) =>
        string.IsNullOrEmpty(token) ? null : Convert.ToBase64String(Encoding.UTF8.GetBytes(token));

    public string Read(string? atRest)
    {
        if (string.IsNullOrEmpty(atRest))
            return "";
        try
        {
            return Encoding.UTF8.GetString(Convert.FromBase64String(atRest));
        }
        catch (FormatException)
        {
            // Hand-edited / foreign at-rest value, the stand-in for a blob DPAPI or the
            // Keychain can't decrypt: read as "no saved token" so the app still launches.
            // What's lost is the operator's saved token — they re-enter it in the dialog.
            return "";
        }
    }
}

/// <summary>
/// The other shape of <see cref="ITapTokenStore"/>: the secret lives OUT-OF-BAND (the
/// macOS Keychain), so <see cref="Write"/> keeps it here and returns null — the settings
/// file gets no at-rest value at all, and <see cref="Read"/> ignores what the file says.
/// <see cref="Held"/> is what the platform secret store would be holding.
/// </summary>
internal sealed class OutOfBandTapTokenStore : ITapTokenStore
{
    public string? Held { get; private set; }

    public string? Write(string token)
    {
        Held = string.IsNullOrEmpty(token) ? null : token;
        return null;
    }

    public string Read(string? atRest) => Held ?? "";
}

/// <summary>
/// A token store the platform refuses: every <see cref="Read"/> throws, the stand-in for a
/// Keychain the operator declined to unlock or a secrets daemon that isn't up. The
/// interface asks an implementation to degrade rather than throw, but the store this is
/// handed to can't verify that of a platform it doesn't own — so the portable half is
/// pinned to survive one that misbehaves.
/// </summary>
internal sealed class DeniedTapTokenStore : ITapTokenStore
{
    public string? Write(string token) => throw new UnauthorizedAccessException("denied by the platform");

    public string Read(string? atRest) => throw new UnauthorizedAccessException("denied by the platform");
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

    // Per-identity outage: a connection whose options.Identity is listed here
    // fails to connect/send while the others stay up, so a multi-pipeline test can
    // knock out exactly one device. Snapshot array (like Up) so pump threads read
    // it lock-free; configure it before emitting audio.
    private volatile string[] _down = [];
    public void SetDown(params string[] identities) => _down = identities;
    internal bool IsUpFor(string identity) => Up && Array.IndexOf(_down, identity) < 0;

    public IReadOnlyList<FakeTapConnection> Connections
    {
        get { lock (_lock) return _conns.ToList(); }
    }

    /// <summary>All connections opened under <paramref name="identity"/> — the
    /// per-pipeline view a multi-device test asserts on (frame bytes can't be used
    /// for attribution; they're rewritten by the Resampler and RMS-gated).</summary>
    public IReadOnlyList<FakeTapConnection> ConnectionsFor(string identity)
    {
        lock (_lock)
            return _conns.Where(c => c.Identity == identity).ToList();
    }

    public int SentCount(int connIndex)
    {
        lock (_lock)
            return connIndex < _conns.Count ? _conns[connIndex].SentCount : 0;
    }

    /// <summary>True once some connection under <paramref name="identity"/> has sent at
    /// least one frame — the race-free "this pipeline is streaming" signal. A connection is
    /// created (so <see cref="ConnectionsFor"/> is non-empty) BEFORE its pump sends the
    /// first frame, so polling on connection-count and then reading SentCount can observe a
    /// transient 0; poll on this instead.</summary>
    public bool HasStreamed(string identity) => ConnectionsFor(identity).Any(c => c.SentCount > 0);

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

    /// <summary>The options this connection was opened with, snapshotted once. Per-
    /// identity attribution (Identity/Name/Session) is read off this, since the
    /// frame bytes are rewritten by the Resampler and never carry identity.</summary>
    public TapConnectionOptions Options { get; } = options;
    public string Identity => Options.Identity;
    public string Name => Options.Name;
    public string? Session => Options.Session;
    public string? UtteranceId => Options.UtteranceId;
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
        if (!transport.IsUpFor(Options.Identity))
            throw new WebSocketException(WebSocketError.Faulted, "transport down");
        return Task.CompletedTask;
    }

    public Task SendFrameAsync(ReadOnlyMemory<byte> frame, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!transport.IsUpFor(Options.Identity))
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
