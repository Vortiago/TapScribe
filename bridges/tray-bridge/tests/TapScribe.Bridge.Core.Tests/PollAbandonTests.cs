using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins B4 — a poll loop that never gives up leaves the tray with no way out but Quit.
/// <see cref="MeetingController"/> treats every non-404 poll failure as a transient blip
/// and self-heals at the poll cadence, forever. Against a Recorder that keeps answering
/// 500 (a wedged job, a half-broken deploy) End meeting and Resume therefore never
/// terminate, and the shell greys BOTH menu items for the whole flow — so the meeting
/// can neither be ended nor restarted, and the header sits on "● Ending meeting…" for as
/// long as the tray runs.
///
/// Self-healing across a blip is right; self-healing forever is not. The loop bounds the
/// run of CONSECUTIVE failures and then emits a terminal view, which is what returns the
/// menu to the operator. Asserted causally — a failure COUNT and a terminal view, never a
/// wall-clock give-up time — so the pin holds on slow CI.
/// </summary>
public class PollAbandonTests
{
    private static Task Immediate(CancellationToken _) => Task.CompletedTask;

    [Fact]
    public async Task Resume_WhenEveryPollKeepsFailing_GivesUpWithATerminalView()
    {
        // The script's last entry repeats, so this Recorder answers 500 forever.
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [(500, "{}")]);
        using var http = new HttpClient();
        using var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
        var views = new List<PipelineView>();
        var controller = new MeetingController(control, "meet-wedged", Immediate);
        controller.Updated += views.Add;

        // Safety bound: the pre-fix loop never returns, so this fails the test fast
        // instead of hanging the suite. The fix must terminate WITHOUT it.
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20));

        await controller.ResumeAsync(cts.Token);

        Assert.False(cts.IsCancellationRequested, "the loop only stopped because the safety bound cancelled it");
        Assert.Equal(PipelinePhase.Failed, views[^1].Phase); // terminal: the shell re-enables the menu on this
        Assert.Contains("recorder", views[^1].FailureReason!, StringComparison.OrdinalIgnoreCase);
        Assert.Equal(MeetingController.MaxConsecutivePollFailures, server.PollCount);
    }
}
