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

    private sealed class World
    {
        public FakeHostView View { get; } = new();

        public FakeHost Host { get; } = new();

        public int Posted { get; private set; }

        public HostController Controller { get; }

        public World()
        {
            Controller = new HostController(
                View,
                post: action =>
                {
                    Posted++;
                    action();
                },
                Host);
        }
    }
}
