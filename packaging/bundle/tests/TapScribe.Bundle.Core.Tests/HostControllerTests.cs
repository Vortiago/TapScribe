namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// What the tray's Recorder section says, and which of Start / Stop Recorder the operator
/// can reach — over a fake view and a fake Recorder lifecycle, so the presentation rules
/// are tested where they are decided rather than by installing a Bundle and breaking it.
/// </summary>
public class HostControllerTests
{
    [Fact]
    public void WhileTheRecorderIsComingUp_NeitherCommandIsOffered()
    {
        // A second Start would spawn a second preflight — an unbounded pip install — and
        // Stop has nothing to stop yet.
        var world = new World();

        world.Controller.Start();

        Assert.False(world.View.Last!.CanStart);
        Assert.False(world.View.Last!.CanStop);
    }

    [Fact]
    public void ARunningRecorderOffersStopOnly()
    {
        var world = new World();
        world.Controller.Start();

        world.Controller.Report(RecorderState.Running, "TapScribe is running.");

        HostView shown = world.View.Last!;
        Assert.True(shown.CanStop);
        Assert.False(shown.CanStart);
        Assert.Equal("TapScribe is running.", shown.Header);
    }

    [Fact]
    public void AnUnmanagedRecorderIsShownAndCannotBeStopped()
    {
        // ADR-0022's ownership rule, at the menu: a Recorder the operator launched from a
        // terminal is visible but not this tray's to kill. Start stays live, because they
        // may stop that one themselves and want this tray to take over.
        var world = new World();
        world.Controller.Start();

        world.Controller.Report(RecorderState.Unmanaged, "already running from somewhere else");

        HostView shown = world.View.Last!;
        Assert.False(shown.CanStop, "the tray offered to stop a Recorder it does not own");
        Assert.True(shown.CanStart);
    }

    [Fact]
    public void AFailedOrStoppedRecorderOffersStart()
    {
        var world = new World();
        world.Controller.Start();

        world.Controller.Report(RecorderState.Failed, "TapScribe could not start.");

        Assert.True(world.View.Last!.CanStart);
        Assert.False(world.View.Last!.CanStop);
    }

    [Fact]
    public void StartRecorder_WhileOneIsAlreadyComingUp_DoesNotStartASecond()
    {
        // Preflight is an unbounded pip install; a double-click must not run two of them.
        // The guard asks Render whether Start would even have been offered, so the menu and
        // the command can never disagree about it.
        var world = new World();
        world.Controller.Start();

        world.Controller.StartRecorder();

        Assert.Equal(1, world.Host.Starts);
    }

    [Fact]
    public void StartRecorder_AfterAFailure_StartsAgain()
    {
        var world = new World();
        world.Controller.Start();
        world.Controller.Report(RecorderState.Failed, "TapScribe could not start.");

        world.Controller.StartRecorder();

        Assert.Equal(2, world.Host.Starts);
    }

    [Fact]
    public void StopRecorder_LeavesAnUnmanagedRecorderAlone()
    {
        // Belt and braces with the disabled menu item: the command itself refuses, so a
        // shell that got the enablement wrong still cannot kill somebody else's Recorder.
        var world = new World { Host = { Manages = false } };
        world.Controller.Start();
        world.Controller.Report(RecorderState.Unmanaged, "elsewhere");

        world.Controller.StopRecorder();

        Assert.False(world.Host.Stopped, "the tray killed a Recorder it does not own");
    }

    [Fact]
    public void StopRecorder_StopsOneTheTrayStarted()
    {
        var world = new World { Host = { Manages = true } };
        world.Controller.Start();
        world.Controller.Report(RecorderState.Running, "up");

        world.Controller.StopRecorder();

        Assert.True(world.Host.Stopped);
        Assert.False(world.View.Last!.CanStop);
        Assert.True(world.View.Last!.CanStart);
    }

    [Fact]
    public void OnlyThisTraysOwnRunningRecorderIsTradedThePasswordForALoginLink()
    {
        // The mint POSTs this install's password to whatever answers the port. Unmanaged means
        // that is not this tray's Recorder — another user's, a start.sh, anything at all — and
        // while it is still coming up, or stopped, nothing on the port is known to be ours.
        var world = new World { Host = { Manages = true } };
        world.Controller.Start();
        Assert.False(world.Controller.OwnsRunningRecorder, "minted while the Recorder was still coming up");

        world.Controller.Report(RecorderState.Running, "up");
        Assert.True(world.Controller.OwnsRunningRecorder);

        world.Controller.Report(RecorderState.Stopped, "down");
        Assert.False(world.Controller.OwnsRunningRecorder);

        var elsewhere = new World { Host = { Manages = false } };
        elsewhere.Controller.Start();
        elsewhere.Controller.Report(RecorderState.Unmanaged, "already running from somewhere else");
        Assert.False(elsewhere.Controller.OwnsRunningRecorder, "the password was offered to a Recorder that is not ours");
    }

    [Fact]
    public void DashboardUrl_ForARecorderThatIsNotOurs_OpensSignedOutWithoutMinting()
    {
        var world = new World { Host = { Manages = false } };
        world.Controller.Start();
        world.Controller.Report(RecorderState.Unmanaged, "already running from somewhere else");
        var logged = new List<string>();
        // No handler behind this client: a mint would throw, so reaching one fails the test.
        using var http = new HttpClient(new RefusingHandler());

        string url = world.Controller.DashboardUrl(http, BundleLayout.ForWindows("prog", "profile"), logged.Add);

        Assert.Equal(BundleDefaults.DashboardUrl, url);
        Assert.Contains(logged, line => line.Contains("not this tray's own", StringComparison.Ordinal));
    }

    [Fact]
    public void ConfirmQuit_AJobInFlightOnOurRecorder_AsksFirst()
    {
        World world = RunningWorld(manages: true);
        string? asked = null;
        int quits = 0;

        int before = world.Posted;
        world.Controller.ConfirmQuit(() => 1, warning => { asked = warning; return true; }, () => quits++, _ => { });
        RunTheAnswer(world, before);

        Assert.NotNull(asked);
        Assert.Equal(1, quits);
    }

    [Fact]
    public void ConfirmQuit_KeepRunning_LeavesTheTrayUp_AndTheNextQuitAsksAgain()
    {
        World world = RunningWorld(manages: true);
        int asks = 0;
        int quits = 0;

        int before = world.Posted;
        world.Controller.ConfirmQuit(() => 1, _ => { asks++; return false; }, () => quits++, _ => { });
        RunTheAnswer(world, before);
        Assert.Equal(0, quits);

        before = world.Posted;
        world.Controller.ConfirmQuit(() => 1, _ => { asks++; return true; }, () => quits++, _ => { });
        RunTheAnswer(world, before);

        Assert.Equal(2, asks);
        Assert.Equal(1, quits);
    }

    [Fact]
    public void ConfirmQuit_ASecondClickWhileAsking_IsTheSameRequest()
    {
        World world = RunningWorld(manages: true);
        int probes = 0;

        int before = world.Posted;
        world.Controller.ConfirmQuit(() => { Interlocked.Increment(ref probes); return 1; }, _ => true, () => { }, _ => { });
        world.Controller.ConfirmQuit(() => { Interlocked.Increment(ref probes); return 1; }, _ => true, () => { }, _ => { });
        RunTheAnswer(world, before);

        Assert.Equal(1, probes);
    }

    [Fact]
    public void ConfirmQuit_ARecorderThisTrayDoesNotOwn_IsNeitherProbedNorAskedAbout()
    {
        // Somebody else's Recorder outlives the Quit, and so does whatever it is working on.
        World world = RunningWorld(manages: false);
        bool probed = false;
        bool asked = false;
        int quits = 0;

        int before = world.Posted;
        world.Controller.ConfirmQuit(() => { probed = true; return 3; }, _ => asked = true, () => quits++, _ => { });
        RunTheAnswer(world, before);

        Assert.False(probed);
        Assert.False(asked);
        Assert.Equal(1, quits);
    }

    [Fact]
    public void ConfirmQuit_AProbeThatThrows_StillQuits()
    {
        World world = RunningWorld(manages: true);
        var logged = new List<string>();
        int quits = 0;

        int before = world.Posted;
        world.Controller.ConfirmQuit(
            () => throw new InvalidOperationException("probe broke"), _ => false, () => quits++, logged.Add);
        RunTheAnswer(world, before);

        Assert.Equal(1, quits);
        Assert.Contains(logged, line => line.Contains("probe broke", StringComparison.Ordinal));
    }

    /// <summary>A controller whose Recorder is up, with the renders that took it there already
    /// shown, so the next post is the Quit check's answer.</summary>
    private static World RunningWorld(bool manages)
    {
        var world = new World(holdPosts: true) { Host = { Manages = manages } };
        world.Controller.Start();
        world.Controller.Report(manages ? RecorderState.Running : RecorderState.Unmanaged, "up");
        world.RunPostsNewestFirst();
        return world;
    }

    /// <summary>Wait for the Quit check, which probes on the pool, to post its answer, then run
    /// it the way the shell's UI thread would.</summary>
    private static void RunTheAnswer(World world, int before)
    {
        Assert.True(
            SpinWait.SpinUntil(() => world.Posted > before, TimeSpan.FromSeconds(10)),
            "the Quit check never posted its answer");
        world.RunPostsNewestFirst();
    }

    private sealed class RefusingHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            throw new InvalidOperationException("the login link was minted against a Recorder that is not ours");
    }

    [Fact]
    public void EveryRenderArrivesThroughTheShellsMarshaller()
    {
        // State arrives on the supervisor's background thread and IHostView promises the
        // UI one. A render that skipped the post would be a cross-thread touch on both
        // shells — WinForms throws, AppKit is worse.
        var world = new World();

        world.Controller.Start();
        world.Controller.Report(RecorderState.Running, "up");

        Assert.Equal(world.View.Renders, world.Posted);
        Assert.True(world.View.Renders >= 2);
    }

    private sealed class FakeHostView : IHostView
    {
        public HostView? Last { get; private set; }

        public int Renders { get; private set; }

        public void ShowHost(HostView? host)
        {
            Last = host;
            Renders++;
        }
    }

    private sealed class FakeHost : IRecorderHost
    {
        public bool Manages { get; set; }

        public int Starts { get; private set; }

        public bool Stopped { get; private set; }

        public Task Start()
        {
            Starts++;
            return Task.CompletedTask;
        }

        public void Stop() => Stopped = true;

        public void Dispose()
        {
        }
    }

    [Fact]
    public void AReportThatReachesTheViewLateDoesNotOverwriteANewerOne()
    {
        // The race, made deterministic: the view is reached outside _gate, so two threads
        // can compute their views in one order and post them in the other. Driven by holding
        // the posts and running them backwards, which is the same observable as losing the
        // scheduling race and needs no threads to reproduce.
        var world = new World(holdPosts: true);

        world.Controller.Report(RecorderState.Running, "TapScribe is running.");
        world.Controller.Report(RecorderState.Stopped, "TapScribe is not running.");
        world.RunPostsNewestFirst();

        // The newer report is what the menu shows, header AND commands together.
        Assert.Equal("TapScribe is not running.", world.View.Last!.Header);
        Assert.True(world.View.Last.CanStart);
        Assert.False(world.View.Last.CanStop);
        // And the stale one was DROPPED rather than merely re-run and overwritten: it must
        // not reach the shell at all, since a shell may do more than assign a label.
        Assert.Equal(1, world.View.Renders);
    }

    private sealed class World
    {
        public FakeHostView View { get; } = new();

        public FakeHost Host { get; } = new();

        // The Quit check posts from the pool while a test waits on the test thread, so the
        // held posts and their count are read and written under one lock, and a post is held
        // BEFORE it is counted: a test that sees the count go up then finds the post to run.
        private readonly Lock _lock = new();
        private readonly List<Action> _held = [];
        private int _posted;

        public int Posted
        {
            get
            {
                lock (_lock)
                    return _posted;
            }
        }

        public HostController Controller { get; }

        public World(bool holdPosts = false)
        {
            Controller = new HostController(
                View,
                post: action =>
                {
                    lock (_lock)
                    {
                        if (holdPosts)
                            _held.Add(action);
                        _posted++;
                    }
                    if (!holdPosts)
                        action();
                },
                Host);
        }

        /// <summary>Run the held posts in reverse: the shape of a newer report reaching the
        /// view before an older one.</summary>
        public void RunPostsNewestFirst()
        {
            Action[] held;
            lock (_lock)
            {
                held = [.. _held];
                _held.Clear();
            }
            for (int i = held.Length - 1; i >= 0; i--)
                held[i]();
        }
    }
}
