using System.Drawing;
using System.Windows.Forms;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// What the summary pane actually paints. Core's <see cref="SummaryLayout"/> decides the runs
/// and is tested there; this is the other half, the mapping from a run to a WinForms font, and
/// it shipped uncovered while the whole decision lived in the shell.
///
/// The numbers are a contract with what operators already read: Core's ramp is a DELTA over a
/// body size, so a heading's absolute point size is arithmetic no test checked. The macOS shell
/// applies the same deltas, so a drift here is also a drift between the two shells.
///
/// A RichTextBox reports a selection's font only once it has a window handle, and it is
/// STA-affine, so every case runs through <see cref="StaShell"/> and forces the handle. No
/// parent, no form, no message loop: a control handle needs none of those.
/// </summary>
public class SummaryRichTextTests
{
    // SummaryRichText's own body size, restated on purpose: this is the reader's side of the
    // contract, so moving the constant has to mean moving a test.
    private const float BodySize = 10f;

    [Fact]
    public void Render_AHeading_IsBoldAndSixPointsOverBody()
    {
        using var sta = new StaShell();

        (float Size, FontStyle Style, float BodyPoints) painted = sta.Get(() =>
        {
            using RichTextBox box = Painted("# Title\n\nbody");
            Font heading = FontAt(box, box.Text.IndexOf('T'));
            Font body = FontAt(box, box.Text.IndexOf('b'));
            return (heading.Size, heading.Style, body.Size);
        });

        Assert.Equal(BodySize + 6, painted.Size);
        Assert.Equal(FontStyle.Bold, painted.Style);
        Assert.Equal(BodySize, painted.BodyPoints);
    }

    [Fact]
    public void Render_EmphasisInsideAHeading_KeepsBothFaces()
    {
        // The flags case from the painting side: exclusive faces would drop one, and the word
        // would read as body text in the middle of a heading.
        using var sta = new StaShell();

        FontStyle style = sta.Get(() =>
        {
            using RichTextBox box = Painted("# a *b*");
            return FontAt(box, box.Text.IndexOf('b')).Style;
        });

        Assert.Equal(FontStyle.Bold | FontStyle.Italic, style);
    }

    [Fact]
    public void Render_InlineCode_IsMonospacedAndShaded()
    {
        using var sta = new StaShell();

        (string Family, bool Shaded) painted = sta.Get(() =>
        {
            using RichTextBox box = Painted("plain `code` after");
            box.SelectionStart = box.Text.IndexOf("code", StringComparison.Ordinal);
            box.SelectionLength = 1;
            return (box.SelectionFont!.FontFamily.Name, box.SelectionBackColor != box.BackColor);
        });

        Assert.Equal("Consolas", painted.Family);
        Assert.True(painted.Shaded, "inline code was painted unshaded");
    }

    [Fact]
    public void Render_ABlockThatPaintsNothing_LeavesNoLeadingGap()
    {
        // The one deliberate change to what a Windows operator sees: an empty fence used to
        // emit its separator anyway, putting two blank lines at the top of the pane.
        using var sta = new StaShell();

        string text = sta.Get(() =>
        {
            using RichTextBox box = Painted("```\n```\n\nHello");
            return box.Text;
        });

        Assert.Equal("Hello", text);
    }

    [Fact]
    public void Render_Twice_PaintsTheSecondSummaryOnly()
    {
        // The pane is repainted on a poll tick, so a missing Clear would grow the text without
        // bound for as long as the meeting card is open.
        using var sta = new StaShell();

        string text = sta.Get(() =>
        {
            using RichTextBox box = Painted("first");
            SummaryRichText.Render(box, SummaryMarkdown.Parse("second"));
            return box.Text;
        });

        Assert.Equal("second", text);
    }

    // A handle-created box with the markdown already painted in. The caller disposes it, on the
    // STA thread that built it.
    private static RichTextBox Painted(string markdown)
    {
        var box = new RichTextBox();
        // Forces the window handle. Without one the selection properties report the control's
        // defaults rather than what Render wrote.
        _ = box.Handle;
        SummaryRichText.Render(box, SummaryMarkdown.Parse(markdown));
        return box;
    }

    // One character, so the range is uniform: SelectionFont is null over a mixed one.
    private static Font FontAt(RichTextBox box, int index)
    {
        Assert.True(index >= 0, "the text to measure was never painted");
        box.SelectionStart = index;
        box.SelectionLength = 1;
        return box.SelectionFont!;
    }
}
