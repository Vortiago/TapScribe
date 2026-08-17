namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Restart-resume (issue #107): a tray that was quit mid-pipeline comes back and picks the
/// in-flight meeting up where it left off, because the Recorder kept it running across both
/// restarts. Resume POLLS ONLY: no drain, no re-trigger, so re-launching cannot fire a second
/// pipeline over a session that is already being processed.
///
/// The entry point is <see cref="BridgeRuntime.Startup"/> rather than anything the constructor
/// does, because a shell has no UI thread to marshal onto until its event loop is pumping.
/// </summary>
public class BridgeRuntimeResumeTests
{
    [Fact]
    public async Task Startup_WithAPersistedMeeting_RidesItToTheSummaryWithoutRetriggering()
    {
        await using FakeRecorder recorder = await FakeRecorder.StartAsync();
        using var harness = new RuntimeHarness
        {
            Recorder = recorder,
            Settings = RuntimeHarness.RecorderSettings(recorder),
        };

        // A meeting a previous tray session left behind: the Recorder is still processing it.
        using var control = new ControlClient("127.0.0.1", recorder.Port, tls: false, token: "tok-abc");
        string session = await control.CreateDetachedSessionAsync();
        harness.StateStore.Save(new MeetingState { SessionId = session });

        BridgeRuntime runtime = harness.Build();
        runtime.Startup();
        await RuntimeHarness.EndSettledAsync(runtime);

        // Rode it to the finished summary...
        Assert.Equal(StatusView.For(new TrayStatus.SummaryReady()), harness.View.LastStatus);
        FakeMeetingWindow window = Assert.Single(harness.View.Windows);
        Assert.Equal(FakeRecorder.SummaryFor(session), window.Last!.SummaryText);

        // ...without firing a second pipeline over a session already being processed, which is
        // the whole reason Resume polls rather than re-running End.
        Assert.Equal(0, recorder.TriggerCount(session));

        // The resume state is cleared once the flow reaches a terminal phase, so the NEXT
        // launch is a fresh one rather than an endless re-resume of a finished meeting.
        Assert.Null(harness.StateStore.Load());
    }

    [Fact]
    public void Startup_WithNoPersistedMeeting_DoesNothing()
    {
        using var harness = new RuntimeHarness();
        BridgeRuntime runtime = harness.Build();

        runtime.Startup();

        // The common case: a fresh launch. Nothing to resume must mean nothing at all, not a
        // flow that briefly disables both commands on every start of the app.
        Assert.Null(runtime.EndTask);
        Assert.True(harness.View.CanStart);
        Assert.False(harness.View.CanEnd);
    }
}
