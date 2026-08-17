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
            Resumer(server, http, "meet-past"), view, new InlineDispatcher(), CancellationToken.None);

        // The view rode progress to the terminal summary — never re-triggering a pipeline.
        Assert.Equal(PipelinePhase.Done, view.Last!.Phase);
        Assert.Equal("decided to ship", view.Last.SummaryText);
        Assert.Equal(0, server.TriggerCount);
    }

    [Fact]
    public async Task ClosingTheWindowMidPoll_StopsCleanly_WithoutRenderingAFailure()
    {
        // The other half of the past-meeting lifetime (B9): the operator closes the window
        // while the poll is still riding a running pipeline. The shell cancels the token on
        // close, and the loop must stop QUIETLY — a "couldn't reach the recorder" splashed
        // into a window the user just closed is a lie about a deliberate act. This is also
        // the only observable the tray's never-dispose-the-source rule protects: the loop
        // holds this token until it returns here, so anything that invalidated it earlier
        // would surface as a failure render instead of a clean stop.
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [Running("strip")]); // the last entry repeats: never terminal
        using var http = new HttpClient();
        var view = new FakeView();
        using var closing = new CancellationTokenSource();

        // Cancel the moment the first poll has been rendered — the window closing mid-flight.
        var cancelOnFirstRender = new InlineDispatcher(() => closing.Cancel());

        await MeetingViewDriver.DriveAsync(
            Resumer(server, http, "meet-open"), view, cancelOnFirstRender, closing.Token);

        Assert.True(view.RenderCount > 0, "nothing was ever rendered, so this proves nothing");
        Assert.NotEqual(PipelinePhase.Failed, view.Last!.Phase);
    }

    [Fact]
    public async Task OpeningAPrunedPastMeeting_404_ShowsNoLongerAvailable_InTheView()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [(404, "{\"detail\":\"session not found\"}")]);
        using var http = new HttpClient();
        var view = new FakeView();

        await MeetingViewDriver.DriveAsync(
            Resumer(server, http, "meet-gone"), view, new InlineDispatcher(), CancellationToken.None);

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
            Resumer(server, http, "meet-progress"), view, new InlineDispatcher(), CancellationToken.None);

        Assert.True(view.RenderCount >= 2); // at least one running view before the done view
        Assert.Equal(PipelinePhase.Done, view.Last!.Phase);
    }
}
