using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>The menu-bar face of one tray state.</summary>
/// <param name="Name">An SF Symbol name, rendered as a template image so it follows the menu
/// bar's own light/dark appearance.</param>
/// <param name="Fallback">What the status item shows as text if the system has no such
/// symbol. Never blank: an empty menu-bar item is indistinguishable from a Bridge that is not
/// running.</param>
internal sealed record StatusSymbol(string Name, string Fallback);

/// <summary>
/// Which glyph stands for which <see cref="TrayIcon"/>. Kept out of the status item because
/// nothing NSObject-derived can be constructed under a test host, and this is the part of the
/// menu bar that is a decision rather than a widget.
/// </summary>
internal static class StatusSymbols
{
    /// <summary>The glyph for one tray state.</summary>
    /// <param name="icon">The state the runtime is reporting.</param>
    internal static StatusSymbol For(TrayIcon icon) => icon switch
    {
        TrayIcon.Idle => new StatusSymbol("waveform", "◦"),
        TrayIcon.Streaming => new StatusSymbol("waveform.circle.fill", "●"),
        TrayIcon.Error => new StatusSymbol("exclamationmark.triangle.fill", "⚠"),
        // A state added in Core reaches here with no glyph of its own. Answering with the
        // idle one would show a Bridge at rest through a failure, so this refuses instead:
        // loud, and on the first status rather than at some later moment.
        _ => throw new ArgumentOutOfRangeException(nameof(icon), icon, "no menu-bar glyph for this tray state"),
    };
}
