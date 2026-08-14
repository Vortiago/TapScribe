using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// The shell as an <see cref="ITrayView"/>: what the runtime tells it, rendered onto a
/// NotifyIcon and a ContextMenuStrip. The lifecycle behind those calls is
/// <see cref="BridgeRuntime"/>'s and is covered without WinForms; what is left here is the
/// mapping, and it is the half that only ever runs on Windows.
///
/// It also pins the seam that makes this whole assembly runnable, so it cannot quietly close
/// again. Everything the tray does that needs a real desktop session (registering a
/// notification-area icon, its tooltip, its balloons) sits behind <see cref="ITrayIndicator"/>.
/// When it did not, constructing the shell called <c>Shell_NotifyIcon</c> on a CI runner with
/// no shell to answer, and the test host died before a single result was reported: no
/// assertion, no stack, the whole assembly aborted.
/// </summary>
public class TrayIndicatorTests
{
    [Fact]
    public void TheShell_RoutesItsStatusThroughTheIndicator_NotTheOSDirectly()
    {
        using var sta = new StaShell();
        using var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);

        // Attached at construction: this is what the icon shows on right-click, and it is the
        // only handle the OS surface gets on the shell's menu.
        Assert.Same(tray.Menu, harness.Indicator.AttachedMenu);

        StatusView error = StatusView.For(new TrayStatus.Error("the recorder is unreachable"));
        sta.Run(() => ((ITrayView)tray).ShowStatus(error));

        // One StatusView reaches the indicator and the menu header together, so a substituted
        // indicator sees exactly what the real one would.
        Assert.Same(error, harness.Indicator.LastStatus);
        Assert.Equal(error.Header, tray.StatusHeader);
    }

    [Fact]
    public void ANotice_GoesToTheChannelItsKindNames()
    {
        // The mapping is the shell's whole contribution here, and getting it backwards is
        // invisible in a screenshot: an information balloon for a failure reads as reassurance.
        using var sta = new StaShell();
        using var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);
        sta.Run(() =>
        {
            ((ITrayView)tray).ShowNotice("mic stopped", "endpoint gone", NoticeKind.Warning);
            ((ITrayView)tray).ShowNotice("Meeting summary ready", "notes are ready", NoticeKind.Information);
        });

        Assert.Equal(("mic stopped", "endpoint gone"), Assert.Single(harness.Indicator.Warnings));
        Assert.Equal(
            ("Meeting summary ready", "notes are ready"), Assert.Single(harness.Indicator.Informations));
    }

    [Fact]
    public void TheMenuState_DrivesTheTwoMeetingCommands()
    {
        // Including "both disabled", which is a legitimate state rather than an error one: a
        // meeting that is ending, or a pipeline in flight.
        using var sta = new StaShell();
        using var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);
        Assert.True(tray.StartItem.Enabled);
        Assert.False(tray.EndItem.Enabled);

        sta.Run(() => ((ITrayView)tray).SetMenuState(canStart: false, canEnd: true));
        Assert.False(tray.StartItem.Enabled);
        Assert.True(tray.EndItem.Enabled);

        sta.Run(() => ((ITrayView)tray).SetMenuState(canStart: false, canEnd: false));
        Assert.False(tray.StartItem.Enabled);
        Assert.False(tray.EndItem.Enabled);
    }

    [Fact]
    public void Quit_ReleasesTheIndicator()
    {
        // The OS registration must go when the tray goes: it is the one resource that outlives
        // the process's own memory if it is leaked. It is released on the marshalled Shutdown
        // at the END of the teardown, so a drain stands between the click and the release.
        using var sta = new StaShell();
        using var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);
        Assert.False(harness.Indicator.Disposed);

        sta.Quit(tray);

        Assert.True(harness.Indicator.Disposed, "the notification-area icon outlived the tray");
    }

    [Fact]
    public void TheShell_BuildsItsRuntimeOnTheLoopsFirstTurn_RatherThanInItsConstructor()
    {
        // Two things this holds. The resume kick used to be a WinForms timer created in the
        // constructor, which registers a native timer window on the calling thread and left it
        // behind on any thread that never pumped a loop and then exited: that is what killed the
        // test host. And the runtime is built on that turn because it is the first moment
        // SynchronizationContext.Current is the WinForms one, so a constructor that built it
        // would capture nothing to marshal through.
        using var sta = new StaShell();
        using var harness = new TrayHarness();
        harness.StateStore.Save(new MeetingState { SessionId = "2026-08-10T09-00-00" });
        var scheduled = new List<Action>();
        TrayDependencies deps = harness.Dependencies with { ScheduleOnLoopStart = scheduled.Add };

        TrayContext tray = sta.Build(harness, deps);

        Action kick = Assert.Single(scheduled);            // exactly one, and deferred...
        Assert.Equal("○ Idle", tray.StatusHeader);         // ...with nothing done in the ctor

        sta.Run(kick.Invoke); // what the message loop would do on its first turn

        // The runtime exists and has resumed the meeting the previous session left behind.
        Assert.Contains("Resuming", tray.StatusHeader, StringComparison.Ordinal);
    }

    [Fact]
    public void AClickBeforeTheLoopsFirstTurn_DoesNothing_RatherThanThrowing()
    {
        // The other side of the deferral above, and a real window rather than a theoretical
        // one: the icon goes visible in the indicator's constructor and the menu is built
        // before Application.Run, while the runtime is built from a one-shot 200 ms timer. So
        // the tray is on screen and clickable for about a fifth of a second with no runtime
        // behind it. Throwing there surfaces an unhandled-exception dialog out of a click
        // handler; doing nothing matches what the operator already believes, which is that they
        // clicked a tray that had not finished starting.
        using var sta = new StaShell();
        using var harness = new TrayHarness();
        var scheduled = new List<Action>();
        TrayDependencies deps = harness.Dependencies with { ScheduleOnLoopStart = scheduled.Add };

        TrayContext tray = sta.Build(harness, deps); // the kick is held, so there is no runtime

        sta.Run(() =>
        {
            tray.StartItem.PerformClick();
            tray.EndItem.PerformClick();
            tray.RebuildPastMeetingsMenu();
        });

        // Nothing happened, and nothing pretended to: the menu is exactly as it was built.
        Assert.True(tray.StartItem.Enabled, "a click with no runtime behind it moved the menu");
        Assert.False(tray.EndItem.Enabled);
        Assert.Equal("○ Idle", tray.StatusHeader);
        // The submenu still rebuilds, from an empty history rather than from a null runtime.
        ToolStripItem placeholder = Assert.Single(tray.PastMeetingsItem.DropDownItems.Cast<ToolStripItem>());
        Assert.False(placeholder.Enabled);
    }
}
