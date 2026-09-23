namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Start meeting while an attached tap is streaming: a TAKEOVER, not a refusal (ADR-0025).
///
/// One device is one speaker, so the two modes are exclusive — an identity feeding two sessions
/// at once would split that speaker across them. An operator who clicks Start while a room mic
/// feeds the current session means they want the meeting, so the tray gives them one.
/// </summary>
public class BridgeRuntimeTakeoverTests
{
    [Fact]
    public async Task Start_WhileAttached_DrainsTheAttachedTapsBeforeItMints()
    {
        // The ORDER is the claim. Draining first flushes the attached taps' last Utterance into
        // the session it was recorded in; minting first would leave that WAV to be cut off by a
        // teardown racing the new meeting's first frames, and the Recorder would strip and
        // transcribe the truncated file.
        using var harness = new RuntimeHarness { HoldMint = true };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);
        Assert.False(mic.Disposed, "nothing was attached, so this test would prove nothing");

        runtime.Start();
        await harness.MintReached.WaitAsync(TimeSpan.FromSeconds(30));

        Assert.True(mic.Disposed, "the mint began while the attached taps were still open");

        harness.CompleteMint();
        await RuntimeHarness.StartSettledAsync(runtime);
        Assert.Equal(TrayCommands.MeetingRunning, harness.View.Commands);
    }

    [Fact]
    public async Task Start_WhileAttached_WithASelectionThatCannotResolve_LeavesTheAttachedTapStreaming()
    {
        // The drain is not undoable and the verdict is pure, so the verdict goes FIRST: a Start
        // the selection alone rules out says so without ending a working attached tap on the
        // way — and leaves the operator the attached tap's commands, not an idle menu with no
        // way left to disconnect what is still streaming.
        using var harness = new RuntimeHarness();
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);
        Assert.Equal(TrayCommands.Attached, harness.View.Commands);

        // Two devices under one identity: the resolve refuses it, before anything opens.
        harness.Settings.Devices =
        [
            new DeviceSelection.FollowDefault(DeviceFlow.Capture, "everyone", "everyone"),
            new DeviceSelection.FollowDefault(DeviceFlow.Render, "everyone", "everyone"),
        ];
        int before = harness.View.Notices.Count;
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        Assert.False(mic.Disposed, "a Start the selection refused drained the attached tap anyway");
        Assert.Null(harness.SessionIdInUse);
        Assert.Equal(TrayCommands.Attached, harness.View.Commands);
        (string title, string message, _) = Assert.Single(harness.View.Notices.Skip(before));
        Assert.Equal("Could not start meeting", title);
        Assert.Contains("identity", message, StringComparison.OrdinalIgnoreCase);

        await runtime.QuitAsync();
    }

    [Fact]
    public async Task End_AfterATakeover_ReturnsToIdleRatherThanToAttached()
    {
        // Attached is not a state the tray falls back to. The operator connected, then chose a
        // meeting; ending that meeting means they are done, not that a room mic should quietly
        // start feeding the current session again.
        using var harness = new RuntimeHarness { Settings = RuntimeHarness.NoProcessOnEnd() };
        harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        runtime.End();
        await RuntimeHarness.EndSettledAsync(runtime);

        Assert.Equal(TrayCommands.Idle, harness.View.Commands);
    }

    [Fact]
    public async Task Connect_AfterAMeetingHasEnded_IsOfferedAgain()
    {
        // The other direction of the same rule: idle offers both modes, so a tray that has run
        // a meeting is not stuck without Connect.
        using var harness = new RuntimeHarness { Settings = RuntimeHarness.NoProcessOnEnd() };
        harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);
        runtime.End();
        await RuntimeHarness.EndSettledAsync(runtime);

        Assert.True(harness.View.CanConnect);

        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);

        Assert.Equal(TrayCommands.Attached, harness.View.Commands);
    }
}
