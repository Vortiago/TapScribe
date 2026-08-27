namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Flattening parsed summary markdown into painted runs (#422).
///
/// The decisions here were WinForms-only and therefore untested: a RichTextBox cannot be
/// constructed in this suite, so the separator rule, the heading ramp, the list markers and
/// the span mapping all rode along inside the shell. The Mac meeting card needs every one of
/// them identically, so they move here and each shell keeps only the mapping from a run to its
/// own font object.
/// </summary>
public class SummaryLayoutTests
{
    [Fact]
    public void Flatten_OneParagraph_IsOneBodyRun()
    {
        IReadOnlyList<SummaryRun> runs = SummaryLayout.Flatten(SummaryMarkdown.Parse("hello there"));

        SummaryRun run = Assert.Single(runs);
        Assert.Equal("hello there", run.Text);
        Assert.Equal(SummaryEmphasis.None, run.Emphasis);
        Assert.False(run.Mono);
        Assert.Equal(0, run.SizePlus);
    }

    [Fact]
    public void Flatten_TwoParagraphs_PutsABlankLineBetweenThem()
    {
        IReadOnlyList<SummaryRun> runs = SummaryLayout.Flatten(SummaryMarkdown.Parse("first\n\nsecond"));

        Assert.Equal(["first", "\n\n", "second"], runs.Select(r => r.Text));
    }

    [Fact]
    public void Flatten_ConsecutiveBullets_KeepsTheListTight()
    {
        // A single newline between same-kind items, the way a markdown <ul> reads. Two would
        // space a three-item list out like three paragraphs, which is what the summary's
        // "decisions" and "next steps" lists mostly are.
        IReadOnlyList<SummaryRun> runs = SummaryLayout.Flatten(SummaryMarkdown.Parse("- one\n- two"));

        Assert.DoesNotContain("\n\n", runs.Select(r => r.Text));
        Assert.Contains("\n", runs.Select(r => r.Text));
    }

    [Fact]
    public void Flatten_Bullets_CarryABulletMarker()
    {
        IReadOnlyList<SummaryRun> runs = SummaryLayout.Flatten(SummaryMarkdown.Parse("- one"));

        Assert.Equal(["•  ", "one"], runs.Select(r => r.Text));
    }

    [Fact]
    public void Flatten_NumberedItems_CountFromTheBlocksOwnNumber()
    {
        // Not from this list's position in the flattened output: SummaryMarkdown already
        // renumbers a fresh list from 1 the way a web <ol> does, so the marker reads the
        // block's Level and inherits that rather than re-deciding it.
        IReadOnlyList<SummaryRun> runs = SummaryLayout.Flatten(SummaryMarkdown.Parse("1. one\n2. two"));

        Assert.Equal(["1.  ", "one", "\n", "2.  ", "two"], runs.Select(r => r.Text));
    }

    [Fact]
    public void Flatten_Headings_StepUpInSizeAndAreBold()
    {
        IReadOnlyList<SummaryRun> runs =
            SummaryLayout.Flatten(SummaryMarkdown.Parse("# one\n\n## two\n\n### three\n\n#### four"));

        Assert.Equal([6, 4, 2, 1], runs.Where(r => r.Text != "\n\n").Select(r => r.SizePlus));
        Assert.All(runs.Where(r => r.Text != "\n\n"), r => Assert.Equal(SummaryEmphasis.Bold, r.Emphasis));
    }

    [Fact]
    public void Flatten_InlineSpans_TakeTheirOwnFace()
    {
        IReadOnlyList<SummaryRun> runs =
            SummaryLayout.Flatten(SummaryMarkdown.Parse("plain **bold** *italic* `code`"));

        Assert.Equal(SummaryEmphasis.None, runs.Single(r => r.Text == "plain ").Emphasis);
        Assert.Equal(SummaryEmphasis.Bold, runs.Single(r => r.Text == "bold").Emphasis);
        Assert.Equal(SummaryEmphasis.Italic, runs.Single(r => r.Text == "italic").Emphasis);
        Assert.True(runs.Single(r => r.Text == "code").Mono);
    }

    [Fact]
    public void Flatten_EmphasisInsideAHeading_KeepsTheHeadingsWeight()
    {
        // The reason SummaryEmphasis is flags. An italic word in a heading is bold AND italic;
        // an enum of exclusive faces would silently drop one, and the heading would have a
        // word in it that reads as body text.
        IReadOnlyList<SummaryRun> runs = SummaryLayout.Flatten(SummaryMarkdown.Parse("# a *b*"));

        SummaryRun emphasised = runs.Single(r => r.Text == "b");
        Assert.Equal(SummaryEmphasis.Bold | SummaryEmphasis.Italic, emphasised.Emphasis);
        Assert.Equal(6, emphasised.SizePlus);
    }

    [Fact]
    public void Flatten_ACodeBlock_IsOneVerbatimMonoRun()
    {
        // Verbatim: a code block's whole point is that nothing inside it was parsed, so it
        // carries Text rather than Spans and is painted as it arrived.
        IReadOnlyList<SummaryRun> runs =
            SummaryLayout.Flatten(SummaryMarkdown.Parse("```\nx = **1**\n```"));

        SummaryRun run = Assert.Single(runs);
        Assert.Contains("**1**", run.Text);
        Assert.True(run.Mono);
        Assert.Equal(SummaryEmphasis.None, run.Emphasis);
    }

    [Fact]
    public void Flatten_NoBlocks_IsNoRuns()
    {
        // An empty summary paints nothing rather than a stray separator, which is what a
        // leading separator would be.
        Assert.Empty(SummaryLayout.Flatten(SummaryMarkdown.Parse("")));
    }

    [Fact]
    public void Flatten_ABlockThatPaintsNothing_EarnsNoSeparator()
    {
        // The same rule as above, for the block that HAS no text rather than the summary that
        // has no blocks. An empty fence parses to a Code block carrying "", and skipping only
        // its own run left the separator behind: two blank lines as the summary's first run,
        // which reads as a card that starts with a gap.
        Assert.Equal(
            ["Hello"],
            SummaryLayout.Flatten(SummaryMarkdown.Parse("```\n```\n\nHello")).Select(r => r.Text));
    }

    [Fact]
    public void Flatten_ABlockThatPaintsNothingBetweenTwoThatDo_DoesNotDoubleTheSeparator()
    {
        // The other half: an empty block is not what the NEXT one is separated from either, or
        // the gap between the two blocks that did paint is twice as wide as anywhere else.
        Assert.Equal(
            ["T", "\n\n", "Hello"],
            SummaryLayout.Flatten(SummaryMarkdown.Parse("# T\n\n```\n```\n\nHello")).Select(r => r.Text));
    }
}
