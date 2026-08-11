using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins B3 — the End-meeting teardown must RELEASE THE DEVICES even when a step of it
/// fails, and must not fault the caller for trying.
///
/// <see cref="TapSession.DisposeAsync"/> sequences <c>capture.Dispose()</c> AFTER
/// <c>capture.Stop()</c> with nothing in between, so a backend whose Stop throws (the
/// endpoint was invalidated mid-meeting — AUDCLNT_E_DEVICE_INVALIDATED) never reaches
/// the Dispose: the device is leaked for the process lifetime and the exception
/// propagates out through <see cref="CaptureOrchestrator.DisposeAsync"/>, whose callers
/// are documented to rely on it being throw-free (the tray's Quit blocks on it with
/// <c>.Wait()</c>, and its End path's drain callback disposes the device enumerator on
/// the line AFTER the await). The Windows backend happens to swallow the COM error
/// itself; the seam never promised that, and the sibling macOS shell (ADR-0020) would
/// have to rediscover it.
/// </summary>
public class TeardownFailureTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    [Fact]
    public async Task EndMeetingAsync_WhenStoppingAnInvalidatedDeviceThrows_StillReleasesEveryDevice()
    {
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat) { ThrowOnStop = true }; // invalidated mid-meeting
        var system = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic"), Spec(system, "system")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        mic.Emit(Loud(40));
        system.Emit(Loud(40));
        await Poll.UntilAsync(
            () => transport.HasStreamed("mic") && transport.HasStreamed("system"),
            Wait, "both pipelines to stream");

        // The barrier completes rather than faulting the caller: the tray's End path
        // disposes the device enumerator after this await, and Quit blocks on the same
        // teardown, so a throw here strands both.
        await orchestrator.EndMeetingAsync().WaitAsync(Wait);

        Assert.True(mic.Disposed, "the invalidated device was never released");
        Assert.True(system.Disposed, "a sibling device was taken down with the failing one");
    }
}
