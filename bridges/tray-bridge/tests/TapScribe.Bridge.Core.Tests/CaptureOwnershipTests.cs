using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins B2 — the captures handed to <see cref="CaptureOrchestrator.StartAll"/> must be
/// released on the REJECTION path too. <see cref="PipelineSpec"/> declares that the
/// orchestrator takes ownership of its <see cref="PipelineSpec.Capture"/>, and the
/// per-device catch already honours that for a device that fails to start — but the
/// refusals that throw BEFORE or AFTER the loop (the duplicate-identity guard, an
/// unfiltered throw out of it) leave every capture the caller opened with no owner: the
/// shell handed ownership over with the specs and its finally then disposes only the
/// enumerator, so those endpoints leak for the process lifetime.
///
/// The duplicate guard is the core's own backstop, not the shell's route in:
/// <see cref="DeviceSelection.Resolve"/> takes the base identity and so returns
/// <see cref="SelectionVerdict.DuplicateIdentity"/> before any device is opened. It stays
/// because the core cannot assume its caller resolved through that path.
///
/// The enumerator is the same claim one level up. It hands its endpoint over to each capture
/// it opens, so it has to outlive them: an ordering that was a rule stated in prose at every
/// teardown path and re-implemented at each, precisely because no single object owned both.
/// <see cref="CaptureOrchestrator.StartAll"/> takes it, so "released, after the captures,
/// exactly once" is one implementation and the tests below are about it rather than about
/// each caller's memory.
/// </summary>
public class CaptureOwnershipTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    [Fact]
    public void StartAll_WhenItRefusesTheSpecs_ReleasesTheEnumeratorAfterTheCaptures()
    {
        // The unwind: every device fails to START, so the core releases each and then refuses
        // to hand back a meeting with zero pipelines. The caller has no handle left on the
        // enumerator either, having passed ownership with the specs, so anything left here is
        // held for the process lifetime.
        var transport = new FakeTapTransport();
        var enumerator = new FakeAudioDeviceEnumerator();
        FakeAudioCapture mic = Doomed();
        FakeAudioCapture system = Doomed();
        enumerator.Add(new CaptureDevice("mic", "mic", DeviceFlow.Capture, true), mic);
        enumerator.Add(new CaptureDevice("system", "system", DeviceFlow.Render, true), system);
        IAudioCapture[] opened =
        [
            enumerator.Open(enumerator.List()[0]),
            enumerator.Open(enumerator.List()[1]),
        ];

        Assert.Throws<InvalidOperationException>(() => CaptureOrchestrator.StartAll(
            [Spec(opened[0], "mic"), Spec(opened[1], "system")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            enumerator, gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create));

        Assert.True(enumerator.Disposed, "the enumerator was stranded by the unwind");
        Assert.Equal(1, enumerator.Disposals);
        Assert.True(enumerator.CapturesReleasedFirst,
            "the enumerator was released while a capture it opened was still live");
    }

    [Fact]
    public async Task DisposeAsync_ReleasesTheEnumeratorAfterTheCaptures_AndOnlyOnce()
    {
        var transport = new FakeTapTransport();
        var enumerator = new FakeAudioDeviceEnumerator();
        enumerator.Add(new CaptureDevice("mic", "mic", DeviceFlow.Capture, true), RecorderFormat);
        IAudioCapture mic = enumerator.Open(enumerator.List()[0]);
        CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            enumerator, gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        await orchestrator.DisposeAsync().AsTask().WaitAsync(Wait);
        // A second teardown is reachable in production: an abandoned start disposes the
        // meeting it built and the shell's own teardown may reach the same orchestrator.
        await orchestrator.DisposeAsync().AsTask().WaitAsync(Wait);

        Assert.True(enumerator.CapturesReleasedFirst,
            "the enumerator was released while the capture it opened was still live");
        Assert.Equal(1, enumerator.Disposals);
    }

    [Fact]
    public async Task EndMeetingAsync_WhenTheDrainThrows_StillReleasesTheEnumerator()
    {
        // The drain faults at its very first step (DrainAllAsync detaches before it awaits),
        // which used to skip the dispose entirely: the endpoints AND the enumerator stayed open
        // for the process lifetime, and the shell's End path carried its own finally to make up
        // for it. The failure still reaches the caller, since whether the taps flushed is its
        // business, but the devices are released either way.
        var transport = new FakeTapTransport();
        var enumerator = new FakeAudioDeviceEnumerator();
        var mic = new FakeAudioCapture(RecorderFormat)
        {
            DetachError = new IOException("endpoint invalidated"),
        };
        enumerator.Add(new CaptureDevice("mic", "mic", DeviceFlow.Capture, true), mic);
        CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
            [Spec(enumerator.Open(enumerator.List()[0]), "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            enumerator, gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        await Assert.ThrowsAsync<IOException>(() => orchestrator.EndMeetingAsync().WaitAsync(Wait));

        Assert.True(enumerator.Disposed, "the enumerator was stranded by the failed drain");
        Assert.Equal(1, enumerator.Disposals);
        // Deliberately NOT asserting the ordering here, which the clean teardown above does.
        // Captures-before-enumerator holds everywhere the teardown SUCCEEDS; this is the path
        // where it throws part-way, and the release is sequenced in a finally precisely so a
        // failed teardown cannot strand the endpoints' owner for the process lifetime. The
        // capture the throw skipped is still live, which is the leak the caller can still act
        // on; holding the ordering here would mean skipping the release instead.
        Assert.False(mic.Disposed, "the failing device released after all, so this proves nothing");
    }

    /// <summary>A capture whose Start throws the way an endpoint that is in use or already
    /// gone does, which is what makes the core refuse a set of specs it has already taken
    /// ownership of.</summary>
    private static FakeAudioCapture Doomed() => new(RecorderFormat)
    {
        StartError = new InvalidOperationException("device open failed"),
    };

    [Fact]
    public void StartAll_WhenAnUnfilteredThrowEscapes_ReleasesEveryCaptureAndSession()
    {
        // The path the "releases on all its throw paths" claim was false for. TapSession's
        // ctor validates the capture format and the gate tuning BEFORE it starts the device,
        // so an out-of-range gate raises an ArgumentOutOfRangeException — which the
        // per-device filter deliberately does not catch, because it is not a skippable
        // device failure. Everything opened by then had no owner left: an endpoint held
        // "in use" for the process lifetime, and a session already begun still streaming
        // with nothing able to stop it.
        var transport = new FakeTapTransport();
        var begun = new FakeAudioCapture(RecorderFormat);
        var doomed = new FakeAudioCapture(RecorderFormat);
        var untouched = new FakeAudioCapture(RecorderFormat);

        Assert.Throws<ArgumentOutOfRangeException>(() => CaptureOrchestrator.StartAll(
            [
                Spec(begun, "mic"),
                Spec(doomed, "system", gate: new GateOptions { OpenThreshold = -1 }),
                Spec(untouched, "line-in"),
            ],
            onConnected: _ => { }, onFailed: (_, _) => { },
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create));

        // The first pipeline really did begin, so the unwind below is a statement about a
        // path that was taken.
        Assert.True(begun.Started, "no session ever began, so this proves nothing");

        Assert.True(begun.Stopped, "a session that had begun was left running");
        Assert.True(begun.Disposed, "a session that had begun was never released");
        Assert.True(doomed.Disposed, "the capture whose pipeline threw was stranded");
        Assert.True(untouched.Disposed, "a capture the loop never reached was stranded");
    }

    [Fact]
    public void StartAll_WhenItRejectsDuplicateIdentities_ReleasesEveryCapture()
    {
        var transport = new FakeTapTransport();
        var a = new FakeAudioCapture(RecorderFormat);
        var b = new FakeAudioCapture(RecorderFormat);

        Assert.Throws<ArgumentException>(() => CaptureOrchestrator.StartAll(
            [Spec(a, "system"), Spec(b, "system")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create));

        // Still refused before any device is opened...
        Assert.False(a.Started);
        Assert.False(b.Started);
        // ...and nothing is left behind un-owned: the caller's finally has no handle on
        // these, so the orchestrator releases what it refuses.
        Assert.True(a.Disposed);
        Assert.True(b.Disposed);
    }
}
