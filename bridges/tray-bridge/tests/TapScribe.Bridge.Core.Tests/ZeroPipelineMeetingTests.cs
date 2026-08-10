using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins B1 — a meeting in which EVERY device failed to start must not come back as a
/// live meeting. <see cref="CaptureOrchestrator.StartAll"/> catches a capture whose
/// <c>Start()</c> throws, reports it through <c>onFailed</c> and carries on, so one
/// dead device can't sink the others (best-effort, by design). But when every device
/// dies that leaves an orchestrator with <c>PipelineCount == 0</c>, which the tray
/// shell publishes anyway: End meeting goes live, the header reads
/// "● Streaming — 0/2 devices" under the green icon, and nothing is being recorded.
///
/// Zero pipelines is not a meeting. The orchestrator refuses to hand one back — the
/// symmetric half of the shell's existing refusal when every device fails to OPEN.
/// </summary>
public class ZeroPipelineMeetingTests
{
    private static PipelineSpec Spec(IAudioCapture capture, string identity) =>
        new(capture, new TapConnectionOptions { Identity = identity, Name = identity });

    [Fact]
    public void StartAll_WhenEveryDeviceFailsToStart_ThrowsInsteadOfReturningADeadMeeting()
    {
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat) { ThrowOnStart = true };
        var system = new FakeAudioCapture(RecorderFormat) { ThrowOnStart = true };
        var failures = new List<string>();

        Assert.Throws<InvalidOperationException>(() => CaptureOrchestrator.StartAll(
            [Spec(mic, "mic"), Spec(system, "system")],
            onConnected: _ => { },
            onFailed: (id, _) => failures.Add(id),
            FastGate(), FastStream(), transport.Create));

        // The refusal sits ON TOP of the per-device best-effort path, not instead of it:
        // each device still surfaced its own failure (the shell balloons them) and each
        // capture was still released.
        Assert.Equal(["mic", "system"], failures);
        Assert.True(mic.Disposed);
        Assert.True(system.Disposed);
    }
}
