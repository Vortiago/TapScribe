using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray's presence in the notification area: the icon, its hover tooltip, and its
/// balloons. Everything the shell does that needs a real desktop session with a running
/// shell — a <c>Shell_NotifyIcon</c> registration — lives behind this and nowhere else.
///
/// That is the whole reason it is a seam. The menu, the status header, the meeting
/// lifecycle and every decision this branch fixes are plain objects; the notification-area
/// icon is the one part that talks to the OS, and a host without a shell to talk to cannot
/// be reasoned with — <c>Shell_NotifyIcon</c> blocks for seconds and then fails or takes the
/// process with it. Substituting it is what lets the tray's behaviour be tested on a CI
/// runner at all. (A later slice replaces this with the full ITrayView; this is deliberately
/// only the OS-facing sliver.)
/// </summary>
internal interface ITrayIndicator : IDisposable
{
    /// <summary>Attach the context menu the icon shows on right-click. Called once.</summary>
    void Attach(ContextMenuStrip menu);

    /// <summary>Apply the current status to the icon and tooltip. The menu's header line is
    /// the shell's own business — this is only the OS-facing half of the same view.</summary>
    void Show(StatusView view);

    /// <summary>A warning balloon: something the operator needs to see went wrong.</summary>
    void Warn(string title, string message);

    /// <summary>An informational balloon: a meeting was saved, a summary is ready.</summary>
    void Inform(string title, string message);
}

/// <summary>The real thing: a <see cref="NotifyIcon"/> over the runtime-drawn
/// <see cref="TrayIcons"/>, which together are the tray's whole OS surface.</summary>
internal sealed class NotifyIconIndicator : ITrayIndicator
{
    /// <summary>NotifyIcon.Text is capped at 63 characters — a property of the OS API, so it
    /// is enforced here rather than by whoever composed the tooltip.</summary>
    private const int TooltipLimit = 63;

    private readonly TrayIcons _icons = new();
    private readonly NotifyIcon _icon;

    public NotifyIconIndicator()
    {
        _icon = new NotifyIcon { Visible = true };
        // The idle icon and tooltip come from the same StatusView every later change does,
        // rather than a hand-copy of its output that could drift from it (and that bypassed
        // the tooltip cap below).
        Show(StatusView.For(new TrayStatus.Idle()));
    }

    public void Attach(ContextMenuStrip menu) => _icon.ContextMenuStrip = menu;

    public void Show(StatusView view)
    {
        ArgumentNullException.ThrowIfNull(view);
        _icon.Icon = _icons[view.Icon];
        _icon.Text = Fit(view.Tooltip);
    }

    // The tooltip, cut to what the OS will take. One short again when the cut lands between
    // the halves of a surrogate pair: a status line carries device names and error text, an
    // emoji or a rarer CJK glyph in one is two chars, and half of one renders as the
    // replacement box rather than as a truncation. The macOS shell's MenuNotice makes the same
    // allowance against its own budget.
    private static string Fit(string tooltip)
    {
        if (tooltip.Length <= TooltipLimit)
            return tooltip;
        int keep = char.IsHighSurrogate(tooltip[TooltipLimit - 1]) ? TooltipLimit - 1 : TooltipLimit;
        return tooltip[..keep];
    }

    public void Warn(string title, string message) =>
        _icon.ShowBalloonTip(4000, title, message, ToolTipIcon.Warning);

    public void Inform(string title, string message) =>
        _icon.ShowBalloonTip(5000, title, message, ToolTipIcon.Info);

    public void Dispose()
    {
        _icon.Visible = false;
        _icon.Dispose();
        _icons.Dispose();
    }
}
