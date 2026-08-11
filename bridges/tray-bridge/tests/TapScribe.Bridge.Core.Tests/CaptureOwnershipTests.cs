using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins B2 — the captures handed to <see cref="CaptureOrchestrator.StartAll"/> must be
/// released on the REJECTION path too. <see cref="PipelineSpec"/> declares that the
/// orchestrator takes ownership of its <see cref="PipelineSpec.Capture"/>, and the
/// per-device catch already honours that for a device that fails to start — but the
/// duplicate-identity guard throws BEFORE the loop, so every capture the caller opened
/// is stranded with no owner. The tray shell's Start reaches that guard for real: two
/// device selections whose identities differ only by a blank one (which
/// <see cref="ResolveResult.ToTapOptions"/> substitutes the base identity for) pass
/// <see cref="SelectionVerdict.Ok"/> and collide here, and the shell's finally disposes
/// only the enumerator — so both WASAPI captures leak for the process lifetime.
/// </summary>
public class CaptureOwnershipTests
{
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
