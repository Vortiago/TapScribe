using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Pins B5 — a device that drops mid-meeting must not be contradicted by the status line.
/// The orchestrator raises a per-identity failure the moment a tap can't reach the
/// Recorder or an endpoint is invalidated (unplugged / disabled / default-device switch),
/// but the tray only popped a balloon over it: the header kept reading
/// "● Streaming — 2/2 devices" under the green icon, for the rest of the meeting, while
/// one side of the conversation was not being recorded at all. <see cref="TrayStatus.Error"/>
/// is documented to cover exactly "a device that dropped"; nothing applied it.
/// </summary>
public class DeviceTallyTests
{
    [Fact]
    public void Dropped_AfterEveryDeviceConnected_ReportsAnError_NotAFullHouse()
    {
        var tally = new DeviceTally(2);
        tally.Connected("mic");
        tally.Connected("system");

        TrayStatus status = tally.Dropped("system"); // the loopback endpoint goes away

        var error = Assert.IsType<TrayStatus.Error>(status);
        Assert.Contains("system", error.Reason, StringComparison.Ordinal); // which device stopped
        Assert.Contains("1/2", error.Reason, StringComparison.Ordinal);    // and what is still recording

        // The at-a-glance signal moves too: green "2/2 devices" was the lie.
        StatusView view = StatusView.For(status);
        Assert.Equal(TrayIcon.Error, view.Icon);
        Assert.DoesNotContain("2/2", view.Header, StringComparison.Ordinal);
    }

    [Fact]
    public void Connected_AfterADrop_ReturnsToStreaming_OnceTheDeviceIsBack()
    {
        // A tap whose FIRST connect fails is terminal for that Utterance only — the
        // pipeline keeps gating, so the next Utterance opens a fresh tap that may well
        // land. The warning has to clear on it: a meeting that recovers after one blip
        // must not wear the error for its whole remaining hour.
        var tally = new DeviceTally(2);
        tally.Connected("mic");
        tally.Connected("system");
        tally.Dropped("system");

        TrayStatus status = tally.Connected("system");

        var streaming = Assert.IsType<TrayStatus.Streaming>(status);
        Assert.Equal(2, streaming.Connected);
        Assert.Equal(2, streaming.Total);
    }
}
