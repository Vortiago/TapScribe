using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// End-to-end coverage for opening a past meeting (#168): the real
/// <see cref="MeetingViewDriver"/> + <see cref="MeetingController"/> + <see cref="ControlClient"/>
/// over loopback HTTP against the stateful <see cref="FakeRecorderServer"/>, driving a fake
/// <see cref="IMeetingView"/>. This is the WinForms <c>MeetingForm</c> open flow with only the
/// widget abstracted away, so the integration — poll → render-marshaling → terminal summary,
/// and the 404 "gone session" path — is exercised on every OS (the windows job AND the ubuntu
/// core job), no desktop required.
/// </summary>
public class MeetingViewDriverTests
{
    // Renders are posted through a SynchronizationContext; running them inline keeps the test
    // deterministic — every Render has happened by the time DriveAsync returns.
    private sealed class InlineSyncContext : SynchronizationContext
    {
        public override void Post(SendOrPostCallback d, object? state) => d(state);
    }

    private sealed class FakeView : IMeetingView
    {
        private readonly List<PipelineView?> _rendered = [];
        public bool IsDisposed => false;
        public void Render(PipelineView? view) { lock (_rendered) _rendered.Add(view); }
        public PipelineView? Last { get { lock (_rendered) return _rendered[^1]; } }
        public int RenderCount { get { lock (_rendered) return _rendered.Count; } }
    }

    private static (int, string) Running(string stage) =>
        (200, $"{{\"ok\":true,\"state\":\"running\",\"stage\":\"{stage}\",\"status\":\"x\"," +
              "\"current\":0,\"total\":0,\"current_file\":null}");

    private static (int, string) Done(string summary) =>
        (200, $"{{\"ok\":true,\"state\":\"done\",\"summary\":{{\"summary\":\"{summary}\"}}}}");

    private static MeetingController Resumer(FakeRecorderServer server, HttpClient http, string session)
    {
        var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
        return new MeetingController(control, session, pollDelay: _ => Task.CompletedTask);
    }

    [Fact]
    public async Task OpeningAPastMeeting_RidesThePollToTheSummary_InTheView()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [Running("strip"), Done("decided to ship")]);
        using var http = new HttpClient();
        var view = new FakeView();

        await MeetingViewDriver.DriveAsync(
            Resumer(server, http, "meet-past"), view, new InlineSyncContext(), CancellationToken.None);

        // The view rode progress to the terminal summary — never re-triggering a pipeline.
        Assert.Equal(PipelinePhase.Done, view.Last!.Phase);
        Assert.Equal("decided to ship", view.Last.SummaryText);
        Assert.Equal(0, server.TriggerCount);
    }

    [Fact]
    public async Task OpeningAPrunedPastMeeting_404_ShowsNoLongerAvailable_InTheView()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [(404, "{\"detail\":\"session not found\"}")]);
        using var http = new HttpClient();
        var view = new FakeView();

        await MeetingViewDriver.DriveAsync(
            Resumer(server, http, "meet-gone"), view, new InlineSyncContext(), CancellationToken.None);

        Assert.Equal(PipelinePhase.Failed, view.Last!.Phase);
        Assert.Contains("no longer available", view.Last.FailureReason!, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task OpeningAPastMeeting_RendersProgressBeforeTheSummary()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [Running("strip"), Running("summarize"), Done("ok")]);
        using var http = new HttpClient();
        var view = new FakeView();

        await MeetingViewDriver.DriveAsync(
            Resumer(server, http, "meet-progress"), view, new InlineSyncContext(), CancellationToken.None);

        Assert.True(view.RenderCount >= 2); // at least one running view before the done view
        Assert.Equal(PipelinePhase.Done, view.Last!.Phase);
    }
}
