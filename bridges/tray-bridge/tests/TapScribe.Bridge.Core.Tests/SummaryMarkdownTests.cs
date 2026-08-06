using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The summary markdown parser the tray window renders (per the meeting-summary viewer):
/// a pure projection from raw LLM markdown to a UI-agnostic block model, mirroring the web
/// dashboard's <c>renderMarkdown</c>/<c>_inlineMd</c> subset. Pinned cross-platform here so
/// the WinForms <c>SummaryRichText</c> renderer stays a dumb painter — the sibling of
/// <see cref="MeetingFormViewTests"/>.
/// </summary>
public class SummaryMarkdownTests
{
    private static MarkdownBlock Single(string text) => Assert.Single(SummaryMarkdown.Parse(text));

    // The single Normal-styled span's text, asserting the block carries exactly one run.
    private static string Plain(MarkdownBlock block)
    {
        MarkdownSpan span = Assert.Single(block.Spans);
        Assert.Equal(MarkdownInline.Normal, span.Style);
        return span.Text;
    }

    private static (MarkdownInline Style, string Text)[] Runs(MarkdownBlock block) =>
        block.Spans.Select(s => (s.Style, s.Text)).ToArray();

    [Theory]
    [InlineData("# Decisions", 1)]
    [InlineData("## Decisions", 2)]
    [InlineData("### Decisions", 3)]
    [InlineData("#### Decisions", 4)]
    [InlineData("##### Decisions", 5)]
    [InlineData("###### Decisions", 6)]
    public void Heading_levels_map_to_depth(string line, int level)
    {
        MarkdownBlock b = Single(line);
        Assert.Equal(MarkdownBlockKind.Heading, b.Kind);
        Assert.Equal(level, b.Level);
        Assert.Equal("Decisions", Plain(b));
    }

    [Fact]
    public void Seven_hashes_is_not_a_heading()
    {
        MarkdownBlock b = Single("####### nope");
        Assert.Equal(MarkdownBlockKind.Paragraph, b.Kind); // #{1,6} caps at six, so this stays prose
    }

    [Theory]
    [InlineData("- ship it")]
    [InlineData("* ship it")]
    public void Dash_and_asterisk_make_bullets(string line)
    {
        MarkdownBlock b = Single(line);
        Assert.Equal(MarkdownBlockKind.Bullet, b.Kind);
        Assert.Equal("ship it", Plain(b));
    }

    [Fact]
    public void Italic_line_is_a_paragraph_not_a_bullet()
    {
        // "*foo*" has no space after the leading '*', so it's emphasis, not a bullet marker.
        MarkdownBlock b = Single("*foo*");
        Assert.Equal(MarkdownBlockKind.Paragraph, b.Kind);
        Assert.Equal(new[] { (MarkdownInline.Italic, "foo") }, Runs(b));
    }

    [Fact]
    public void Numbered_items_renumber_sequentially_ignoring_the_source_digit()
    {
        IReadOnlyList<MarkdownBlock> blocks = SummaryMarkdown.Parse("1. a\n2. b\n9. c");

        Assert.All(blocks, b => Assert.Equal(MarkdownBlockKind.Numbered, b.Kind));
        Assert.Equal(new[] { 1, 2, 3 }, blocks.Select(b => b.Level).ToArray());
    }

    [Fact]
    public void Numbered_run_restarts_after_a_blank_line()
    {
        IReadOnlyList<MarkdownBlock> blocks = SummaryMarkdown.Parse("1. a\n\n5. b");

        Assert.Equal(new[] { 1, 1 }, blocks.Select(b => b.Level).ToArray()); // a fresh <ol> each time
    }

    [Fact]
    public void A_bullet_breaks_the_numbered_run_so_it_restarts()
    {
        IReadOnlyList<MarkdownBlock> blocks = SummaryMarkdown.Parse("1. a\n- x\n4. b");

        Assert.Equal(MarkdownBlockKind.Numbered, blocks[0].Kind);
        Assert.Equal(1, blocks[0].Level);
        Assert.Equal(MarkdownBlockKind.Bullet, blocks[1].Kind);
        Assert.Equal(MarkdownBlockKind.Numbered, blocks[2].Kind);
        Assert.Equal(1, blocks[2].Level); // restarts, not 2
    }

    [Fact]
    public void Inline_spans_mix_bold_italic_and_code()
    {
        MarkdownBlock b = Single("Plain **bold** then *italic* then `code`.");

        Assert.Equal(
            new[]
            {
                (MarkdownInline.Normal, "Plain "),
                (MarkdownInline.Bold, "bold"),
                (MarkdownInline.Normal, " then "),
                (MarkdownInline.Italic, "italic"),
                (MarkdownInline.Normal, " then "),
                (MarkdownInline.Code, "code"),
                (MarkdownInline.Normal, "."),
            },
            Runs(b));
    }

    [Fact]
    public void Unmatched_markup_stays_literal_text()
    {
        Assert.Equal("a * b", Plain(Single("a * b")));       // lone asterisk
        Assert.Equal("*not closed", Plain(Single("*not closed")));
    }

    [Fact]
    public void Fenced_code_is_verbatim_with_no_inline_parsing()
    {
        MarkdownBlock b = Single("```\nrun **x** now\nsecond line\n```");

        Assert.Equal(MarkdownBlockKind.Code, b.Kind);
        Assert.Empty(b.Spans);
        Assert.Equal("run **x** now\nsecond line", b.Text); // ** stays literal inside a fence
    }

    [Fact]
    public void Unterminated_fence_still_emits_what_was_collected()
    {
        MarkdownBlock b = Single("```\nleft\nopen");

        Assert.Equal(MarkdownBlockKind.Code, b.Kind);
        Assert.Equal("left\nopen", b.Text);
    }

    [Fact]
    public void Consecutive_lines_join_into_one_paragraph()
    {
        MarkdownBlock b = Single("line one\nline two");
        Assert.Equal("line one line two", Plain(b)); // single newlines join with a space
    }

    [Fact]
    public void A_blank_line_separates_paragraphs()
    {
        IReadOnlyList<MarkdownBlock> blocks = SummaryMarkdown.Parse("first\n\nsecond");

        Assert.Equal(2, blocks.Count);
        Assert.Equal("first", Plain(blocks[0]));
        Assert.Equal("second", Plain(blocks[1]));
    }

    [Fact]
    public void Crlf_line_endings_parse_like_lf()
    {
        MarkdownBlock b = Single("alpha\r\nbeta");
        Assert.Equal("alpha beta", Plain(b)); // trailing \r is stripped, lines still join
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   \n  \n")]
    public void Empty_or_whitespace_input_yields_no_blocks(string? text)
    {
        Assert.Empty(SummaryMarkdown.Parse(text));
    }

    [Theory]
    [InlineData("- Unable to connect")] // a recorder error that *looks* like a bullet
    [InlineData("**Error**: timeout")]  // …or like bold
    [InlineData("1. retry failed")]     // …or like a numbered item
    public void Plain_keeps_markdown_looking_status_text_verbatim(string status)
    {
        MarkdownBlock b = Assert.Single(SummaryMarkdown.Plain(status));
        Assert.Equal(MarkdownBlockKind.Paragraph, b.Kind);
        Assert.Equal(status, Plain(b)); // one Normal run, markup untouched
    }

    [Fact]
    public void A_realistic_summary_parses_into_the_expected_block_shape()
    {
        const string summary =
            "## Decisions\n" +
            "- Ship Q3 feature\n" +
            "- Hire **2** backend engineers\n" +
            "\n" +
            "## Action items\n" +
            "1. Draft the spec\n" +
            "2. Review `api/tap`\n";

        IReadOnlyList<MarkdownBlock> blocks = SummaryMarkdown.Parse(summary);

        Assert.Equal(
            new[]
            {
                MarkdownBlockKind.Heading,
                MarkdownBlockKind.Bullet,
                MarkdownBlockKind.Bullet,
                MarkdownBlockKind.Heading,
                MarkdownBlockKind.Numbered,
                MarkdownBlockKind.Numbered,
            },
            blocks.Select(b => b.Kind).ToArray());
        Assert.Equal(new[] { 1, 2 }, blocks.Where(b => b.Kind == MarkdownBlockKind.Numbered).Select(b => b.Level).ToArray());
    }
}
