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
    public async Task QuitAsync_MidAttachedTap_ClosesEveryPipelineBeforeReleasingTheShell()
    {
        // The attached twin of the meeting case below. An attached tap holds the same
        // orchestrator over the same devices, so a teardown that only knows about meetings
        // leaves a room mic streaming into the Recorder with no tray left to stop it.
        using var harness = new RuntimeHarness();
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        FakeAudioCapture system = harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);
        Assert.True(harness.View.CanDisconnect, "nothing was attached, so this proves nothing");

        await runtime.QuitAsync();

        Assert.True(mic.Disposed);
        Assert.True(system.Disposed);
        Assert.True(harness.Enumerator.Disposed, "the device enumerator was stranded at quit");
        Assert.True(harness.View.ShutdownCalled, "the shell was never told teardown had finished");
        Assert.True(harness.View.CapturesReleasedBeforeShutdown);
    }

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
    public async Task QuitAsync_WhenTheTeardownOutlivesItsCap_StillLetsTheShellGo()
    {
        // The cap is a BACKSTOP, so overrunning it is an expected outcome rather than an error:
        // a device that hangs closing, or a drain waiting on a Recorder that accepted the
        // connection and went quiet.
        //
        // Enforcing the cap with Task.WaitAsync turns that expected outcome into a
        // TimeoutException, which skips the Shutdown at the end of the teardown. The shell's
        // Quit is a fire-and-forget click handler with nothing to report that, and _quitting is
        // already latched, so the operator is left with a tray that refuses to start a meeting
        // AND never exits.
        using var hold = new ManualResetEventSlim(false);
        var mic = new FakeAudioCapture(Fixtures.RecorderFormat) { DisposeHold = hold };
        using var harness = new RuntimeHarness
        {
            Budgets = new RuntimeBudgets
            {
                PollInterval = TimeSpan.FromMilliseconds(10),
                StartSettleTimeout = TimeSpan.FromSeconds(5),
                QuitTeardownCap = TimeSpan.FromMilliseconds(50),
            },
        };
        harness.AddDevice("mic", DeviceFlow.Capture, mic);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);
        Assert.True(harness.View.CanEnd, "no meeting was running, so this proves nothing");
        // An open Utterance, so the session's teardown has a drain to await and the hung release
        // below lands on a continuation rather than on the quit's own thread. Without it the
        // whole teardown runs inline and the cap is never consulted.
        mic.Emit(Fixtures.Loud(40));

        try
        {
            await runtime.QuitAsync();

            // The claim: the shell is told it may go while the teardown is still out there.
            Assert.True(harness.View.ShutdownCalled, "an overrun teardown kept the shell alive forever");
            // ...and the anti-vacuity guard for it. The tail still closing behind the Shutdown
            // is exactly what "a sub-second tail may drop on a hard quit" means, and it is what
            // says the cap was reached at all rather than the teardown simply finishing in time.
            Assert.False(
                harness.View.CapturesReleasedBeforeShutdown,
                "the teardown finished inside the cap, so the overrun path was never taken");
        }
        finally
        {
            hold.Set(); // let the held teardown finish rather than leaving a pool thread parked
        }
    }

    [Fact]
    public async Task QuitAsync_WhenTheTeardownFaults_StillLetsTheShellGo()
    {
        // The other way the bounded teardown does not finish cleanly: a capture whose endpoint
        // was invalidated by the time its owner let go, which FAULTS the teardown rather than
        // overrunning it. Whether the taps closed cleanly is not the thing standing between the
        // operator and a shell that goes away, and the orchestrator releases what it can either
        // way.
        using var harness = new RuntimeHarness();
        harness.AddDevice(
            "mic", DeviceFlow.Capture,
            new FakeAudioCapture(Fixtures.RecorderFormat) { DetachError = new IOException("endpoint invalidated") });

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        await runtime.QuitAsync();

        Assert.True(harness.View.ShutdownCalled, "a faulted teardown kept the shell alive forever");
        Assert.True(harness.Enumerator.Disposed, "the enumerator was stranded by the failed teardown");
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
