using System.Text.RegularExpressions;

namespace TapScribe.Bridge.Core;

/// <summary>Inline run style within a <see cref="MarkdownBlock"/>.</summary>
public enum MarkdownInline { Normal, Bold, Italic, Code }

/// <summary>A styled inline run — text plus how to render it.</summary>
public sealed record MarkdownSpan(string Text, MarkdownInline Style);

/// <summary>What kind of block a <see cref="MarkdownBlock"/> is.</summary>
public enum MarkdownBlockKind { Heading, Paragraph, Bullet, Numbered, Code }

/// <summary>
/// One parsed block of a summary. <see cref="Spans"/> holds the inline runs for every
/// kind except <see cref="MarkdownBlockKind.Code"/> (which carries verbatim
/// <see cref="Text"/>). <see cref="Level"/> is the heading depth (1..6) for headings and
/// the sequential list number (1,2,3…) for numbered items; it is 0 otherwise.
/// </summary>
public sealed record MarkdownBlock(
    MarkdownBlockKind Kind,
    IReadOnlyList<MarkdownSpan> Spans,
    int Level = 0,
    string Text = "");

/// <summary>
/// Parses the minimal markdown subset TapScribe summaries use into a UI-agnostic block
/// model the WinForms tray window renders. Deliberately mirrors the web dashboard's
/// <c>renderMarkdown</c>/<c>_inlineMd</c> (<c>tapscribe/web/js/templates.js</c>) so the two
/// renderers stay in conceptual sync: <c>#</c>–<c>######</c> headings, <c>-</c>/<c>*</c>
/// bullets, <c>1.</c>/<c>1)</c> numbered items, fenced <c>```</c> code blocks, paragraphs,
/// and inline <c>`code`</c> / <c>**bold**</c> / <c>*italic*</c> (no tables, links, or
/// nesting). Anything else stays literal text. Lives in Core (no WinForms) so it builds and
/// is exhaustively unit-tested on the Linux/CI cross-platform job, like <see cref="PipelineView"/>.
/// </summary>
public static class SummaryMarkdown
{
    private static readonly Regex HeadingRe = new(@"^(#{1,6})\s+(.*)$", RegexOptions.Compiled);
    private static readonly Regex BulletRe = new(@"^[-*]\s+(.*)$", RegexOptions.Compiled);
    private static readonly Regex NumberedRe = new(@"^\d+[.)]\s+(.*)$", RegexOptions.Compiled);

    // Same alternation as the web _inlineMd: `code` | **bold** | *italic* — flat, no nesting.
    private static readonly Regex InlineRe = new(@"(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)", RegexOptions.Compiled);

    private enum ListKind { None, Bullet, Numbered }

    /// <summary>Parse summary markdown into the block model. Null/empty text → no blocks.</summary>
    public static IReadOnlyList<MarkdownBlock> Parse(string? text)
    {
        var blocks = new List<MarkdownBlock>();
        var para = new List<string>();
        List<string>? fence = null;
        var list = ListKind.None;
        int ordinal = 0;

        void FlushPara()
        {
            if (para.Count == 0)
                return;
            blocks.Add(new MarkdownBlock(MarkdownBlockKind.Paragraph, ParseInline(string.Join(" ", para))));
            para.Clear();
        }

        void FlushFence()
        {
            blocks.Add(new MarkdownBlock(
                MarkdownBlockKind.Code, Array.Empty<MarkdownSpan>(), Text: string.Join("\n", fence!)));
            fence = null;
        }

        // Split on '\n' and drop a trailing '\r' so CRLF and LF parse identically (the web
        // splits on /\r?\n/).
        foreach (string raw in (text ?? "").Split('\n'))
        {
            string line = raw.EndsWith('\r') ? raw[..^1] : raw;

            if (fence is not null)
            {
                if (line.TrimStart().StartsWith("```", StringComparison.Ordinal))
                    FlushFence();
                else
                    fence.Add(line);
                continue;
            }

            string t = line.Trim();

            if (t.StartsWith("```", StringComparison.Ordinal))
            {
                FlushPara();
                list = ListKind.None;
                fence = new List<string>();
                continue;
            }

            if (t.Length == 0)
            {
                FlushPara();
                list = ListKind.None;
                continue;
            }

            Match h = HeadingRe.Match(t);
            if (h.Success)
            {
                FlushPara();
                list = ListKind.None;
                blocks.Add(new MarkdownBlock(
                    MarkdownBlockKind.Heading, ParseInline(h.Groups[2].Value), Level: h.Groups[1].Value.Length));
                continue;
            }

            // Bullets win over numbered (the web tests `[-*]` first); a bullet also breaks
            // any open numbered run, so the next numbered item restarts at 1.
            Match b = BulletRe.Match(t);
            if (b.Success)
            {
                FlushPara();
                list = ListKind.Bullet;
                blocks.Add(new MarkdownBlock(MarkdownBlockKind.Bullet, ParseInline(b.Groups[1].Value)));
                continue;
            }

            Match n = NumberedRe.Match(t);
            if (n.Success)
            {
                FlushPara();
                if (list != ListKind.Numbered) // a fresh <ol> renumbers from 1, like the web
                {
                    list = ListKind.Numbered;
                    ordinal = 0;
                }
                ordinal++;
                blocks.Add(new MarkdownBlock(MarkdownBlockKind.Numbered, ParseInline(n.Groups[1].Value), Level: ordinal));
                continue;
            }

            list = ListKind.None;
            para.Add(t);
        }

        FlushPara();
        if (fence is not null)
            FlushFence(); // unterminated fence — still emit what we collected, like the web

        return blocks;
    }

    /// <summary>
    /// Wrap plain, non-markdown text (a status line, a failure reason — which may carry a
    /// raw recorder error containing <c>-</c>/<c>*</c>/backticks) as a single normal
    /// paragraph, so it renders verbatim and is never reinterpreted as markdown. The
    /// non-Done counterpart to <see cref="Parse"/>; the caller picks by phase via
    /// <c>MeetingFormView.BodyIsMarkdown</c>.
    /// </summary>
    public static IReadOnlyList<MarkdownBlock> Plain(string? text) =>
        new[] { new MarkdownBlock(MarkdownBlockKind.Paragraph, new[] { new MarkdownSpan(text ?? "", MarkdownInline.Normal) }) };

    /// <summary>
    /// Split a line into styled inline runs, mirroring the web <c>_inlineMd</c>: backtick
    /// code, <c>**bold**</c>, <c>*italic*</c>, with the text between matches kept literal.
    /// Unmatched markup stays as <see cref="MarkdownInline.Normal"/> text.
    /// </summary>
    public static IReadOnlyList<MarkdownSpan> ParseInline(string text)
    {
        var spans = new List<MarkdownSpan>();
        int last = 0;
        foreach (Match m in InlineRe.Matches(text))
        {
            if (m.Index > last)
                spans.Add(new MarkdownSpan(text[last..m.Index], MarkdownInline.Normal));

            if (m.Groups[1].Success)
                spans.Add(new MarkdownSpan(m.Value[1..^1], MarkdownInline.Code));
            else if (m.Groups[2].Success)
                spans.Add(new MarkdownSpan(m.Value[2..^2], MarkdownInline.Bold));
            else
                spans.Add(new MarkdownSpan(m.Value[1..^1], MarkdownInline.Italic));

            last = m.Index + m.Length;
        }
        if (last < text.Length)
            spans.Add(new MarkdownSpan(text[last..], MarkdownInline.Normal));
        return spans;
    }
}
