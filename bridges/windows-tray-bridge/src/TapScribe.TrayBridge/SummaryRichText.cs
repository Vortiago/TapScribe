using System.Drawing;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// Renders the Core <see cref="SummaryMarkdown"/> block model onto a read-only
/// <see cref="RichTextBox"/> with native styled runs — headings in a larger bold font,
/// bullet/numbered prefixes, inline <c>**bold**</c>/<c>*italic*</c>, and monospace
/// <c>`code`</c> on a tinted background. Presentation only: every parsing/structure decision
/// lives in Core, so this just maps blocks to selection fonts/colours via the standard
/// "move the caret to the end, set the selection font, AppendText" pattern. A plain static
/// helper (not a custom <see cref="Control"/> subclass) so it adds no public Control property
/// and never trips the WFO1000 WinForms designer analyser.
/// </summary>
internal static class SummaryRichText
{
    private const string UiFamily = "Segoe UI";
    private const string MonoFamily = "Consolas";
    private const float BodySize = 10f;

    private static readonly Color CodeBack = Color.FromArgb(242, 242, 242);

    // Fonts are GDI handles; cache the handful of (family, size, style) combos for the app's
    // lifetime so re-rendering on every poll tick neither allocates nor leaks them.
    private static readonly Dictionary<(string Family, float Size, FontStyle Style), Font> FontCache = new();

    internal static void Render(RichTextBox box, IReadOnlyList<MarkdownBlock> blocks)
    {
        box.Clear();

        MarkdownBlockKind? prev = null;
        foreach (MarkdownBlock block in blocks)
        {
            if (prev is not null)
                Append(box, Separator(prev.Value, block.Kind), UiFamily, BodySize, FontStyle.Regular);
            prev = block.Kind;

            switch (block.Kind)
            {
                case MarkdownBlockKind.Heading:
                    float size = block.Level switch { 1 => 16f, 2 => 14f, 3 => 12f, _ => 11f };
                    AppendSpans(box, block.Spans, UiFamily, size, FontStyle.Bold);
                    break;

                case MarkdownBlockKind.Bullet or MarkdownBlockKind.Numbered:
                    string marker = block.Kind == MarkdownBlockKind.Bullet ? "•  " : $"{block.Level}.  ";
                    Append(box, marker, UiFamily, BodySize, FontStyle.Regular);
                    AppendSpans(box, block.Spans, UiFamily, BodySize, FontStyle.Regular);
                    break;

                case MarkdownBlockKind.Code:
                    Append(box, block.Text, MonoFamily, BodySize, FontStyle.Regular, CodeBack);
                    break;

                default: // Paragraph
                    AppendSpans(box, block.Spans, UiFamily, BodySize, FontStyle.Regular);
                    break;
            }
        }

        // Open scrolled to the top so the reader starts at the summary's first line.
        box.SelectionStart = 0;
        box.SelectionLength = 0;
        box.ScrollToCaret();
    }

    // Blank line between block-level elements; a single newline keeps a run of same-kind
    // list items tight, the way a markdown <ul>/<ol> reads.
    private static string Separator(MarkdownBlockKind prev, MarkdownBlockKind cur)
    {
        bool tightList =
            (prev == MarkdownBlockKind.Bullet && cur == MarkdownBlockKind.Bullet) ||
            (prev == MarkdownBlockKind.Numbered && cur == MarkdownBlockKind.Numbered);
        return tightList ? "\n" : "\n\n";
    }

    private static void AppendSpans(
        RichTextBox box, IReadOnlyList<MarkdownSpan> spans, string family, float size, FontStyle baseStyle)
    {
        foreach (MarkdownSpan span in spans)
        {
            switch (span.Style)
            {
                case MarkdownInline.Bold:
                    Append(box, span.Text, family, size, baseStyle | FontStyle.Bold);
                    break;
                case MarkdownInline.Italic:
                    Append(box, span.Text, family, size, baseStyle | FontStyle.Italic);
                    break;
                case MarkdownInline.Code:
                    Append(box, span.Text, MonoFamily, size, FontStyle.Regular, CodeBack);
                    break;
                default:
                    Append(box, span.Text, family, size, baseStyle);
                    break;
            }
        }
    }

    private static void Append(
        RichTextBox box, string text, string family, float size, FontStyle style, Color? back = null)
    {
        if (text.Length == 0)
            return;
        box.SelectionStart = box.TextLength;
        box.SelectionLength = 0;
        box.SelectionFont = Font(family, size, style);
        box.SelectionBackColor = back ?? box.BackColor;
        box.AppendText(text);
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
