using System.Drawing;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Windows.Tests;

/// <summary>
/// The settings dialog fits, at every text size an operator can be running.
///
/// This is the one test in the tray suite that is about pixels, and it exists because
/// nothing else could catch what it catches. The dialog used to place every control at a
/// literal coordinate and give every paragraph a literal height, which is correct at
/// exactly one scale: on a 150% display the font came back 1.6x taller while the constants
/// did not, so descriptions were truncated to one line, captions sat on top of their
/// inputs, "Test connection" lost half its caption and Save/Cancel fell off the bottom.
/// Every unit test passed, because none of them looked at a rectangle.
///
/// CI runs at 100%, so the scale is varied through the FONT rather than the display:
/// setting a bigger font on the form is what the accessibility text-size slider does, it
/// re-fires the same <c>PerformAutoScale</c> a high-DPI display does, and it needs no
/// particular hardware — the assertions hold identically on a 4K laptop and a headless
/// runner.
///
/// Three failures are worth distinguishing, and all three were really present:
/// <list type="bullet">
/// <item><b>Clipped</b> — the control's box is smaller than the text in it.</item>
/// <item><b>Overlapping</b> — two siblings occupy the same pixels.</item>
/// <item><b>Escaped</b> — a control hangs off the edge of its parent.</item>
/// </list>
///
/// No window is shown and no message loop is entered, per this assembly's rules; the
/// dialog is laid out with <see cref="Control.CreateControl"/> +
/// <see cref="Control.PerformLayout()"/>, which is enough because every size in it is a
/// measurement rather than a coordinate.
/// </summary>
public class SettingsFormLayoutTests
{
    /// <summary>
    /// Segoe UI 9pt is the metric the dialog's sizes are written in; the rest are what a
    /// scaled display and the text-size slider multiply that by. 14.4pt is the 150% case
    /// that was reported, and 18pt is past anything anyone has eyeballed — its job is to
    /// prove the layout holds where no one has looked.
    /// </summary>
    [Theory]
    [InlineData(9f)]
    [InlineData(11.25f)]
    [InlineData(14.4f)]
    [InlineData(18f)]
    public void NothingIsClippedOverlappingOrOffItsParent(float pointSize)
    {
        using var sta = new StaShell();
        LayoutSurvey survey = sta.Get(() => Survey(pointSize));

        // Before the faults: a layout test that measured nothing passes for the wrong
        // reason, and this one nearly shipped doing exactly that. Every control on an
        // unshown form reports Visible == false (the property is effective, not own-state),
        // so an inspection that skipped invisible controls skipped the entire dialog and was
        // green against a deliberately re-broken layout. The floor is well under the ~70
        // controls the dialog has, so it fails on "nothing was walked", not on a redesign.
        Assert.True(survey.Measured > 40, $"only {survey.Measured} controls were laid out — the survey found nothing to check");
        Assert.Empty(survey.Faults);
    }

    /// <summary>
    /// The dialog OPENS at its natural size rather than opening scrolled — no tab's content
    /// is taller than the body it was given.
    ///
    /// Its own test because it pins something the survey above does not, and something that
    /// turned out to be independent of it: with the layout framework-driven, deleting
    /// <c>AutoScaleMode = Font</c> breaks nothing the survey can see — the pages simply
    /// scroll, and nothing is clipped or hidden. What is worth pinning separately is that
    /// the dialog's declared <c>ClientSize</c> still covers its content, so adding a row
    /// without adjusting it is caught here rather than by an operator.
    ///
    /// Asserted only at the 9pt metric the dialog's sizes are written in, and that is not a
    /// weaker claim than checking several: a scaled display scales the ClientSize and the
    /// content by the same factor, so fitting here is what makes it fit there. Only the
    /// collapsed state counts — opening Advanced is MEANT to push the pin grid past the
    /// bottom edge, which is what the scroll is for.
    /// </summary>
    [Fact]
    public void EveryTabFitsTheDialogItOpensAt()
    {
        using var sta = new StaShell();
        IReadOnlyList<string> scrolled = sta.Get(() => Scrolled(9f));

        Assert.Empty(scrolled);
    }

    private static IReadOnlyList<string> Scrolled(float pointSize)
    {
        using SettingsForm form = Laid(pointSize, openAdvanced: false);
        var scrolled = new List<string>();

        foreach (TabPage tab in Tabs(form))
        {
            tab.PerformLayout();
            foreach (TableLayoutPanel body in tab.Controls.OfType<TableLayoutPanel>())
            {
                // The virtual scrollable extent against the visible one: what AutoScroll
                // would put a scrollbar over.
                int needed = body.DisplayRectangle.Height;
                if (needed > body.ClientSize.Height)
                    scrolled.Add($"{tab.Text} needs {needed}px of its {body.ClientSize.Height}px body");
            }
        }
        return scrolled;
    }

    private sealed record LayoutSurvey(int Measured, IReadOnlyList<string> Faults);

    private static LayoutSurvey Survey(float pointSize)
    {
        using SettingsForm form = Laid(pointSize, openAdvanced: true);

        var faults = new List<string>();
        int measured = 0;
        // Every tab, not just the first: a test that pokes only the selected page leaves
        // three quarters of the dialog unchecked.
        foreach (TabPage tab in Tabs(form))
        {
            tab.PerformLayout();
            Inspect(tab, faults, ref measured);
        }
        return new LayoutSurvey(measured, faults);
    }

    /// <summary>The dialog at one text size, laid out, with nothing shown.</summary>
    private static SettingsForm Laid(float pointSize, bool openAdvanced)
    {
        using var font = new Font("Segoe UI", pointSize);
        var form = new SettingsForm(new BridgeSettings { Name = "Atle" }, () => [], () => new NoDevices());
        // After construction, so the ctor's own auto-scale has run first and this re-scale is
        // the operator moving the text-size slider on an already-open dialog.
        form.Font = font;
        // Handles, not CreateControl: a TabControl sizes its pages from OnHandleCreated, and
        // CreateControl skips children of a form that was never shown, so without this every
        // TabPage keeps its 200x100 design size and the whole survey measures a layout that
        // never happened. Touching Handle creates it regardless of visibility.
        _ = form.Handle;
        foreach (Control control in Descendants(form))
            _ = control.Handle;

        // The pin grid is collapsed until the operator opens Advanced, and it is the block
        // with the most in it — so it is opened rather than left unmeasured. Same for the
        // status line, which is empty until something goes wrong and is exactly the wrapping
        // the fixed-height version got wrong.
        if (openAdvanced)
        {
            foreach (Control control in Descendants(form))
            {
                if (control is TableLayoutPanel { Visible: false } collapsed)
                    collapsed.Visible = true;
                if (control is Label { ForeColor.IsSystemColor: false, AutoSize: false } status)
                {
                    status.Text = "Could not list devices: the audio service is not running, so no "
                                + "endpoint could be enumerated on this machine right now.";
                }
            }
        }
        form.PerformLayout();
        return form;
    }

    private static IEnumerable<TabPage> Tabs(Control form) =>
        Descendants(form).OfType<TabControl>().SelectMany(t => t.TabPages.Cast<TabPage>());

    // Deliberately no Visible filter: on an unshown form that would be every control (see
    // the Measured assertion). Nothing in this dialog is hidden by design once Advanced has
    // been opened, and checking a hidden control against the layout it does have is harmless.
    private static void Inspect(Control container, List<string> faults, ref int measured)
    {
        var siblings = container.Controls.Cast<Control>().ToList();

        foreach (Control control in siblings)
        {
            measured++;

            if (Ink(control).Height > control.Height)
                faults.Add($"clipped: {Name(control)} shows {control.Height}px of {Ink(control).Height}px of text");

            foreach (string escape in Escapes(container, control))
                faults.Add(escape);

            foreach (Control other in siblings)
            {
                if (ReferenceEquals(other, control))
                    continue;
                Rectangle hit = Rectangle.Intersect(Ink(control), Ink(other));
                // One pixel of shared edge is a rounding artefact, not a collision.
                if (hit.Width > 1 && hit.Height > 1)
                    faults.Add($"overlapping: {Name(control)} and {Name(other)} share {hit.Width}x{hit.Height}px");
            }

            // A DataGridView's cells are its own business, not a layout of siblings.
            if (control is not DataGridView && control.Controls.Count > 0)
                Inspect(control, faults, ref measured);
        }
    }

    /// <summary>
    /// Whether <paramref name="control"/> hangs off the edge of <paramref name="container"/>
    /// in a way the operator cannot recover from.
    ///
    /// The asymmetry is the point. A tab body scrolls, so content taller than the page is
    /// what the scroll is FOR — a long Devices tab at 200% text is meant to be scrolled, not
    /// squeezed. Content WIDER than the page is a different thing: side-scrolling to finish
    /// reading a sentence is a layout failure whether or not a scrollbar appears, so it
    /// counts against a scrolling container too. A container that does not scroll clips
    /// outright, so both directions count there.
    /// </summary>
    private static IEnumerable<string> Escapes(Control container, Control control)
    {
        // A TabPage reports a client rectangle it has not been given until it is the selected
        // tab, so it is not a yardstick; the scrolling body inside it is.
        if (container is TabPage)
            yield break;

        Size room = container.ClientSize;
        if (control.Right > room.Width)
            yield return $"escaped sideways: {Name(control)} at {control.Bounds} needs {control.Right}px of {Name(container)}'s {room.Width}px width";

        bool scrolls = container is ScrollableControl { AutoScroll: true };
        if (!scrolls && control.Bottom > room.Height)
            yield return $"escaped below: {Name(control)} at {control.Bounds} needs {control.Bottom}px of {Name(container)}'s {room.Height}px height, which does not scroll";
    }

    /// <summary>
    /// What the control actually paints, which for a wrapping label is taller than the box
    /// it was given whenever the box is wrong — so measuring the text is what makes "this
    /// paragraph is sitting on the checkbox below it" visible at all.
    /// </summary>
    private static Rectangle Ink(Control control)
    {
        if (control is not Label { AutoSize: false } label || label.Text.Length == 0)
            return control.Bounds;

        int text = TextRenderer.MeasureText(
            label.Text,
            label.Font,
            new Size(Math.Max(label.Width, 1), int.MaxValue),
            TextFormatFlags.WordBreak).Height;
        return new Rectangle(label.Left, label.Top, label.Width, Math.Max(label.Height, text));
    }

    private static IEnumerable<Control> Descendants(Control root) =>
        root.Controls.Cast<Control>().SelectMany(c => new[] { c }.Concat(Descendants(c)));

    // Enough to find the control in the source: its type, and the caption or text a reader
    // can grep for.
    private static string Name(Control control)
    {
        string text = control.Text.Replace(Environment.NewLine, " ");
        if (text.Length > 40)
            text = text[..40] + "…";
        return text.Length > 0 ? $"{control.GetType().Name} \"{text}\"" : control.GetType().Name;
    }
}
