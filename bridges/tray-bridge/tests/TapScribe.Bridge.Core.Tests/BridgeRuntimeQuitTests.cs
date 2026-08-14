namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Quit, as an AWAITABLE teardown. The WinForms shell could get away with blocking its UI
/// thread twice (once for a start still in flight, once for the pipelines to close), because
/// every await in it used ConfigureAwait(false) and so no continuation needed that thread to
/// progress: an unstated, unenforced, whole-file invariant.
///
/// On AppKit the main thread IS the run loop, blocking it for up to two budgets is a beachball
/// and a watchdog risk, and there is no ConfigureAwait(false) analogue because the problem is
/// occupying the main queue rather than capturing a context. So teardown is async, and the
/// view is told twice: the shell stops offering commands, then releases its UI once nothing is
/// streaming any more.
/// </summary>
public class BridgeRuntimeQuitTests
{
    [Fact]
    public async Task QuitAsync_MidMeeting_ClosesEveryPipelineBeforeReleasingTheShell()
    {
        using var harness = new RuntimeHarness();
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        FakeAudioCapture system = harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);
        Assert.True(harness.View.CanEnd, "no meeting was running, so this proves nothing");

        await runtime.QuitAsync();

        // Nothing is left streaming: the captures are stopped and released, and the enumerator
        // that handed them their endpoints outlives them.
        Assert.True(mic.Disposed);
        Assert.True(system.Disposed);
        Assert.True(harness.Enumerator.Disposed, "the device enumerator was stranded at quit");

        // The shell is told to release its UI, and only after the teardown above: releasing
        // first would leave the pipelines posting into a view that is already gone.
        Assert.True(harness.View.ShutdownCalled, "the shell was never told teardown had finished");
        Assert.True(harness.View.CapturesReleasedBeforeShutdown);
    }

    [Fact]
    public async Task QuitAsync_WhileAStartIsStillMinting_TearsThatMeetingDownInsteadOfPublishingIt()
    {
        using var harness = new RuntimeHarness { HoldMint = true };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await harness.MintReached; // the start is parked mid-pre-flight, with nothing published yet

        // Quit claims the shell first, then releases the mint so the start runs on into a
        // shell that is already gone. Publishing there would hand the meeting to nobody:
        // no one would ever dispose it, the captures would keep streaming, and the detached
        // session would stay open on the Recorder until the process died.
        Task quit = runtime.QuitAsync();
        harness.CompleteMint();
        await quit;

        await Poll.UntilAsync(() => mic.Disposed, TimeSpan.FromSeconds(10), "the abandoned start to release its capture");
        Assert.True(harness.Enumerator.Disposed);
        Assert.False(harness.View.CanEnd, "an abandoned start published a meeting into a dead shell");
    }
}
