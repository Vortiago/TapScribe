using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Windows.Tests;

/// <summary>
/// Pins B10 — the tray's menu must not outlive the tray. <c>NotifyIcon.Dispose</c> does NOT
/// dispose the <see cref="ContextMenuStrip"/> it renders, so quitting released the icon and the
/// icon bitmaps and left the whole menu behind; and <c>RebuildPastMeetingsMenu</c> disposes
/// only the PREVIOUS set of submenu items on each open, so the last set built always
/// survived too. On a tray that runs for days across many meetings that is a menu item per
/// meeting per submenu open, never released.
///
/// The submenu itself is the shell's, and so are these tests: what goes IN it comes from
/// <c>BridgeRuntime.PastMeetings</c>, which has its own cover.
/// </summary>
public class TrayMenuLifetimeTests
{
    [Fact]
    public void Quit_DisposesTheMenu_AndEveryItemUnderIt()
    {
        using var sta = new StaShell();
        using var harness = new TrayHarness();
        harness.HistoryStore.Append(
            new MeetingRecord { SessionId = "2026-08-01T10-00-00", StartedAt = DateTimeOffset.Now });
        harness.HistoryStore.Append(
            new MeetingRecord { SessionId = "2026-08-02T11-30-00", StartedAt = DateTimeOffset.Now });

        TrayContext tray = sta.Build(harness);
        ContextMenuStrip menu = null!;
        ToolStripItem[] topLevel = [];
        ToolStripItem[] pastMeetings = [];

        sta.Run(() =>
        {
            tray.RebuildPastMeetingsMenu(); // what opening the Past-meetings submenu does

            menu = tray.Menu;
            topLevel = [.. menu.Items.Cast<ToolStripItem>()];
            pastMeetings = [.. tray.PastMeetingsItem.DropDownItems.Cast<ToolStripItem>()];
        });
        sta.Quit(tray);

        // Guard against a vacuous pass: there really was a menu, and the submenu really was
        // built from the seeded history, before the quit ran.
        Assert.Equal(2, pastMeetings.Length);
        Assert.NotEmpty(topLevel);

        Assert.True(menu.IsDisposed, "the ContextMenuStrip outlived the tray");
        Assert.All(topLevel, item =>
            Assert.True(item.IsDisposed, $"menu item '{item.Text}' outlived the tray"));
        Assert.All(pastMeetings, item =>
            Assert.True(item.IsDisposed, $"past-meeting item '{item.Text}' outlived the tray"));
    }

    [Fact]
    public void RebuildPastMeetingsMenu_WithNoHistory_ShowsThePlaceholder_AndQuitReleasesIt()
    {
        // The empty case takes the other branch of the rebuild (a single disabled placeholder
        // rather than one item per meeting), and it has no other owner either.
        using var sta = new StaShell();
        using var harness = new TrayHarness();

        TrayContext tray = sta.Build(harness);
        ToolStripItem[] pastMeetings = [];
        sta.Run(() =>
        {
            tray.RebuildPastMeetingsMenu();
            pastMeetings = [.. tray.PastMeetingsItem.DropDownItems.Cast<ToolStripItem>()];
        });
        sta.Quit(tray);

        ToolStripItem placeholder = Assert.Single(pastMeetings);
        Assert.False(placeholder.Enabled);
        Assert.True(placeholder.IsDisposed, "the empty-history placeholder outlived the tray");
    }
}
