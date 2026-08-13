using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Pins the two Start-path shell bugs, driven through the real <c>Start</c> handler on an
/// STA thread with a scripted outside world (no WASAPI endpoint, no Recorder).
///
/// B2 — the shell opens one capture per resolved device into a plain local and hands them to
/// <c>CaptureOrchestrator.StartAll</c>. Nothing else can reach them in that window, and the
/// finally disposed only the enumerator, so an unexpected throw between the two stranded
/// every capture already opened for the process lifetime.
///
/// B6 — the shell published the meeting and then kept working inside the same try (the
/// "some devices unavailable" balloon, the final control/status post). A throw in that
/// window fell to the catch, which classifies it as a failed START: Start re-enabled and End
/// greyed out on a meeting that was streaming, leaving the operator no way to end it.
/// </summary>
public class TrayStartTests
{
    [Fact]
    public void Start_WhenOpeningADeviceThrowsUnexpectedly_ReleasesTheCapturesAlreadyOpened()
    {
        using var sta = new StaShell();
        var harness = new TrayHarness();
        harness.Enumerator.Add("mic", DeviceFlow.Capture);
        harness.Enumerator.Add("system", DeviceFlow.Render);
        // One device opens, the NEXT one blows up. Counted rather than named: which device the
        // shell reaches first is not this test's subject, and pinning it to one made the test
        // unable to reach its subject at all the first time it ran for real.
        harness.Enumerator.FailOpenNumber = 2;

        TrayContext tray = sta.Build(harness);
        sta.Run(tray.Start);
        Task start = tray.StartTask!;
        var failure = Assert.Throws<AggregateException>(() => start.Wait(StaShell.CallTimeout));

        // It really did escape the shell's own classification — that is what leaves the
        // finally as the captures' only owner.
        Assert.IsType<IOException>(failure.InnerException);

        // The guard, on the right property this time. A capture is handed out NOT started (a
        // TapSession starts it, and StartAll — which builds those — is never reached on this
        // path), so "was it opened" is the enumerator's record, never capture.Started. With
        // nothing opened there is nothing that could have leaked and the rest proves nothing.
        FakeCapture stranded = Assert.Single(harness.Enumerator.Opened);
        Assert.False(stranded.Started, "StartAll was reached, so this is no longer the pre-handoff path");

        // The subject: nothing the shell took out of the enumerator is left un-owned.
        Assert.All(harness.Enumerator.Opened, capture =>
        {
            Assert.True(capture.Disposed, "a capture the shell opened was stranded with no owner");
            Assert.Equal(1, capture.Disposals); // released once, not double-released
        });
        Assert.True(harness.Enumerator.Disposed, "the device enumerator was stranded too");

        // And released in the right order: the enumerator handed its endpoint to the capture,
        // so it has to outlive it.
        Assert.True(harness.Enumerator.CapturesReleasedFirst);
    }

    [Fact]
    public void Start_WhenTheOrchestratorRefusesTheSpecs_ReleasesEachCaptureExactlyOnce()
    {
        // The real B2 trigger, end to end: both devices open, are handed to StartAll, and
        // StartAll gives back no orchestrator. Every device here fails to START — each is
        // released by the core's per-device skip, and then zero pipelines is not a meeting,
        // so the whole set is refused. Two owners would both release those captures — the
        // core (which releases what it refuses) and the shell's finally — on a backend that
        // is contract-bound to be throw-free but nowhere promised to be idempotent.
        using var sta = new StaShell();
        var harness = new TrayHarness();
        harness.Enumerator.Add("mic", DeviceFlow.Capture, capture: new FakeCapture { ThrowOnStart = true });
        harness.Enumerator.Add("system", DeviceFlow.Render, capture: new FakeCapture { ThrowOnStart = true });

        TrayContext tray = sta.BuildAndStart(harness);

        // The guard: both devices really were opened and handed over, so StartAll was
        // genuinely reached and refused them — otherwise the release count below is a
        // statement about a path nobody took. The attempted Start is the other half of it:
        // only the core starts a capture, so it proves the specs got that far.
        Assert.Equal(2, harness.Enumerator.Opened.Count);
        Assert.All(harness.Enumerator.Opened, capture => Assert.Equal(1, capture.StartAttempts));

        Assert.All(harness.Enumerator.Opened, capture =>
            Assert.Equal(1, capture.Disposals)); // released once, by the core — not again by the shell
        Assert.True(harness.Enumerator.Disposed);

        // ...and the operator gets the failure, not a half-started meeting.
        Assert.True(tray.StartItem.Enabled);
        Assert.False(tray.EndItem.Enabled);
    }

    [Fact]
    public void Start_WhenAPostFailsAfterThePublish_LeavesTheLiveMeetingsControlsAlone()
    {
        using var sta = new StaShell();
        var harness = new TrayHarness();
        FakeCapture mic = harness.Enumerator.Add("mic", DeviceFlow.Capture);
        // No render endpoint is present, so the system-audio selection does not resolve and
        // the shell has a "some devices unavailable" balloon to post AFTER it publishes the
        // meeting. That balloon is the first thing it posts, and it is exactly the window
        // B6 lives in.
        TrayContext tray = sta.Build(harness);
        sta.ThrowOnNextPost();

        sta.Run(tray.Start);
        try
        {
            tray.StartTask!.Wait(StaShell.CallTimeout);
        }
        catch (AggregateException)
        {
            // With the fix the failed post is outside the try, so it escapes this
            // fire-and-forget task; before the fix it was caught and classified as a failed
            // START. WHICH of the two happened is the bug, and it is asserted below through
            // what the operator would see — so swallowing the exception here is what keeps
            // this test on the observable rather than on the mechanism. Nothing is lost: an
            // escape that carried a different failure would still show up in the menu state.
        }
        _ = sta.Drain(); // let any consequences the shell did post actually happen

        Assert.True(mic.Started, "the meeting never started, so this proves nothing");
        Assert.False(mic.Disposed, "the meeting is supposed to still be streaming");

        // The harm: a failure to RENDER classified as a failure to START would re-enable
        // Start over a live meeting (and grey out the only way to end it).
        Assert.False(tray.StartItem.Enabled, "Start was re-enabled while a meeting is streaming");
        Assert.DoesNotContain("⚠", tray.StatusHeader, StringComparison.Ordinal);

        sta.Run(tray.Quit); // the meeting really is live — tear it down before leaving
    }

    [Fact]
    public void Start_WhenEveryDeviceIsMissing_ReturnsTheMenuToIdle_AndReleasesTheEnumerator()
    {
        // The guardrail for the test above: a start that genuinely fails BEFORE the publish
        // must still roll the menu back to idle, so "never roll back" can't be implemented
        // by never rolling back at all.
        using var sta = new StaShell();
        var harness = new TrayHarness(); // no devices registered at all

        TrayContext tray = sta.BuildAndStart(harness);

        Assert.True(tray.StartItem.Enabled, "Start stayed greyed out after a failed start");
        Assert.False(tray.EndItem.Enabled);
        Assert.Contains("⚠", tray.StatusHeader, StringComparison.Ordinal);
        Assert.True(harness.Enumerator.Disposed);
    }
}
