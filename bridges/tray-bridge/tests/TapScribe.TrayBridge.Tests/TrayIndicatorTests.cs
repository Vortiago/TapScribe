using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

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
        Assert.True(tray.StartTask!.Wait(StaShell.CallTimeout), "the start never settled");
        _ = sta.Drain();

        StatusView shown = Assert.IsType<StatusView>(harness.Indicator.LastStatus);
        Assert.Equal(TrayIcon.Error, shown.Icon);
        Assert.Equal(shown.Header, tray.StatusHeader);
        Assert.NotEmpty(harness.Indicator.Balloons);
    }

    [Fact]
    public void ADeviceThatKeepsDropping_ShowsTheError_ButToastsOnce()
    {
        // B5's shell wiring, and the cost of getting the repeat wrong. A dropped device is
        // reported once per Utterance for the rest of the meeting: the status line has to say
        // so throughout, but the operator must be TOLD once — ShowBalloon is a real 4-second
        // Windows toast, so the naive wiring toasts every utterance until the meeting ends.
        using var sta = new StaShell();
        // A device is reported by the IDENTITY its tap streams under — what the Recorder
        // attributes its recordings to, and what the operator sees on the dashboard — not by
        // the endpoint's device name. Spelled out here rather than left to the default pair,
        // where the mic's identity is quietly the operator's own label.
        const string micIdentity = "Alice";
        BridgeSettings settings = TrayHarness.DefaultSettings();
        settings.Devices =
        [
            new DeviceSelection.FollowDefault(DeviceFlow.Capture, micIdentity, micIdentity),
            new DeviceSelection.FollowDefault(DeviceFlow.Render, "System audio", "System audio"),
        ];
        var harness = new TrayHarness { Settings = settings };
        FakeCapture mic = harness.Enumerator.Add("mic-endpoint", DeviceFlow.Capture);
        harness.Enumerator.Add("system-endpoint", DeviceFlow.Render);

        TrayContext tray = sta.StartMeeting(harness);
        int balloonsBefore = harness.Indicator.Balloons.Count;

        mic.RaiseFailed(new IOException("endpoint gone"));
        mic.RaiseFailed(new IOException("endpoint gone")); // the next utterance says the same
        mic.RaiseFailed(new IOException("endpoint gone")); // ...and the next
        _ = sta.Drain();

        (string Title, string Message) toast =
            Assert.Single(harness.Indicator.Balloons.Skip(balloonsBefore));
        Assert.Equal($"{micIdentity} stopped", toast.Title); // the whole message, not just the name

        // The status still says it, though — that is what the header is for.
        Assert.Contains($"{micIdentity} stopped", tray.StatusHeader, StringComparison.Ordinal);
        Assert.Equal(TrayIcon.Error, harness.Indicator.LastStatus!.Icon);
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

        sta.Build(harness, deps);

        Action kick = Assert.Single(scheduled);   // exactly one, and deferred...
        Assert.Equal(0, harness.Stores.StateLoads); // ...with nothing done in the ctor

        sta.Run(kick.Invoke); // what the message loop would do on its first turn

        Assert.Equal(1, harness.Stores.StateLoads); // and THEN it looks for a meeting to resume
    }
}
