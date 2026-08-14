using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Whole-flow regression tests for the meeting bracket, driven over real sockets
/// against the stateful <see cref="FakeRecorder"/>: real <see cref="CaptureOrchestrator"/>
/// + <see cref="TapClient"/> + <see cref="ControlClient"/> + <see cref="MeetingController"/>,
/// with NO faked drain. These cover the flows the tray exists to support — Start meeting →
/// stream → End meeting → summary — end to end, where the controller's own unit tests use
/// a scripted server and a closure drain.
/// </summary>
public class MeetingFlowTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);
    private static Task Immediate(CancellationToken _) => Task.CompletedTask;

    private static TapConnectionOptions Tap(int port, string identity, string session) => new()
    {
        Host = "127.0.0.1",
        Port = port,
        Identity = identity,
        Name = identity,
        Session = session,
        Token = "tok-abc",
    };

    private static ControlClient Control(FakeRecorder rec, HttpClient http) =>
        new("127.0.0.1", rec.Port, tls: false, token: "tok-abc", http);

    [Fact]
    public async Task FullMeeting_MintStreamTwoDevices_End_DrainsBothTaps_AndReceivesTheSummary()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);

        // Start meeting: mint a detached session and stream the mic + system loopback into it.
        string session = await control.CreateDetachedSessionAsync();
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([new PipelineSpec(mic, Tap(rec.Port, "mic", session)), new PipelineSpec(system, Tap(rec.Port, "system", session))]),
            onConnected: _ => { }, onFailed: (_, _) => { },
            gate: FastGate(), stream: FastStream());

        mic.Emit(Loud(40));
        system.Emit(Loud(40));
        await Poll.UntilAsync(
            () => rec.FramesFor(session, "mic") > 0 && rec.FramesFor(session, "system") > 0,
            Wait, "both devices to stream into the detached session");

        // End meeting: the REAL orchestrator Drain closes both taps, then the pipeline runs.
        var views = new List<PipelineView>();
        var controller = new MeetingController(
            control, session, pollDelay: Immediate, drainAsync: () => orchestrator.DisposeAsync().AsTask());
        controller.Updated += view => { lock (views) views.Add(view); };

        await controller.EndAsync();

        // The real Drain closed every open tap...
        await Poll.UntilAsync(() => rec.AllTapsClosed(session), Wait, "the real Drain to close both taps");
        // ...the pipeline ran exactly once on THIS session, showing progress and ending in the summary.
        Assert.Equal(1, rec.NewSessionCount);
        Assert.Equal(1, rec.TriggerCount(session));
        Assert.Contains(views, v => v.Phase == PipelinePhase.Running);
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
        Assert.Equal(FakeRecorder.SummaryFor(session), views[^1].SummaryText);
    }

    [Fact]
    public async Task FullMeeting_RecordOnly_StreamsAndDrains_ButLeavesTheSessionUnprocessed()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);

        // Start meeting: mint a detached session and stream the mic into it.
        string session = await control.CreateDetachedSessionAsync();
        var mic = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([new PipelineSpec(mic, Tap(rec.Port, "mic", session))]),
            onConnected: _ => { }, onFailed: (_, _) => { },
            gate: FastGate(), stream: FastStream());

        mic.Emit(Loud(40));
        await Poll.UntilAsync(
            () => rec.FramesFor(session, "mic") > 0, Wait, "the mic to stream into the detached session");

        // End meeting in RECORD-ONLY mode: the real Drain still closes the tap, but the
        // pipeline is never triggered — the recordings are simply left for later.
        var views = new List<PipelineView>();
        var controller = new MeetingController(
            control, session, pollDelay: Immediate, drainAsync: () => orchestrator.EndMeetingAsync());
        controller.Updated += view => { lock (views) views.Add(view); };

        await controller.EndAsync(triggerPipeline: false);

        await Poll.UntilAsync(() => rec.AllTapsClosed(session), Wait, "the real Drain to close the tap");
        Assert.Equal(1, rec.NewSessionCount);        // the session was minted...
        Assert.Equal(0, rec.TriggerCount(session));  // ...but no pipeline ever ran on it
        Assert.Equal(PipelinePhase.Saved, views[^1].Phase);
    }

    [Fact]
    public async Task FullMeeting_WalksEveryPipelineStageInOrder()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);
        const string session = "meet-stages";

        var views = new List<PipelineView>();
        var controller = new MeetingController(control, session, Immediate, drainAsync: () => Task.CompletedTask);
        controller.Updated += views.Add;

        await controller.EndAsync();

        // ending (taps draining) → the four stage lines from the recorder → done.
        Assert.Equal(PipelinePhase.Ending, views[0].Phase);
        List<string?> progress = views.Where(v => v.Phase == PipelinePhase.Running).Select(v => v.Progress).ToList();
        Assert.Equal(
            ["Stripping silence…", "Transcribing 1/2…", "Transcribing 2/2…", "Summarizing…"],
            progress);
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
        Assert.Equal(FakeRecorder.SummaryFor(session), views[^1].SummaryText);
    }

    [Fact]
    public async Task TwoConcurrentMeetings_EachReceivesItsOwnSessionsSummary_NeverCrossed()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);

        async Task<string> EndAndReadSummary(string session)
        {
            var views = new List<PipelineView>();
            var controller = new MeetingController(control, session, Immediate, drainAsync: () => Task.CompletedTask);
            controller.Updated += view => { lock (views) views.Add(view); };
            await controller.EndAsync();
            Assert.Equal(PipelinePhase.Done, views[^1].Phase);
            return views[^1].SummaryText!;
        }

        // Two meetings ended concurrently against the same Recorder.
        string[] summaries = await Task.WhenAll(EndAndReadSummary("meet-A"), EndAndReadSummary("meet-B"));

        // Each meeting got the summary persisted for ITS OWN session — not the other's.
        Assert.Equal(FakeRecorder.SummaryFor("meet-A"), summaries[0]);
        Assert.Equal(FakeRecorder.SummaryFor("meet-B"), summaries[1]);
        Assert.NotEqual(summaries[0], summaries[1]);
    }

    [Fact]
    public async Task EndMeeting_WhenAJobAlreadyRunsOnTheSession_GetsARealConflict_ButStillReceivesTheSummary()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);
        const string session = "meet-busy";

        // A job is already in flight on this session (e.g. a dashboard transcribe).
        Assert.Equal(PipelineTriggerOutcome.Accepted, await control.TriggerPipelineAsync(session));

        var views = new List<PipelineView>();
        var notices = new List<string>();
        var controller = new MeetingController(control, session, Immediate, drainAsync: () => Task.CompletedTask);
        controller.Updated += views.Add;
        controller.OperatorNotice += notices.Add;

        await controller.EndAsync();

        Assert.Equal(2, rec.TriggerCount(session)); // the prior job + End's attempt, which got the real 409
        Assert.Contains(notices, n => n.Contains("busy", StringComparison.OrdinalIgnoreCase));
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
        Assert.Equal(FakeRecorder.SummaryFor(session), views[^1].SummaryText);
    }

    [Fact]
    public async Task Resume_RidesARunningPipelineToTheSummary_WithoutReTriggering()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);
        const string session = "meet-resume";

        // A pipeline is running on the recorder; the tray restarts and resumes it.
        Assert.Equal(PipelineTriggerOutcome.Accepted, await control.TriggerPipelineAsync(session));

        var views = new List<PipelineView>();
        var controller = new MeetingController(control, session, Immediate);
        controller.Updated += views.Add;

        await controller.ResumeAsync();

        Assert.Equal(1, rec.TriggerCount(session)); // resume NEVER re-triggers (a 2nd would 409)
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
        Assert.Equal(FakeRecorder.SummaryFor(session), views[^1].SummaryText);
    }

    [Fact]
    public async Task PastMeetings_RecordTwo_ThenReReadEachOwnSummary_NeverCrossed()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);

        // End two meetings; each is appended to the tray's LOCAL history at End time
        // (the same moment the active-resume state is persisted in the real tray).
        MeetingHistory history = MeetingHistory.Empty;
        foreach (string session in new[] { "meet-A", "meet-B" })
        {
            var controller = new MeetingController(control, session, Immediate, drainAsync: () => Task.CompletedTask);
            await controller.EndAsync();
            history = history.Append(new MeetingRecord { SessionId = session, StartedAt = DateTimeOffset.UnixEpoch });
        }

        // The Past-meetings list is newest-first.
        Assert.Equal(["meet-B", "meet-A"], history.Meetings.Select(m => m.SessionId));

        // Re-open each past meeting via the SAME tap-token poll endpoint (no re-trigger):
        // each rides to ITS OWN session's persisted summary — never the other's.
        foreach (MeetingRecord record in history.Meetings)
        {
            var views = new List<PipelineView>();
            var reopen = new MeetingController(control, record.SessionId, Immediate);
            reopen.Updated += views.Add;

            await reopen.ResumeAsync();

            Assert.Equal(PipelinePhase.Done, views[^1].Phase);
            Assert.Equal(FakeRecorder.SummaryFor(record.SessionId), views[^1].SummaryText);
        }

        // Re-reads never re-fire a pipeline (a second trigger would 409); only the two Ends did.
        Assert.Equal(1, rec.TriggerCount("meet-A"));
        Assert.Equal(1, rec.TriggerCount("meet-B"));
    }

    [Fact]
    public async Task EndMeeting_AFailingStage_SurfacesItAndStops()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync(failTranscribe: true);
        using var http = new HttpClient();
        using ControlClient control = Control(rec, http);
        const string session = "meet-fail";

        var views = new List<PipelineView>();
        var controller = new MeetingController(control, session, Immediate, drainAsync: () => Task.CompletedTask);
        controller.Updated += views.Add;

        await controller.EndAsync();

        Assert.Equal(PipelinePhase.Failed, views[^1].Phase);
        Assert.Equal("transcribe", views[^1].FailureStage);
        Assert.Contains("No usable audio", views[^1].FailureReason, StringComparison.Ordinal);
    }
}
