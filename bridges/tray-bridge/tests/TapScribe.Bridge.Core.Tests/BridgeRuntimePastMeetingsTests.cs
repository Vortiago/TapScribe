using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Re-opening a past meeting (#168), as the runtime owns it: the menu asks for the history,
/// picks a record, and gets a window of its own that rides the session to its summary.
///
/// Two claims that are easy to lose in a shell. Opening last week's notes must not disturb a
/// LIVE meeting: not its status line, not the Start/End commands, not its taps. And closing
/// the window must stop the poll, which is what <see cref="IMeetingWindow.Closed"/> is for:
/// without a consumer the loop would keep talking to the Recorder for as long as the process
/// lives, rendering into a window nobody is looking at.
/// </summary>
public class BridgeRuntimePastMeetingsTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(30);

    private static (int, string) Running(string stage) =>
        (200, $"{{\"ok\":true,\"state\":\"running\",\"stage\":\"{stage}\",\"status\":\"x\"," +
              "\"current\":0,\"total\":0,\"current_file\":null}");

    private static (int, string) Done(string summary) =>
        (200, $"{{\"ok\":true,\"state\":\"done\",\"summary\":{{\"summary\":\"{summary}\"}}}}");

    /// <summary>Point the runtime at a scripted Recorder, so a past meeting really is polled
    /// over loopback HTTP through the runtime's own ControlClient.</summary>
    private static BridgeSettings Scripted(FakeRecorderServer server) => new()
    {
        Host = "127.0.0.1",
        Port = server.Port,
        Identity = "alice",
        Name = "Alice",
        Token = "tok-abc",
        Devices = [],
    };

    private static MeetingRecord Record(string sessionId) =>
        new() { SessionId = sessionId, StartedAt = DateTimeOffset.Now };

    [Fact]
    public void PastMeetings_ReadsThePersistedHistory_NewestFirst()
    {
        using var harness = new RuntimeHarness();
        harness.HistoryStore.Append(Record("2026-08-01T10-00-00"));
        harness.HistoryStore.Append(Record("2026-08-02T11-30-00"));
        BridgeRuntime runtime = harness.Build();

        IReadOnlyList<MeetingRecord> meetings = runtime.PastMeetings();

        Assert.Equal(
            ["2026-08-02T11-30-00", "2026-08-01T10-00-00"],
            meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void PastMeetings_IsReadEachTime_SoAMeetingEndedSinceTheLastLookIsThere()
    {
        // The menu rebuilds its submenu from this on every open, which only reflects meetings
        // ended since it was last shown if the read goes to the store rather than to a snapshot
        // taken at construction.
        using var harness = new RuntimeHarness();
        BridgeRuntime runtime = harness.Build();
        Assert.Empty(runtime.PastMeetings());

        harness.HistoryStore.Append(Record("2026-08-03T09-00-00"));

        Assert.Single(runtime.PastMeetings());
    }

    [Fact]
    public async Task OpenPastMeeting_RidesThePollToTheSummary_InAWindowOfItsOwn()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [Running("strip"), Done("decided to ship")]);
        using var harness = new RuntimeHarness { Settings = Scripted(server) };
        BridgeRuntime runtime = harness.Build();

        runtime.OpenPastMeeting(Record("meet-past"));
        await RuntimeHarness.PastMeetingSettledAsync(runtime);

        FakeMeetingWindow window = Assert.Single(harness.View.Windows);
        Assert.Equal(PipelinePhase.Done, window.Last!.Phase);
        Assert.Equal("decided to ship", window.Last.SummaryText);
        Assert.Equal(0, server.TriggerCount); // read-only: never re-triggers a pipeline
    }

    [Fact]
    public async Task OpenPastMeeting_LeavesALiveMeetingsStatusAndCommandsAlone()
    {
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [Done("last week's notes")]);
        using var harness = new RuntimeHarness { Settings = Scripted(server) };
        harness.AddDevice("mic", DeviceFlow.Capture);
        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);
        StatusView streaming = harness.View.LastStatus!;
        Assert.True(harness.View.CanEnd, "no meeting is streaming, so this proves nothing");

        runtime.OpenPastMeeting(Record("meet-past"));
        await RuntimeHarness.PastMeetingSettledAsync(runtime);

        // The past meeting rendered into its own window...
        Assert.Equal(PipelinePhase.Done, Assert.Single(harness.View.Windows).Last!.Phase);
        // ...and the live meeting is untouched: still streaming, still endable.
        Assert.Equal(streaming, harness.View.LastStatus);
        Assert.False(harness.View.CanStart);
        Assert.True(harness.View.CanEnd);

        await runtime.QuitAsync().WaitAsync(Wait);
    }

    [Fact]
    public async Task OpenPastMeeting_WhenTheOperatorClosesTheWindow_StopsPollingQuietly()
    {
        // The consumer of IMeetingWindow.Closed, and the only thing that makes the interface's
        // promise true. The script never reaches a terminal state, so the loop would poll for
        // as long as the process lives; closing the window has to be what ends it. Quietly, too:
        // a "couldn't reach the recorder" splashed into a window the operator just closed is a
        // lie about a deliberate act.
        await using FakeRecorderServer server = await FakeRecorderServer.StartAsync(
            pollScript: [Running("strip")]); // the last entry repeats: never terminal
        using var harness = new RuntimeHarness { Settings = Scripted(server) };
        BridgeRuntime runtime = harness.Build();
        // Close the window the instant it has something to show, which is the one moment a test
        // cannot otherwise reach: mid-poll, with the loop running.
        harness.AfterFirstPost = () => harness.View.Windows[0].Close();

        runtime.OpenPastMeeting(Record("meet-open"));
        await RuntimeHarness.PastMeetingSettledAsync(runtime);

        FakeMeetingWindow window = Assert.Single(harness.View.Windows);
        Assert.True(window.Rendered.Count > 0, "nothing was ever rendered, so this proves nothing");
        Assert.NotEqual(PipelinePhase.Failed, window.Last!.Phase);
        int polled = server.PollCount;
        await Task.Delay(50);
        Assert.Equal(polled, server.PollCount); // the loop really stopped, rather than settling late
    }
}
