using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Pins B7 — a meeting exists from the operator's first click, not from the moment it is
/// published. Quit tore down whatever <c>TakeMeeting</c> handed it, and that returns
/// all-nulls for the whole span of a Start (a network round-trip to mint the detached
/// session). Quitting inside that span therefore tore down nothing: <c>ExitThread</c> ended
/// the message loop while the start was still awaiting, the start then published its meeting
/// into a shell that was already gone, and the captures kept streaming into a detached
/// session left open on the Recorder until the process died.
///
/// The ordering is decided under the shell's own lock, so this drives it explicitly: the
/// mint is held, Quit is let claim the shell, and only THEN does the mint answer — so the
/// publish is guaranteed to be the side that sees the flag, rather than whichever side the
/// scheduler happened to favour.
/// </summary>
public class TrayQuitRaceTests
{
    private static readonly TimeSpan Settle = TimeSpan.FromSeconds(30);

    [Fact]
    public void Quit_WhileAStartIsInFlight_TearsThatMeetingDown_InsteadOfPublishingIt()
    {
        using var sta = new StaShell();
        sta.RequireWinForms();
        var harness = new TrayHarness { HoldMint = true };
        FakeCapture mic = harness.Enumerator.Add("mic", DeviceFlow.Capture);
        FakeCapture system = harness.Enumerator.Add("system", DeviceFlow.Render);

        TrayContext tray = sta.Build(harness);
        sta.Run(tray.Start);
        Assert.True(harness.MintReached.Wait(Settle), "the start never reached the session mint");

        // Quit occupies the shell thread while it waits for the start to settle, so drive it
        // from here and let the mint answer only once Quit has claimed the shell.
        Task quitting = Task.Run(() => sta.Run(tray.Quit));
        StaShell.SpinUntil(() => tray.IsQuitting, "Quit to claim the shell");
        harness.CompleteMint();

        Assert.True(quitting.Wait(Settle), "Quit never returned");
        Assert.True(tray.StartTask!.Wait(Settle), "the start never settled");

        // The start built a real meeting (both devices opened and started) and then, seeing
        // the shell claimed, tore it down itself rather than handing it to nobody.
        Assert.True(mic.Started && system.Started, "no meeting was built, so this proves nothing");
        Assert.True(mic.Disposed, "the mic capture was left streaming into an abandoned meeting");
        Assert.True(system.Disposed, "the loopback capture was left streaming into an abandoned meeting");
        Assert.Equal(1, mic.Disposals); // torn down exactly once — not by both Quit and the start
        Assert.Equal(1, system.Disposals);
        Assert.True(harness.Enumerator.Disposed, "the device enumerator was abandoned");
        Assert.Equal(1, harness.Enumerator.Disposals);
    }

    [Fact]
    public void Start_WhileAnotherStartIsInFlight_IsRefused()
    {
        // The same start-in-flight state, from the other side: until this branch existed the
        // only guard was the published orchestrator, so the whole span of the mint was
        // unguarded in the shell's own model (the greyed-out menu item was the only thing
        // stopping a second meeting).
        using var sta = new StaShell();
        sta.RequireWinForms();
        var harness = new TrayHarness { HoldMint = true };
        harness.Enumerator.Add("mic", DeviceFlow.Capture);

        TrayContext tray = sta.Build(harness);
        sta.Run(tray.Start);
        Assert.True(harness.MintReached.Wait(Settle), "the start never reached the session mint");
        Task first = tray.StartTask!;

        sta.Run(tray.Start); // a second click, mid-mint

        Assert.Same(first, tray.StartTask); // no second start was ever launched
        harness.CompleteMint();
        Assert.True(first.Wait(Settle), "the start never settled");

        sta.Run(tray.Quit);
    }

    [Fact]
    public void Start_AfterQuit_IsRefused()
    {
        using var sta = new StaShell();
        sta.RequireWinForms();
        var harness = new TrayHarness();
        FakeCapture mic = harness.Enumerator.Add("mic", DeviceFlow.Capture);

        TrayContext tray = sta.Build(harness);
        sta.Run(tray.Quit);
        sta.Run(tray.Start);

        Assert.Null(tray.StartTask);
        Assert.False(mic.Started, "a meeting was started on a shell that is already gone");
    }
}
