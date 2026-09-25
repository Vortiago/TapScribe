using AppKit;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Putting text on the general pasteboard, which is a three-call incantation with one
/// non-obvious term (<c>NSPasteboardType.String.GetConstant()</c>).
///
/// Here rather than twice in this project: the meeting window's Copy and the tray's Copy
/// password are the same act, and the second copy of it had already lost the empty guard the
/// first one carries.
/// </summary>
internal static class Pasteboard
{
    /// <summary>Replace the pasteboard's contents with <paramref name="text"/>. Empty text is
    /// ignored — clearing the pasteboard for nothing would be a theft of whatever the
    /// operator had on it.</summary>
    internal static void Put(string text)
    {
        if (string.IsNullOrEmpty(text))
            return;

        NSPasteboard pasteboard = NSPasteboard.GeneralPasteboard;
        pasteboard.ClearContents();
        pasteboard.SetStringForType(text, NSPasteboardType.String.GetConstant()!);
    }
}
