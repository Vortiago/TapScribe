using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Pins the two End-path shell bugs.
///
/// B3 — the End drain callback released the device enumerator on the line AFTER
/// <c>await orchestrator.EndMeetingAsync()</c>, so a teardown that threw skipped it and the
/// endpoints stayed open for the process lifetime; and <c>RunPipelineFlowAsync</c>'s catch
/// filter is narrow, so anything outside it escaped a fire-and-forget task leaving BOTH menu
/// items greyed with the header stuck on "● Ending meeting…" — a tray that has to be
/// restarted.
///
/// B4 — the End/Resume flow ran on <c>CancellationToken.None</c>, so Quit ended the message
/// loop and left the flow talking to the Recorder and posting into a context nobody pumps.
/// </summary>
public class TrayEndTeardownTests
{
    // Record-only: End drains and closes the taps but fires no pipeline, so these tests
    // exercise the teardown with no Recorder in the picture at all.
    private static BridgeSettings RecordOnly()
    {
        BridgeSettings settings = TrayHarness.DefaultSettings();
        settings.ProcessOnEnd = false;
        return settings;
    }

    private static TrayContext StartMeeting(StaShell sta, TrayHarness harness)
    {
        TrayContext tray = sta.Build(harness);
        sta.Run(tray.Start);
        Assert.True(tray.StartTask!.Wait(StaShell.CallTimeout), "the meeting never started");
        _ = sta.Drain();
        Assert.True(tray.EndItem.Enabled, "the meeting was never published as running");
        return tray;
    }

    [Fact]
    public void End_WhenTheTeardownThrows_StillReleasesTheDevices()
    {
        using var sta = new StaShell();
        var harness = new TrayHarness { Settings = RecordOnly() };
        harness.Enumerator.Add("mic", DeviceFlow.Capture, capture: new FakeCapture { ThrowOnDetach = true });
        harness.Enumerator.Add("system", DeviceFlow.Render);

        TrayContext tray = StartMeeting(sta, harness);
        sta.Run(tray.End);
        // The flow always clears the resume state on its way out, whichever way it ended —
        // so this is "the End flow finished", with no wall clock in it.
        StaShell.SpinUntil(() => harness.Stores.StateClears > 0, "the End flow to terminate");

        Assert.True(harness.Enumerator.Disposed,
            "the teardown threw and the endpoints were never released");
        Assert.Equal(1, harness.Enumerator.Disposals);
    }

    [Fact]
    public void End_WhenTheTeardownThrowsUnexpectedly_ReturnsTheMenuToTheOperator()
    {
        using var sta = new StaShell();
        var harness = new TrayHarness { Settings = RecordOnly() };
        harness.Enumerator.Add("mic", DeviceFlow.Capture, capture: new FakeCapture { ThrowOnDetach = true });
        harness.Enumerator.Add("system", DeviceFlow.Render);

        TrayContext tray = StartMeeting(sta, harness);
        sta.Run(tray.End);
        Assert.False(tray.StartItem.Enabled); // busy while the flow runs — both items greyed
        StaShell.SpinUntil(() => harness.Stores.StateClears > 0, "the End flow to terminate");
        _ = sta.Drain();

        Assert.True(tray.StartItem.Enabled,
            "the tray was left with both menu items greyed out — unusable until restarted");
        Assert.False(tray.EndItem.Enabled);
        Assert.Contains("⚠", tray.StatusHeader, StringComparison.Ordinal);
        Assert.DoesNotContain("Ending meeting", tray.StatusHeader, StringComparison.Ordinal);
    }

    [Fact]
    public void End_WhenTheTeardownSucceeds_ReleasesTheDevices_AndReturnsToIdle()
    {
        // The guardrail for both tests above: the healthy record-only End must land on the
        // same released devices and usable menu, so neither can pass by a teardown that
        // never runs or a menu that is never busy in the first place.
        using var sta = new StaShell();
        var harness = new TrayHarness { Settings = RecordOnly() };
        FakeCapture mic = harness.Enumerator.Add("mic", DeviceFlow.Capture);
        harness.Enumerator.Add("system", DeviceFlow.Render);

        TrayContext tray = StartMeeting(sta, harness);
        sta.Run(tray.End);
        StaShell.SpinUntil(() => harness.Stores.StateClears > 0, "the End flow to terminate");
        _ = sta.Drain();

        Assert.True(mic.Stopped && mic.Disposed, "End must stop and release the capture");
        Assert.True(harness.Enumerator.Disposed);
        Assert.True(tray.StartItem.Enabled);
        Assert.False(tray.EndItem.Enabled);
    }

    [Fact]
    public void Quit_CancelsTheInFlightEndFlow_SoItNeverReachesTheRecorder()
    {
        using var sta = new StaShell();
        var harness = new TrayHarness(); // ProcessOnEnd defaults to true: the trigger runs
        var slowMic = new FakeCapture { HoldDispose = true };
        harness.Enumerator.Add("mic", DeviceFlow.Capture, capture: slowMic);

        TrayContext tray = StartMeeting(sta, harness);
        sta.Run(tray.End);
        Assert.True(slowMic.DisposeReached.Wait(StaShell.CallTimeout), "the End drain never reached the device teardown");
        _ = sta.Drain(); // the "ending" render, while the tray is still alive

        sta.Run(tray.Quit);       // cancelling the in-flight flow is Quit's first act
        slowMic.ReleaseDispose(); // ...and only now does the drain finish and the trigger run

        StaShell.SpinUntil(() => harness.Stores.StateClears > 0, "the End flow to terminate");
        _ = sta.Drain();

        // Nothing is listening on 127.0.0.1:9, so an UNCANCELLED trigger would have opened a
        // socket and been refused — which the shell classifies as "Recorder unreachable". A
        // cancelled one never opens a socket at all and classifies as a timeout, so the
        // wording is the observable: it says the token reached the controller.
        Assert.Contains("did not respond within the timeout", tray.StatusHeader, StringComparison.Ordinal);
    }
}
