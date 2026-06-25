using System.Net.WebSockets;
using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

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
    public async Task UpdateGate_RetunesTheLivePipeline_WithoutRestart()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        // Start deaf (threshold above a loud frame's RMS) so this level never opens.
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, DeafGate(), FastStream(), transport.Create);

        // The gate runs synchronously on the capture thread, so once Emit returns the
        // deaf gate has already decided NOT to open — no Utterance, no connection.
        capture.Emit(Loud(20));
        Assert.Empty(transport.Connections);

        // Re-tune sensitive mid-meeting (no Stop/Start) and the same level now records.
        session.UpdateGate(FastGate()); // OpenThreshold 0.02
        capture.Emit(Loud(20));
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "the re-tuned pipeline to stream");

        await session.DisposeAsync();

        FakeTapConnection conn = Assert.Single(transport.Connections);
        Assert.Equal("mic", conn.Identity);
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
    public async Task MutedMic_RecordsNothing_AndUnmuteResumesCapture()
    {
        // #159: a muted mic still delivers a residual (noise floor / DC offset / device
        // blips) that crosses the level gate, opening a recurring "quiet" tap. Honouring
        // the device mute makes "muted" a hard gate-closed: no tap while muted, capture
        // resumes on unmute. Asserting on the CONNECTION COUNT after dispose is the
        // deterministic signal: TapStream connects on a background pump, so a check right
        // after Emit would race it — but DisposeAsync awaits every pump, so the final
        // count is stable. The mid-stream Silence is load-bearing for the red signal: it
        // closes the bug's muted tap so the later unmuted speech opens a SECOND one
        // (buggy -> 2 connections), whereas honouring mute opens only the unmuted one
        // (fixed -> 1).
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream(), transport.Create);

        capture.SetMuted(true);     // mic muted at the OS/endpoint level
        capture.Emit(Loud(40));     // the residual a muted endpoint keeps delivering — RMS 0.24, over the gate
        capture.Emit(Silence(10));  // > hangover: would close the bug's muted tap, separating it from the next
        capture.SetMuted(false);    // unmute
        capture.Emit(Loud(40));     // resumes — opens exactly one tap
        await Poll.UntilAsync(() => transport.HasStreamed("mic"), Wait, "the unmuted audio to stream");

        await session.DisposeAsync();

        // Only the unmuted speech ever streamed; the muted residual opened no tap.
        Assert.Single(transport.Connections);
    }

    [Fact]
    public async Task MutingMidUtterance_ClosesItPromptly_WithoutWaitingOutTheHangover()
    {
        // An open utterance must close the instant the mic mutes, not linger streaming
        // the residual until the gate's hangover elapses — so mute drives a prompt close
        // with no silence frames at all.
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(20)); // opens an utterance
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "the utterance to stream");

        capture.SetMuted(true); // mute mid-utterance — closes it without any silence frames
        await Poll.UntilAsync(() => transport.Connections[0].Closed, Wait, "the muted utterance to close");

        await session.DisposeAsync();
    }

    [Fact]
    public async Task UnmuteResumesCapture_EvenWhenNoFramesArriveWhileMuted()
    {
        // The gate resync on mute must NOT depend on a frame arriving during the muted
        // interval: if the device stops delivering frames while muted, the gate would
        // otherwise stay open from the pre-mute utterance and swallow the first resumed
        // frame as a continuation into the already-closed tap — losing the resumed speech.
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var session = TapSession.Begin(capture, new TapConnectionOptions { Identity = "mic" },
            onConnected: () => { }, onFailed: _ => { }, FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(20)); // utterance 1 opens
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "utterance 1 to stream");

        capture.SetMuted(true); // closes utterance 1; NO frames are emitted while muted
        await Poll.UntilAsync(() => transport.Connections[0].Closed, Wait, "utterance 1 to close on mute");

        capture.SetMuted(false); // unmute, then resume speech — must open a fresh utterance
        capture.Emit(Loud(20));
        await Poll.UntilAsync(() => transport.Connections.Count >= 2 && transport.Connections[1].SentCount > 0,
            Wait, "resumed speech to open a fresh utterance");

        await session.DisposeAsync();

        Assert.Equal(2, transport.Connections.Count);
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
