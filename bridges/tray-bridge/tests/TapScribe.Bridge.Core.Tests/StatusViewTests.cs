using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="StatusView"/> — the pure map from a <see cref="TrayStatus"/> to
/// the three things the NotifyIcon shows: a context-menu header line, an icon key, and a
/// tooltip. Keeping it pure lets the at-a-glance status (issue #106) be unit-tested with
/// no WinForms; the shell just applies the result on the events it already raises.
/// </summary>
public class StatusViewTests
{
    [Fact]
    public void For_AMeetingMissingADevice_BadgesTheCount_AndWarns()
    {
        // The failure an operator actually pays for: a meeting recording one side of a call
        // while they believe it is recording both. Until now the only trace outside the menu was
        // a glyph identical to the healthy one, so nobody saw it until the transcript.
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
