using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Pins the seam that makes this whole assembly runnable, so it cannot quietly close again.
///
/// Everything the tray does that needs a real desktop session — registering a
/// notification-area icon, its tooltip, its balloons — sits behind
/// <see cref="ITrayIndicator"/>. When it did not, constructing the shell called
/// <c>Shell_NotifyIcon</c> on a CI runner with no shell to answer, and the test host died
/// before a single result was reported: no assertion, no stack, the whole assembly aborted.
/// The rule these tests hold is "the shell's status and balloons go through the indicator,
/// and nothing else in the shell touches the OS" — reintroduce a direct NotifyIcon and the
/// first assertion here goes quiet rather than the run going red.
/// </summary>
public class TrayIndicatorTests
{
    [Fact]
    public void TheShell_RoutesItsStatusThroughTheIndicator_NotTheOSDirectly()
    {
        using var sta = new StaShell();
        var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);

        // Attached at construction: this is what the icon shows on right-click, and it is the
        // only handle the OS surface gets on the shell's menu.
        Assert.Same(tray.Menu, harness.Indicator.AttachedMenu);

        // A status change reaches the indicator and the menu header together, from one
        // StatusView — so a substituted indicator sees exactly what the real one would.
        sta.Run(tray.Start); // no devices registered: resolves to nothing and fails to idle
        Assert.True(tray.StartTask!.Wait(TimeSpan.FromSeconds(30)), "the start never settled");
        _ = sta.Drain();

        StatusView shown = Assert.IsType<StatusView>(harness.Indicator.LastStatus);
        Assert.Equal(TrayIcon.Error, shown.Icon);
        Assert.Equal(shown.Header, tray.StatusHeader);
        Assert.NotEmpty(harness.Indicator.Balloons);
    }

    [Fact]
    public void Quit_ReleasesTheIndicator()
    {
        // The OS registration must go when the tray goes — it is the one resource that
        // outlives the process's own memory if it is leaked.
        using var sta = new StaShell();
        var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);
        Assert.False(harness.Indicator.Disposed);

        sta.Run(tray.Quit);

        Assert.True(harness.Indicator.Disposed, "the notification-area icon outlived the tray");
    }

    [Fact]
    public void TheShell_SchedulesItsResumeKick_RatherThanTimingItItself()
    {
        // The other thing that killed the host: the resume kick used to be a WinForms timer
        // created in the constructor, which registers a native timer window on the calling
        // thread — left behind on any thread that never pumps a loop and then exits. It is a
        // scheduling decision now, so a caller that has no message loop supplies none.
        using var sta = new StaShell();
        var harness = new TrayHarness();
        var scheduled = new List<Action>();
        TrayDependencies deps = harness.Dependencies with { ScheduleOnLoopStart = scheduled.Add };

        TrayContext tray = sta.Get(() => new TrayContext(harness.Settings, deps));
        try
        {
            Action kick = Assert.Single(scheduled);   // exactly one, and deferred...
            Assert.Equal(0, harness.Stores.StateLoads); // ...with nothing done in the ctor

            sta.Run(kick.Invoke); // what the message loop would do on its first turn

            Assert.Equal(1, harness.Stores.StateLoads); // and THEN it looks for a meeting to resume
        }
        finally
        {
            sta.Run(tray.Dispose);
        }
    }
}
