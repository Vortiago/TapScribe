using TapScribe.Bridge.Core;
using TapScribe.Bridge.Core.Tests;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// The REAL end-to-end past-meeting open (#168) on Windows: a real <see cref="MeetingForm"/>
/// driven by a real <see cref="MeetingController"/> over loopback HTTP against the in-process
/// <see cref="FakeRecorderServer"/>, marshalled through <see cref="MeetingWindowDriver"/> — the
/// exact component the tray's OpenPastMeeting flow uses. No tray shell, no NotifyIcon. Asserts
/// the window rides the poll to its summary, and degrades to "no longer available" for a session
/// the Recorder has pruned (a 404). Runs on the windows dotnet-build CI job.
/// </summary>
public class PastMeetingWindowTests
{
    private static (int, string) Running(string stage) =>
        (200, $"{{\"ok\":true,\"state\":\"running\",\"stage\":\"{stage}\",\"status\":\"x\"," +
              "\"current\":0,\"total\":0,\"current_file\":null}");

    private static (int, string) Done(string summary) =>
        (200, $"{{\"ok\":true,\"state\":\"done\",\"summary\":{{\"summary\":\"{summary}\"}}}}");

    [Fact]
    public void OpeningAPastMeeting_RidesThePollToTheSummary_InTheWindow() => Sta.Run(() =>
    {
        FakeRecorderServer server = FakeRecorderServer.StartAsync(
            pollScript: [Running("strip"), Done("decided to ship")]).GetAwaiter().GetResult();
        try
        {
            using var http = new HttpClient();
            using var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
            var controller = new MeetingController(control, "meet-past", pollDelay: _ => Task.CompletedTask);
            using var form = new MeetingForm();
            var ui = new DrainableSyncContext();

            Task drive = MeetingWindowDriver.DriveAsync(controller, form, ui, CancellationToken.None);
            ui.PumpUntil(() => drive.IsCompleted, TimeSpan.FromSeconds(15));

            Assert.True(drive.IsCompleted, "the driver did not ride the poll to a terminal state in time");
            drive.GetAwaiter().GetResult(); // observe any driver exception
            Assert.Equal("decided to ship", form.CurrentBodyText());
            Assert.True(form.CopyEnabled());
        }
        finally
        {
            server.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
    });

    [Fact]
    public void OpeningAPrunedPastMeeting_404_ShowsNoLongerAvailable_InTheWindow() => Sta.Run(() =>
    {
        FakeRecorderServer server = FakeRecorderServer.StartAsync(
            pollScript: [(404, "{\"detail\":\"session not found\"}")]).GetAwaiter().GetResult();
        try
        {
            using var http = new HttpClient();
            using var control = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);
            var controller = new MeetingController(control, "meet-gone", pollDelay: _ => Task.CompletedTask);
            using var form = new MeetingForm();
            var ui = new DrainableSyncContext();

            Task drive = MeetingWindowDriver.DriveAsync(controller, form, ui, CancellationToken.None);
            ui.PumpUntil(() => drive.IsCompleted, TimeSpan.FromSeconds(15));

            Assert.True(drive.IsCompleted, "the driver did not surface the 404 terminal state in time");
            drive.GetAwaiter().GetResult();
            Assert.Contains("no longer available", form.CurrentBodyText(), StringComparison.OrdinalIgnoreCase);
            Assert.False(form.CopyEnabled());
        }
        finally
        {
            server.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
    });
}
