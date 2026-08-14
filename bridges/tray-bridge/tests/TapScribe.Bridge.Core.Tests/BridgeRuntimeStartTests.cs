namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The meeting lifecycle, hoisted out of the WinForms shell into Core (issue #419, slice 0B).
/// Start resolves the operator's device selection against the devices present now, mints a
/// detached session, and runs one pipeline per resolved device: reporting everything the
/// operator would see through <see cref="ITrayView"/>.
///
/// Driven here with no WinForms, no WASAPI endpoint and no Recorder, which is the whole point
/// of the extraction: the AppKit shell inherits this behaviour with this cover, instead of
/// re-implementing an untestable copy of it.
/// </summary>
public class BridgeRuntimeStartTests
{
    [Fact]
    public async Task Start_WithTwoResolvableDevices_PublishesAStreamingMeeting()
    {
        using var harness = new RuntimeHarness();
        harness.AddDevice("mic", DeviceFlow.Capture);
        harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // The meeting is live: End is the only move left, and the header names both devices.
        // Zero CONNECTED is expected and is not the subject: the harness points at a port
        // nothing listens on, so the taps are streaming into a refused connection while the
        // core retries. What Start owns is that two pipelines exist and the meeting was
        // published; whether a tap reached the Recorder is TapStream's own cover.
        Assert.Equal(StatusView.For(new TrayStatus.Streaming(0, 2)), harness.View.LastStatus);
        Assert.False(harness.View.CanStart, "Start stayed enabled over a live meeting");
        Assert.True(harness.View.CanEnd, "the meeting was never published as running");
    }
}
