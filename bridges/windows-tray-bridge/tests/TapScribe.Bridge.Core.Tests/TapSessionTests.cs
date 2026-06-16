using System.Buffers.Binary;
using System.Net.WebSockets;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for the gated capture pipeline (capture → resampler → level gate → a
/// <see cref="TapStream"/> per Utterance). The gating composition is driven by a
/// <see cref="FakeAudioCapture"/> feeding synthetic 16 kHz mono int16 PCM into a
/// deterministic <see cref="FakeTapTransport"/>; one test runs the whole chain
/// against a real in-process /tap server. No real microphone or audio stack.
/// </summary>
public class TapSessionTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static TapStreamOptions FastStream() => new()
    {
        Backoff = [TimeSpan.FromMilliseconds(10), TimeSpan.FromMilliseconds(20)],
        BackoffCap = TimeSpan.FromMilliseconds(40),
        BackoffJitter = 0,
        DrainBudget = TimeSpan.FromMilliseconds(500),
        PollInterval = TimeSpan.FromMilliseconds(10),
    };

    // Gate that opens easily and closes after a short hangover, so tests run fast.
    private static GateOptions FastGate() => new()
    {
        OpenThreshold = 0.02,
        Hangover = TimeSpan.FromMilliseconds(60), // 3 silent frames
        PreRoll = TimeSpan.Zero,
    };

    // 16 kHz mono int16, so the resampler is a near-identity and the gate sees the
    // level we emit. value 8000 -> RMS 0.24 (loud); 0 -> silent.
    private static AudioFormat RecorderFormat => new(16_000, 1, SampleKind.Int16);

    private static byte[] Pcm(short value, int frames)
    {
        var bytes = new byte[frames * TapWire.FrameBytes];
        for (int i = 0; i < frames * TapWire.FrameSamples; i++)
            BinaryPrimitives.WriteInt16LittleEndian(bytes.AsSpan(i * 2, 2), value);
        return bytes;
    }

    private static byte[] Loud(int frames) => Pcm(8000, frames);
    private static byte[] Silence(int frames) => Pcm(0, frames);

    [Fact]
    public async Task Begin_StartsCaptureImmediately_SoTheGateCanHearSpeech()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream(), transport.Create);

        Assert.True(capture.Started);        // running before any audio, unlike the tracer bullet
        Assert.Empty(transport.Connections); // but no Utterance / WS until the level crosses

        await session.DisposeAsync();
    }

    [Fact]
    public async Task LoudAudio_OpensUtterance_MintsId_AndStreamsFrames()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var connected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => connected.TrySetResult(),
            onFailed: ex => connected.TrySetException(ex),
            FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(40)); // crosses the threshold -> opens an Utterance, connects, streams
        await connected.Task.WaitAsync(Wait);
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "frames to stream");

        await session.DisposeAsync();

        FakeTapConnection conn = Assert.Single(transport.Connections);
        Assert.False(string.IsNullOrEmpty(conn.UtteranceId)); // a fresh id was minted
        Assert.True(conn.SentCount > 0);
    }

    [Fact]
    public async Task Silence_AfterHangover_ClosesUtterance_AndTheNextSpeechOpensAFreshOne()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(20)); // Utterance 1
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "utterance 1 to stream");
        capture.Emit(Silence(10)); // > hangover -> Utterance 1 closes (drains + closes cleanly)
        await Poll.UntilAsync(() => transport.Connections[0].Closed, Wait, "utterance 1 to close");

        capture.Emit(Loud(20)); // Utterance 2 -> a new WS
        await Poll.UntilAsync(() => transport.Connections.Count >= 2, Wait, "utterance 2 to open");

        await session.DisposeAsync();

        Assert.True(transport.Connections.Count >= 2);
        Assert.True(transport.Connections[0].Closed);
        // Each Utterance carries its own id, so the Recorder writes a separate WAV.
        Assert.NotEqual(transport.Connections[0].UtteranceId, transport.Connections[1].UtteranceId);
    }

    [Fact]
    public async Task UtteranceFirstConnectFailure_SurfacesViaOnFailed()
    {
        var transport = new FakeTapTransport { Up = false }; // Recorder unreachable / refusing
        var capture = new FakeAudioCapture(RecorderFormat);
        var failed = new TaskCompletionSource<Exception>(TaskCreationOptions.RunContinuationsAsynchronously);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: ex => failed.TrySetResult(ex),
            FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(20)); // opens an Utterance whose first connect fails

        Exception error = await failed.Task.WaitAsync(Wait);
        Assert.IsType<WebSocketException>(error);

        await session.DisposeAsync();
    }

    [Fact]
    public async Task Dispose_DrainsTheOpenUtterance_AndStopsAndDisposesCapture()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(20));
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "the Utterance to stream");

        await session.DisposeAsync();

        Assert.True(transport.Connections[0].Closed, "the open Utterance is drained + closed on dispose");
        Assert.True(capture.Stopped);
        Assert.True(capture.Disposed);
    }

    [Fact]
    public async Task FullChain_StreamsToRealTapServer_WithSubprotocol()
    {
        await using RecordingTapServer server = await RecordingTapServer.StartAsync();
        var capture = new FakeAudioCapture(RecorderFormat);
        var options = new TapConnectionOptions
        {
            Host = "127.0.0.1",
            Port = server.Port,
            Identity = "mic",
            Token = "tok-abc",
        };
        // Default connection factory => the real TapClient over a real WebSocket.
        var session = TapSession.Begin(capture, options,
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream());

        capture.Emit(Loud(50)); // opens an Utterance -> real /tap WS -> frames
        await server.WaitForFramesAsync(1, Wait);

        await session.DisposeAsync();

        Assert.True(server.Connections.Count >= 1);
        Assert.Equal("tapscribe.v1.tap.tok-abc", server.Connections[0].SubProtocol);
        Assert.True(server.TotalFrames > 0);
    }
}
