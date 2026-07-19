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
    private static PipelineSpec Spec(IAudioCapture capture, string identity) =>
        new(capture, new TapConnectionOptions { Identity = identity, Name = "" }, null);

    [Fact]
    public async Task CaptureFailingMidStream_SurfacesThroughOnFailed_TaggedByIdentity()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            FastGate(), FastStream(), transport.Create);

        // The endpoint is invalidated mid-capture, after a clean Start — the exact case
        // the Stop()/Dispose() comments acknowledge (AUDCLNT_E_DEVICE_INVALIDATED).
        var boom = new InvalidOperationException("device invalidated mid-capture");
        capture.RaiseFailed(boom);

        var failure = Assert.Single(failures);
        Assert.Equal("mic", failure.Identity);
        Assert.Same(boom, failure.Error);
    }

    [Fact]
    public async Task CaptureStoppingCleanly_DoesNotSurfaceAsFailure()
    {
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        var failures = new List<(string Identity, Exception Error)>();

        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { },
            onFailed: (id, ex) => failures.Add((id, ex)),
            FastGate(), FastStream(), transport.Create);

        // A clean stop carries no exception — it must NOT be misreported as a failure.
        capture.RaiseFailed(null);

        Assert.Empty(failures);
    }
}
