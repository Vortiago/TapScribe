namespace TapScribe.Bridge.Core;

/// <summary>Which faces a run is painted in. Flags, because a heading is bold and an italic
/// span inside one is both.</summary>
[Flags]
public enum SummaryEmphasis
{
    None = 0,
    Bold = 1,
    Italic = 2,
}

/// <summary>
/// One painted run of a rendered summary: a stretch of text that shares a face and a size.
/// </summary>
/// <param name="Text">The characters to paint. Never empty.</param>
/// <param name="Emphasis">Bold, italic, both or neither.</param>
/// <param name="Mono">A monospace face. Also the cue for a shaded background, since the two
/// go together everywhere a summary uses either.</param>
/// <param name="SizePlus">Points ABOVE the shell's own body size, so 0 is body text and a
/// heading is a step up. Relative rather than absolute because the two shells disagree about
/// what body size is (10pt on Windows, the system size on macOS) and neither should have to
/// know about the other's.</param>
public sealed record SummaryRun(string Text, SummaryEmphasis Emphasis, bool Mono, int SizePlus);

/// <summary>
/// Flattens <see cref="SummaryMarkdown"/>'s block model into painted runs.
///
/// Here rather than in a shell because every decision it makes is one both shells make
/// identically: where a blank line goes, how tight a run of list items is, what marker a
/// bullet gets, how far a heading steps up, and which face an inline span takes. What is left
/// for a shell is mapping a run to a font object, which is the only part that is genuinely
/// about a toolkit.
/// </summary>
public static class SummaryLayout
{
    /// <summary>Flatten <paramref name="blocks"/> into the runs to paint, in order,
    /// separators and list markers included.</summary>
    public static IReadOnlyList<SummaryRun> Flatten(IReadOnlyList<MarkdownBlock> blocks)
    {
        ArgumentNullException.ThrowIfNull(blocks);
        List<SummaryRun> runs = [];
        MarkdownBlockKind? previous = null;
        // A block that paints nothing earns no separator and is not what the next one is
        // separated from. An empty fenced code block is the shape that reaches here.
        foreach (MarkdownBlock block in blocks.Where(Paints))
        {
            if (previous is not null)
                runs.Add(new SummaryRun(Separator(previous.Value, block.Kind), SummaryEmphasis.None, false, 0));
            previous = block.Kind;

            switch (block.Kind)
            {
                case MarkdownBlockKind.Heading:
                    Add(runs, block.Spans, SummaryEmphasis.Bold, HeadingSizePlus(block.Level));
                    break;

                case MarkdownBlockKind.Code:
                    runs.Add(new SummaryRun(block.Text, SummaryEmphasis.None, true, 0));
                    break;

                case MarkdownBlockKind.Bullet or MarkdownBlockKind.Numbered:
                    string marker = block.Kind == MarkdownBlockKind.Bullet ? "•  " : $"{block.Level}.  ";
                    runs.Add(new SummaryRun(marker, SummaryEmphasis.None, false, 0));
                    Add(runs, block.Spans, SummaryEmphasis.None, sizePlus: 0);
                    break;

                default:
                    Add(runs, block.Spans, SummaryEmphasis.None, sizePlus: 0);
                    break;
            }
        }

        return runs;
    }

    // Whether this block puts anything on screen. A list item always does: it has a marker.
    private static bool Paints(MarkdownBlock block) => block.Kind switch
    {
        MarkdownBlockKind.Code => block.Text.Length > 0,
        MarkdownBlockKind.Bullet or MarkdownBlockKind.Numbered => true,
        _ => block.Spans.Any(static s => s.Text.Length > 0),
    };

    // How far a heading sits above body text. A ramp rather than one heading size, because a
    // summary uses two or three levels and they have to be told apart; it flattens past level 4
    // because a summary that deep is a nested list wearing headings and stepping further would
    // only make the smallest ones smaller than the body.
    private static int HeadingSizePlus(int level) => level switch { 1 => 6, 2 => 4, 3 => 2, _ => 1 };

    // A blank line between block-level elements, but a single newline inside a run of same-kind
    // list items: a markdown <ul> reads tight, and a summary's decisions and next-steps lists
    // would otherwise be spaced like separate paragraphs.
    private static string Separator(MarkdownBlockKind previous, MarkdownBlockKind current)
    {
        bool tightList =
            (previous == MarkdownBlockKind.Bullet && current == MarkdownBlockKind.Bullet)
            || (previous == MarkdownBlockKind.Numbered && current == MarkdownBlockKind.Numbered);
        return tightList ? "\n" : "\n\n";
    }

    // Empty spans are filtered rather than painted: a zero-length run is invisible either way,
    // and letting one through would make every caller's "did anything render" check lie.
    private static void Add(
        List<SummaryRun> runs,
        IReadOnlyList<MarkdownSpan> spans,
        SummaryEmphasis baseEmphasis,
        int sizePlus)
    {
        foreach (MarkdownSpan span in spans.Where(static s => s.Text.Length > 0))
        {
            // A span's emphasis is ADDED to the block's rather than replacing it, so an italic
            // word in a heading stays bold. Inline code is the exception: it switches face
            // entirely, because a monospace run carrying the heading's weight reads as neither.
            (SummaryEmphasis emphasis, bool mono) = span.Style switch
            {
                MarkdownInline.Bold => (baseEmphasis | SummaryEmphasis.Bold, false),
                MarkdownInline.Italic => (baseEmphasis | SummaryEmphasis.Italic, false),
                MarkdownInline.Code => (SummaryEmphasis.None, true),
                _ => (baseEmphasis, false),
            };
            runs.Add(new SummaryRun(span.Text, emphasis, mono, sizePlus));
        }
    }
}
