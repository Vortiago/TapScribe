using System.Buffers.Binary;
using System.Diagnostics;
using System.Net.WebSockets;
using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="CaptureOrchestrator"/> — N concurrent per-identity capture
/// pipelines over fake devices and a deterministic <see cref="FakeTapTransport"/>.
/// Pipelines are attributed by Identity (read off the connection), never by frame
/// bytes: the <see cref="Resampler"/> rewrites them and the <see cref="LevelGate"/>
/// only passes loud frames. No real microphone, loopback, or socket anywhere.
/// </summary>
public class CaptureOrchestratorTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static PipelineSpec Spec(IAudioCapture capture, string identity, string name = "") =>
        new(capture, new TapConnectionOptions { Identity = identity, Name = name });

    [Fact]
    public async Task StartAll_WithOneSpec_StreamsUnderItsIdentity()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(40)); // crosses the threshold -> opens an Utterance, connects, streams
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "the pipeline to stream");

        Assert.Equal(1, orchestrator.PipelineCount);
        FakeTapConnection conn = Assert.Single(transport.Connections);
        Assert.Equal("mic", conn.Identity);
    }

    [Fact]
    public async Task StartAll_WithTwoSpecs_StreamsEachUnderItsOwnIdentityAndName()
    {
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic", "Alice"), Spec(system, "system", "System Audio")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        mic.Emit(Loud(40));
        system.Emit(Loud(40));
        await Poll.UntilAsync(
            () => transport.ConnectionsFor("mic").Count > 0 && transport.ConnectionsFor("system").Count > 0,
            Wait, "both pipelines to stream");

        Assert.Equal(2, orchestrator.PipelineCount);

        // Each device streams under its OWN identity and name — the per-speaker split.
        FakeTapConnection micConn = Assert.Single(transport.ConnectionsFor("mic"));
        Assert.Equal("Alice", micConn.Name);
        FakeTapConnection systemConn = Assert.Single(transport.ConnectionsFor("system"));
        Assert.Equal("System Audio", systemConn.Name);

        // No cross-attribution: every connection belongs to exactly one of the two.
        Assert.All(transport.Connections, c => Assert.Contains(c.Identity, new[] { "mic", "system" }));
    }

    [Fact]
    public async Task OneDevicesSilence_DoesNotCloseAnotherDevicesUtterance()
    {
        var transport = new FakeTapTransport();
        var a = new FakeAudioCapture(RecorderFormat);
        var b = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(a, "a"), Spec(b, "b")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        // Both speaking -> both utterances open.
        a.Emit(Loud(20));
        b.Emit(Loud(20));
        await Poll.UntilAsync(
            () => transport.ConnectionsFor("a").Count > 0 && transport.ConnectionsFor("b").Count > 0,
            Wait, "both utterances to open");

        // B falls silent past the hangover -> B's utterance closes; A keeps speaking.
        b.Emit(Silence(10));
        a.Emit(Loud(20));
        await Poll.UntilAsync(() => transport.ConnectionsFor("b")[0].Closed, Wait, "B's utterance to close");

        // A's utterance is untouched by B's gate — each pipeline gates independently.
        Assert.False(transport.ConnectionsFor("a")[0].Closed);
    }

    // A WASAPI loopback render mix is typically 48 kHz / 2 ch / Float32; the core
    // Resampler downmixes + resamples it to the 16 kHz mono int16 wire format. This
    // proves a loopback-SHAPED source streams through the standard pipeline without
    // any Windows audio — the format-handling half of "loopback streams to /tap".
    private static AudioFormat RenderMixFormat => new(48_000, 2, SampleKind.Float32);

    private static byte[] LoudStereoFloat(int interleavedFrames)
    {
        // Interleaved L/R float32 at a loud amplitude (0.5), enough to cross the gate
        // after downmix + resample. 8 bytes per stereo sample frame.
        var bytes = new byte[interleavedFrames * RenderMixFormat.BytesPerInterleavedFrame];
        for (int i = 0; i < interleavedFrames * RenderMixFormat.Channels; i++)
            BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(i * 4, 4), 0.5f);
        return bytes;
    }

    [Fact]
    public async Task LoopbackShapedSource_StreamsThroughStandardPipeline()
    {
        var transport = new FakeTapTransport();
        var loopback = new FakeAudioCapture(RenderMixFormat); // 48 kHz / 2 ch / Float32
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(loopback, "system", "System Audio")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        // 48 kHz: 480 interleaved samples = 10 ms; emit ~200 ms so several 16 kHz
        // wire frames survive the downmix/resample and open the gate.
        loopback.Emit(LoudStereoFloat(48_000 / 5));
        await Poll.UntilAsync(() => transport.ConnectionsFor("system").Count > 0, Wait, "loopback to open");
        await Poll.UntilAsync(() => transport.SentCount(0) > 0, Wait, "loopback frames to stream");

        Assert.True(transport.ConnectionsFor("system")[0].SentCount > 0);
    }

    [Fact]
    public async Task DisposeAsync_DrainsClosesAndStopsAllPipelines()
    {
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic"), Spec(system, "system")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        mic.Emit(Loud(20));
        system.Emit(Loud(20));
        await Poll.UntilAsync(
            () => transport.ConnectionsFor("mic").Count > 0 && transport.ConnectionsFor("system").Count > 0,
            Wait, "both pipelines to stream");

        await orchestrator.DisposeAsync();

        Assert.True(mic is { Stopped: true, Disposed: true });
        Assert.True(system is { Stopped: true, Disposed: true });
        Assert.All(transport.Connections, c => Assert.True(c.Closed)); // every open utterance drained + closed
    }

    [Fact]
    public async Task DisposeAsync_IsBounded_NotSerialized()
    {
        var transport = new FakeTapTransport();
        TapStreamOptions stream = FastStream(); // DrainBudget = 500 ms
        const int n = 5;
        var captures = new List<FakeAudioCapture>();
        var specs = new List<PipelineSpec>();
        for (int i = 0; i < n; i++)
        {
            var capture = new FakeAudioCapture(RecorderFormat);
            captures.Add(capture);
            specs.Add(Spec(capture, $"dev{i}"));
        }

        var orchestrator = CaptureOrchestrator.StartAll(
            specs, onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), stream, transport.Create);

        foreach (FakeAudioCapture capture in captures)
            capture.Emit(Loud(20)); // open + connect + stream
        await Poll.UntilAsync(() => transport.Connections.Count >= n, Wait, "all pipelines to connect");

        // Knock the transport down and buffer un-sendable frames, so each pipeline's
        // drain must wait out its full DrainBudget (it can't flush against a wall).
        transport.Up = false;
        foreach (FakeAudioCapture capture in captures)
            capture.Emit(Loud(5));

        var sw = Stopwatch.StartNew();
        await orchestrator.DisposeAsync();
        sw.Stop();

        // Serial teardown is ~n * DrainBudget (2.5 s); bounded teardown is ~one
        // budget. A regression that disposes sessions one-by-one trips here.
        Assert.True(sw.Elapsed < TimeSpan.FromMilliseconds(1500),
            $"DisposeAsync took {sw.ElapsedMilliseconds} ms; expected ~one drain budget, not n*budget");
    }

    [Fact]
    public async Task StartAll_WhenOneDeviceFailsToOpen_KeepsOthers_DisposesFailedCapture_SurfacesIt()
    {
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat);
        var badSystem = new ThrowingOnStartCapture(RecorderFormat); // device fails to open
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic"), Spec(badSystem, "system")],
            onConnected: _ => { },
            onFailed: (id, ex) => { lock (failures) failures.Add((id, ex)); },
            FastGate(), FastStream(), transport.Create);

        // Best-effort: the device that opened still runs the meeting.
        Assert.Equal(1, orchestrator.PipelineCount);
        mic.Emit(Loud(40));
        await Poll.UntilAsync(() => transport.ConnectionsFor("mic").Count > 0, Wait, "the good pipeline to stream");

        // The failed device is surfaced under its identity, and its capture is
        // disposed (TapSession.Begin starts capture in its ctor and rethrows WITHOUT
        // disposing — the orchestrator must, or it leaks).
        Assert.Contains(failures, f => f.Identity == "system");
        Assert.True(badSystem.Disposed);
    }

    [Fact]
    public async Task OnePipelineFirstConnectFailure_SurfacesTaggedByIdentity_OthersUnaffected()
    {
        var transport = new FakeTapTransport();
        transport.SetDown("system"); // only the system pipeline's connect fails
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic"), Spec(system, "system")],
            onConnected: _ => { },
            onFailed: (id, ex) => { lock (failures) failures.Add((id, ex)); },
            FastGate(), FastStream(), transport.Create);

        mic.Emit(Loud(40));    // mic connects + streams
        system.Emit(Loud(40)); // system opens an utterance whose first connect fails

        await Poll.UntilAsync(() => failures.Count > 0, Wait, "the down pipeline to surface a failure");
        await Poll.UntilAsync(() => transport.ConnectionsFor("mic").Count > 0, Wait, "the up pipeline to stream");

        // The failure is attributed to the right device; the other keeps recording.
        (string Identity, Exception Error) failure = Assert.Single(failures);
        Assert.Equal("system", failure.Identity);
        Assert.IsType<WebSocketException>(failure.Error);
        Assert.True(transport.ConnectionsFor("mic")[0].SentCount > 0);
    }

    [Fact]
    public void StartAll_WithDuplicateIdentities_ThrowsBeforeStartingAny()
    {
        var transport = new FakeTapTransport();
        var a = new FakeAudioCapture(RecorderFormat);
        var b = new FakeAudioCapture(RecorderFormat);

        // Two devices under one identity would cross-attribute at the Recorder (it
        // buckets WAVs by the sanitised identity), so the orchestrator refuses up front.
        Assert.Throws<ArgumentException>(() => CaptureOrchestrator.StartAll(
            [Spec(a, "system"), Spec(b, "system")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create));

        // The guard runs before any device is opened — nothing was started.
        Assert.False(a.Started);
        Assert.False(b.Started);
    }

    [Fact]
    public async Task TwoSpecsSharingSession_CoLocate_ButKeepDistinctIdentities()
    {
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        const string session = "2026-06-16T12-00-00";

        await using var orchestrator = CaptureOrchestrator.StartAll(
            [
                new PipelineSpec(mic, new TapConnectionOptions { Identity = "mic", Session = session }),
                new PipelineSpec(system, new TapConnectionOptions { Identity = "system", Session = session }),
            ],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        mic.Emit(Loud(40));
        system.Emit(Loud(40));
        await Poll.UntilAsync(
            () => transport.ConnectionsFor("mic").Count > 0 && transport.ConnectionsFor("system").Count > 0,
            Wait, "both pipelines to stream");

        // Co-located: both land in ONE (detached) session, distinct speakers within it.
        Assert.Equal(session, transport.ConnectionsFor("mic")[0].Session);
        Assert.Equal(session, transport.ConnectionsFor("system")[0].Session);
        Assert.NotEqual(
            transport.ConnectionsFor("mic")[0].Identity,
            transport.ConnectionsFor("system")[0].Identity);
    }
}
