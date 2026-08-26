using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="StatusView"/> — the pure map from a <see cref="TrayStatus"/> to the
/// four things a tray shows: a menu header line, an icon key, a tooltip, and the badge beside
/// the glyph. Keeping it pure lets the at-a-glance status (issue #106) be unit-tested with no
/// WinForms and no AppKit; a shell just applies the result on the events it already raises.
/// </summary>
public class StatusViewTests
{
    [Fact]
    public void For_AMeetingWhereNobodyHasSpokenYet_BadgesNothing()
    {
        // Connected counts devices that have STREAMED, and a tap opens only when the level gate
        // opens on speech (TapSession.OpenUtterance). So every meeting begins at 0-of-N, and a
        // badge there would fire the alarm on second one of every healthy meeting, which is how
        // an operator learns to ignore it before it ever means anything.
        StatusView view = StatusView.For(new TrayStatus.Streaming(Connected: 0, Total: 2));

        Assert.Equal("", view.Badge);
        Assert.Equal(TrayIcon.Streaming, view.Icon);
    }

    [Fact]
    public void For_AMeetingMissingADevice_BadgesTheCount_AndWarns()
    {
        // One side has been heard and the other has not: the shape of a microphone whose grant
        // was dismissed, which delivers silence rather than an error and so never reaches the
        // tally's Dropped path at all. A device that drops AFTER streaming is TrayStatus.Error
        // instead, with a message naming it, so this arm is exactly the never-heard case.
        StatusView view = StatusView.For(new TrayStatus.Streaming(Connected: 1, Total: 2));

        Assert.Equal("1/2", view.Badge);
        Assert.Equal(TrayIcon.Degraded, view.Icon);
    }

    [Fact]
    public void For_Streaming_UsesStreamingIcon_AndShowsTheDeviceCount()
    {
        // The healthy pair, and the badge stays empty for it: a number sitting in the menu bar
        // for every meeting is one an operator stops reading, which is how the degraded case
        // would go unnoticed again.
        StatusView view = StatusView.For(new TrayStatus.Streaming(Connected: 2, Total: 2));

        Assert.Equal(TrayIcon.Streaming, view.Icon);
        Assert.Equal("", view.Badge);
        Assert.Contains("2/2", view.Header, StringComparison.Ordinal);
    }

    [Fact]
    public void For_Idle_UsesIdleIcon()
    {
        StatusView view = StatusView.For(new TrayStatus.Idle());

        Assert.Equal(TrayIcon.Idle, view.Icon);
        Assert.False(string.IsNullOrWhiteSpace(view.Header));
    }

    [Fact]
    public void For_Starting_UsesIdleIcon()
    {
        StatusView view = StatusView.For(new TrayStatus.Starting());

        Assert.Equal(TrayIcon.Idle, view.Icon);
        Assert.False(string.IsNullOrWhiteSpace(view.Header));
    }

    [Fact]
    public void For_Error_UsesErrorIcon_AndSurfacesTheReason()
    {
        StatusView view = StatusView.For(new TrayStatus.Error("Tap token rejected"));

        Assert.Equal(TrayIcon.Error, view.Icon);
        Assert.Contains("Tap token rejected", view.Header, StringComparison.Ordinal);
        Assert.Contains("Tap token rejected", view.Tooltip, StringComparison.Ordinal);
    }

    [Fact]
    public void For_Ending_ShowsAnActiveIcon_WhileTapsDrain()
    {
        StatusView view = StatusView.For(new TrayStatus.Ending());

        Assert.Equal(TrayIcon.Streaming, view.Icon);
        Assert.False(string.IsNullOrWhiteSpace(view.Header));
    }

    [Fact]
    public void For_Processing_SurfacesTheStageLabel_WithAnActiveIcon()
    {
        StatusView view = StatusView.For(new TrayStatus.Processing("Transcribing 3/12…"));

        Assert.Equal(TrayIcon.Streaming, view.Icon);
        Assert.Contains("Transcribing 3/12…", view.Header, StringComparison.Ordinal);
    }

    [Fact]
    public void For_SummaryReady_UsesIdleIcon()
    {
        StatusView view = StatusView.For(new TrayStatus.SummaryReady());

        Assert.Equal(TrayIcon.Idle, view.Icon);
        Assert.False(string.IsNullOrWhiteSpace(view.Header));
    }

    [Fact]
    public void For_PipelineFailed_UsesErrorIcon_AndSurfacesTheReason()
    {
        StatusView view = StatusView.For(new TrayStatus.PipelineFailed("No usable audio was captured."));

        Assert.Equal(TrayIcon.Error, view.Icon);
        Assert.Contains("No usable audio was captured.", view.Header, StringComparison.Ordinal);
    }
}
