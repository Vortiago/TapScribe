using System.Drawing;
using System.Windows.Forms;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Windows;

/// <summary>
/// Paints Core's <see cref="SummaryLayout"/> runs into a <see cref="RichTextBox"/>.
///
/// Everything this used to decide now lives in Core, where the Mac shell reads the same
/// answers: the separator rule, the heading ramp and the list markers were shell-local and so
/// were written twice. What is left is the one genuinely WinForms part, the "move the caret to
/// the end, set the selection font, AppendText" pattern that RichTextBox requires for mixed
/// formatting. <c>SummaryRichTextTests</c> covers the mapping over an STA-built box.
///
/// A plain static class rather than a control subclass: nothing here holds state beyond the
/// font cache, and the caller already owns the box.
/// </summary>
internal static class SummaryRichText
{
    private const string UiFamily = "Segoe UI";
    private const string MonoFamily = "Consolas";

    // What Core's SummaryRun.SizePlus is relative to.
    private const float BodySize = 10f;

    private static readonly Color CodeBack = Color.FromArgb(242, 242, 242);

    // Fonts are GDI handles; cache the handful of (family, size, style) combos for the app's
    // lifetime so re-rendering on every poll tick neither allocates nor leaks them.
    private static readonly Dictionary<(string Family, float Size, FontStyle Style), Font> FontCache = new();

    internal static void Render(RichTextBox box, IReadOnlyList<MarkdownBlock> blocks)
    {
        box.Clear();

        foreach (SummaryRun run in SummaryLayout.Flatten(blocks))
        {
            box.SelectionStart = box.TextLength;
            box.SelectionLength = 0;
            box.SelectionFont = Font(
                run.Mono ? MonoFamily : UiFamily,
                BodySize + run.SizePlus,
                Style(run.Emphasis));
            // Shading follows the monospace face, which is the convention Core's Mono flag
            // carries: inline code and code blocks are the only shaded runs a summary has.
            box.SelectionBackColor = run.Mono ? CodeBack : box.BackColor;
            box.AppendText(run.Text);
        }

        // Open scrolled to the top so the reader starts at the summary's first line.
        box.SelectionStart = 0;
        box.SelectionLength = 0;
        box.ScrollToCaret();
    }

    private static FontStyle Style(SummaryEmphasis emphasis)
    {
        FontStyle style = FontStyle.Regular;
        if (emphasis.HasFlag(SummaryEmphasis.Bold))
            style |= FontStyle.Bold;
        if (emphasis.HasFlag(SummaryEmphasis.Italic))
            style |= FontStyle.Italic;
        return style;
    }

    private static Font Font(string family, float size, FontStyle style)
    {
        (string, float, FontStyle) key = (family, size, style);
        if (!FontCache.TryGetValue(key, out Font? font))
        {
            font = new Font(family, size, style);
            FontCache[key] = font;
        }

        return font;
    }
}
