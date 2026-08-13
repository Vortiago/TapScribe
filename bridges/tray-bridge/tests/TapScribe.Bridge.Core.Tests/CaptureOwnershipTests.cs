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
/// </summary>
public class CaptureOwnershipTests
{
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
            FastGate(), FastStream(), transport.Create));

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
            FastGate(), FastStream(), transport.Create));

        // Still refused before any device is opened...
        Assert.False(a.Started);
        Assert.False(b.Started);
        // ...and nothing is left behind un-owned: the caller's finally has no handle on
        // these, so the orchestrator releases what it refuses.
        Assert.True(a.Disposed);
        Assert.True(b.Disposed);
    }
}
