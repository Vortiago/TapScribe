using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// End-to-end coverage for the End-meeting flow: drives the REAL
/// <see cref="ControlClient"/> over loopback against a scripted
/// <see cref="FakeRecorderServer"/> (real HTTP, real JSON, real poll loop). Each
/// test maps to an acceptance criterion of issue #107. The poll cadence is
/// collapsed (<see cref="Immediate"/>) so the loop spins and the scripted server
/// advances state without real waits.
/// </summary>
public class MeetingControllerTests
{
    private static Task Immediate(CancellationToken _) => Task.CompletedTask;

    // Poll bodies as the Recorder serialises them (app.py api_tap_pipeline_poll).
    private static (int, string) Running(string stage, string status, int current = 0, int total = 0, string? file = null) =>
        (200, $"{{\"ok\":true,\"state\":\"running\",\"stage\":\"{stage}\",\"status\":\"{status}\"," +
              $"\"current\":{current},\"total\":{total},\"current_file\":{(file is null ? "null" : $"\"{file}\"")}}}");

    private static (int, string) Done(string summary) =>
        (200, $"{{\"ok\":true,\"state\":\"done\",\"summary\":{{\"summary\":\"{summary}\"}}}}");

    private static (int, string) Failed(string stage, string kind, string error) =>
        (200, $"{{\"ok\":true,\"state\":\"failed\",\"stage\":\"{stage}\",\"error\":\"{error}\",\"error_kind\":\"{kind}\"}}");

    private static MeetingController EndController(
        FakeRecorderServer server, HttpClient http, List<PipelineView> views, Func<Task>? drainAsync = null)
    {
        var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
        var controller = new MeetingController(control, "meet1", Immediate, drainAsync ?? (() => Task.CompletedTask));
        controller.Updated += views.Add;
        return controller;
    }

    [Fact]
    public async Task EndMeeting_DrainsTapsBeforeTriggering_AndEmitsTheFinishedSummary()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202,
            pollScript: [Running("strip", "stripping"), Done("decided to ship")]);

        var sync = new object();
        var events = new List<string>();
        void Log(string s) { lock (sync) events.Add(s); }
        server.OnTrigger = () => Log("triggered");

        using var http = new HttpClient();
        using var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);

        var views = new List<PipelineView>();
        var controller = new MeetingController(
            control, "meet1",
            pollDelay: Immediate,
            drainAsync: async () => { await Task.Yield(); Log("drained"); });
        controller.Updated += v => { lock (sync) views.Add(v); };

        await controller.EndAsync();

        // The taps were drained (gate close + Drain) BEFORE the pipeline was triggered.
        Assert.Equal("drained", events[0]);
        Assert.Equal("triggered", events[1]);

        // The finished summary is the terminal view the tray shows.
        PipelineView last = views[^1];
        Assert.Equal(PipelinePhase.Done, last.Phase);
        Assert.Equal("decided to ship", last.SummaryText);
    }

    [Fact]
    public async Task EndMeeting_EmitsPerStageProgress_WhilePolling()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202,
            pollScript:
            [
                Running("strip", "stripping"),
                Running("transcribe", "transcribing", 1, 3),
                Running("transcribe", "transcribing", 3, 3),
                Running("summarize", "summarizing"),
                Done("ok"),
            ]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();

        await EndController(server, http, views).EndAsync();

        // First, while taps drain, the card shows the pre-trigger "ending" phase.
        Assert.Equal(PipelinePhase.Ending, views[0].Phase);
        // Then a live progress line per stage, in order.
        List<string?> progress = views.Where(v => v.Phase == PipelinePhase.Running).Select(v => v.Progress).ToList();
        Assert.Equal(
            ["Stripping silence…", "Transcribing 1/3…", "Transcribing 3/3…", "Summarizing…"],
            progress);
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
    }

    [Fact]
    public async Task EndMeeting_AFailedStage_SurfacesTheStageAndDomainError_ThenStops()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202,
            pollScript: [Running("strip", "stripping"), Failed("transcribe", "NoUsableWavs", "boom")]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();

        await EndController(server, http, views).EndAsync();

        PipelineView last = views[^1];
        Assert.Equal(PipelinePhase.Failed, last.Phase);
        Assert.Equal("transcribe", last.FailureStage);
        Assert.Contains("No usable audio", last.FailureReason, StringComparison.Ordinal);
    }

    [Fact]
    public async Task EndMeeting_WhenSessionBusy_SurfacesBusy_ButStillPollsToTheSummary()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 409, // another job already in flight on this session
            pollScript: [Running("strip", "stripping"), Done("decided to ship")]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();
        var notices = new List<string>();

        MeetingController controller = EndController(server, http, views);
        controller.OperatorNotice += notices.Add;

        await controller.EndAsync();

        Assert.Equal(1, server.TriggerCount); // one POST, which got the 409 — not retried
        Assert.Contains(notices, n => n.Contains("busy", StringComparison.OrdinalIgnoreCase));
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
        Assert.Equal("decided to ship", views[^1].SummaryText);
    }

    [Fact]
    public async Task EndMeeting_IsSingleUse_ASecondClickFiresNoSecondPipeline()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202, pollScript: [Done("ok")]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();
        int drainCount = 0;

        MeetingController controller =
            EndController(server, http, views, drainAsync: () => { Interlocked.Increment(ref drainCount); return Task.CompletedTask; });

        // Two near-simultaneous clicks.
        await Task.WhenAll(controller.EndAsync(), controller.EndAsync());

        Assert.Equal(1, server.TriggerCount);
        Assert.Equal(1, drainCount);
    }

    [Fact]
    public async Task Resume_PollsAPersistedSession_WithoutDrainingOrRetriggering()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202,
            pollScript: [Running("summarize", "summarizing"), Done("decided to ship")]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();
        int drainCount = 0;

        using var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
        var controller = new MeetingController(
            control, "meet1", Immediate, drainAsync: () => { Interlocked.Increment(ref drainCount); return Task.CompletedTask; });
        controller.Updated += views.Add;

        await controller.ResumeAsync();

        Assert.Equal(0, server.NewSessionCount);
        Assert.Equal(0, server.TriggerCount); // resume never re-triggers
        Assert.Equal(0, drainCount);          // resume never re-drains
        Assert.Equal(PipelinePhase.Done, views[^1].Phase);
        Assert.Equal("decided to ship", views[^1].SummaryText);
    }

    [Fact]
    public async Task Resume_AGoneSession_404_SurfacesUnavailable_AndStopsPolling()
    {
        // Re-opening a past meeting (#168) whose session the Recorder has pruned: the poll
        // 404s. Unlike a transient blip (5xx / connection refused), a 404 means the Recorder
        // is UP and disowns the session — terminal, so the loop must STOP, not self-heal.
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202,
            pollScript: [(404, "{\"detail\":\"session not found\"}")]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();
        using var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
        var controller = new MeetingController(control, "meet-gone", Immediate);
        controller.Updated += views.Add;

        // Safety bound: if the loop wrongly RETRIES the 404 (the pre-fix behaviour), this
        // cancels it so the test fails fast instead of spinning forever.
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));

        await controller.ResumeAsync(cts.Token);

        Assert.Equal(PipelinePhase.Failed, views[^1].Phase);
        Assert.Contains("no longer available", views[^1].FailureReason!, StringComparison.OrdinalIgnoreCase);
        Assert.Equal(1, server.PollCount); // terminal — not retried like a 500 blip
    }

    [Fact]
    public async Task EndMeeting_ATransientPollFailure_SelfHeals_AndKeepsPolling()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            triggerStatus: 202,
            pollScript: [Running("strip", "stripping"), (500, "{}"), Done("decided to ship")]);
        using var http = new HttpClient();
        var views = new List<PipelineView>();

        await EndController(server, http, views).EndAsync();

        Assert.Equal(PipelinePhase.Done, views[^1].Phase); // the 500 blip didn't abort the loop
        Assert.True(server.PollCount >= 3);
    }
}
