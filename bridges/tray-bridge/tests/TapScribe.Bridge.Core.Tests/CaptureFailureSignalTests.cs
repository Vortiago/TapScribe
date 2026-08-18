using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// RED contract for #218 — a capture that fails mid-stream (the endpoint invalidated
/// AFTER Start: unplugged / disabled / default-device switch) must reach the
/// orchestrator's per-identity onFailed channel, not be silently swallowed. Today
/// <see cref="IAudioCapture"/> leaks nothing about mid-stream liveness, so a lost mic
/// simply stops delivering <see cref="IAudioCapture.DataAvailable"/> frames — hours of a
/// meeting can go uncaptured with no signal, balloon, or gate-close.
///
/// Scope: the CORE forwarding limb only — a capture <c>Failed(exception)</c> reaches
/// <c>onFailed(identity, ex)</c>. WASAPI raising Failed from NAudio's RecordingStopped,
/// and the tray "microphone lost" balloon, are named follow-ups (Windows-only, verified
/// by the windows CI job) and are OUT of this gate. Asserted at the onFailed aggregation
/// layer, fix-agnostic about where the subscription lives (TapSession vs the orchestrator).
/// </summary>
public class CaptureFailureSignalTests
{
    [Fact]
    public async Task CaptureFailingMidStream_SurfacesThroughOnFailed_TaggedByIdentity()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(capture, "mic")]),
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        // The endpoint is invalidated mid-capture, after a clean Start — the exact case
        // the Stop()/Dispose() comments acknowledge (AUDCLNT_E_DEVICE_INVALIDATED).
        var boom = new InvalidOperationException("device invalidated mid-capture");
        capture.RaiseFailed(boom);

        var failure = Assert.Single(failures);
        Assert.Equal("mic", failure.Identity);
        Assert.Same(boom, failure.Error);
    }

    [Fact]
    public async Task OneCaptureFailingMidStream_LeavesTheOtherPipelinesStreaming()
    {
        // The claim the whole multi-pipeline shape rests on. A meeting is the operator's
        // microphone AND what the Mac plays, each its own device with its own way of going
        // away mid-call: a mic unplugged, an output the audio moved off. Losing one has to cost
        // exactly that one - the other speaker's track keeps being recorded, and the meeting
        // stays endable - because the alternative is that a headphone jack ends the meeting.
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(mic, "mic"), Spec(system, "system")]),
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        system.RaiseFailed(new ExternalException("the endpoint behind the tap was invalidated"));

        // Emitted AFTER the failure, so this is about what the surviving pipeline does from
        // here rather than about what it had already sent.
        mic.Emit(Loud(50));
        await Poll.UntilAsync(
            () => transport.HasStreamed("mic"), TimeSpan.FromSeconds(10), "the surviving mic to keep streaming");

        Assert.Equal("system", Assert.Single(failures).Identity);
        Assert.Equal(2, orchestrator.PipelineCount);
    }

    [Fact]
    public async Task CaptureStoppingCleanly_DoesNotSurfaceAsFailure()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(capture, "mic")]),
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        // A clean stop carries no exception — it must NOT be misreported as a failure.
        capture.RaiseFailed(null);

        Assert.Empty(failures);
    }

    // The forwarding is wired by subscribing to capture.Failed for the pipeline's life and
    // unsubscribing on every teardown path. Both green tests above raise Failed on a LIVE
    // session, so they pin the subscribe but leave the DETACH unpinned: a subscribe-but-
    // never-unsubscribe leak — the capture keeping a live handler on a torn-down session —
    // would ship green while a late device-loss Failed forwards to a dead identity (a
    // spurious "microphone lost" balloon after the meeting is over). These three pin the
    // detach on each teardown path: after teardown, a subsequent Failed must reach nobody.

    [Fact]
    public async Task DisposeAsync_DetachesFromCapture_SoAPostTeardownFailedIsNotForwarded()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(capture, "mic")]),
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);
        await orchestrator.DisposeAsync();

        // Endpoints can fire Failed on their own thread after Stop; the session must have
        // unsubscribed on dispose so this late loss reaches nobody.
        capture.RaiseFailed(new InvalidOperationException("device lost after dispose"));

        Assert.Empty(failures);
    }

    [Fact]
    public async Task DrainAllAsync_DetachesFromCapture_SoAPostTeardownFailedIsNotForwarded()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(capture, "mic")]),
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);
        await orchestrator.DrainAllAsync(); // End-of-meeting drain detaches before the un-capped await

        capture.RaiseFailed(new InvalidOperationException("device lost after drain"));

        Assert.Empty(failures);
    }

    [Fact]
    public async Task StartFailureUnwindingTheCtor_DetachesFromCapture_SoAPostFailureIsNotForwardedTwice()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat)
        {
            StartError = new InvalidOperationException("device open failed"),
        };
        // A healthy sibling, so the meeting still has a pipeline: an orchestrator on which
        // EVERY device failed to start is refused outright (ZeroPipelineMeetingTests), and
        // this test is about the failed device's ctor unwind, not about that refusal.
        var healthy = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(capture, "mic"), Spec(healthy, "system")]),
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        // The failed Start surfaces once through the orchestrator's per-identity catch.
        Assert.Single(failures);

        // The ctor unsubscribed as it unwound, so the half-built session forwards nothing
        // — a late Failed on the (now-disposed) capture must NOT add a second surfacing.
        capture.RaiseFailed(new InvalidOperationException("device lost after failed start"));

        Assert.Single(failures);
    }
}
