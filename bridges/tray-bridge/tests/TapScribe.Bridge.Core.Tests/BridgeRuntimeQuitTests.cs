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

        // ...and it arrives MARSHALLED, like every other view call. Both awaits above are
        // ConfigureAwait(false), so calling Shutdown straight through delivers it on a
        // thread-pool thread: AppKit's teardown is main-thread-only and WinForms has to end its
        // message loop from the thread that owns it, so neither shell can absorb that.
        Assert.True(harness.View.ShutdownWasMarshalled, "Shutdown bypassed the dispatcher");
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

    [Fact]
    public async Task QuitAsync_CancelsTheInFlightEndFlow_SoItStopsTalkingToTheRecorder()
    {
        // The End flow's poll loop used to run on CancellationToken.None: quitting ended the
        // shell and left it polling the Recorder and rendering into a view that was gone, for as
        // long as the process lingered. The script never reaches a terminal state, so the loop
        // would poll forever; the cancellation is what has to stop it.
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript:
            [
                (200, "{\"ok\":true,\"state\":\"running\",\"stage\":\"strip\",\"status\":\"x\"," +
                      "\"current\":0,\"total\":0,\"current_file\":null}"),
            ]);
        using var harness = new RuntimeHarness
        {
            Settings = new BridgeSettings
            {
                Host = "127.0.0.1",
                Port = server.Port,
                Identity = "alice",
                Name = "Alice",
                Token = "tok-abc",
                Devices = [],
            },
        };
        harness.AddDevice("mic", DeviceFlow.Capture);
        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        runtime.End();
        // The anti-vacuity guard: the loop really was running when the quit arrived.
        await Poll.UntilAsync(
            () => server.PollCount > 0, TimeSpan.FromSeconds(10), "the pipeline to start polling");

        await runtime.QuitAsync();

        // The claim, with no clock in it: the flow TERMINATES. Against a script that never
        // reaches a terminal state an uncancelled loop polls this server for as long as the
        // process lives, so settling at all is the observable, and the bounded wait inside
        // EndSettledAsync is what fails if the token never reached the controller. Counting
        // polls after the fact would be a race instead: a request already on the wire still
        // reaches the server after the task it belonged to has been cancelled.
        await RuntimeHarness.EndSettledAsync(runtime);
    }
}
