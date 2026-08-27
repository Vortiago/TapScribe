using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// A notice, squeezed into the one line a menu item is.
///
/// The Mac shell shows <see cref="ITrayView.ShowNotice"/> under the status header instead of
/// as a system notification, because a notification needs an authorization grant a locally
/// built, unsigned .app routinely does not get, and a notice that silently never appears is
/// worse than one the operator finds where they are already looking. Slice 7 owns real
/// failure signalling; this is the honest version of it until then.
/// </summary>
internal static class MenuNotice
{
    /// <summary>The widest line the menu shows. A notice's message is exception text, which
    /// has no length its author was thinking about.</summary>
    internal const int MaxLength = 120;

    /// <summary>Compose the menu line for one notice.</summary>
    /// <param name="message">The detail behind it, often empty and often exception text.</param>
    internal static string Line(string title, string message, NoticeKind kind)
    {
        string detail = Flatten(message);
        string body = detail.Length == 0 ? title : $"{title}: {detail}";
        string prefix = kind == NoticeKind.Warning ? "⚠ " : "ℹ ";
        string line = prefix + body;
        // One short of the budget to leave room for the ellipsis; Clamp owns the rest.
        return line.Length <= MaxLength ? line : DisplayText.Clamp(line, MaxLength - 1) + "…";
    }

    // A menu item is one line, so every run of whitespace (the line breaks an IOException
    // arrives with, in particular) becomes a single space rather than a break the menu would
    // swallow or truncate at.
    private static string Flatten(string message) =>
        string.Join(' ', message.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
}
