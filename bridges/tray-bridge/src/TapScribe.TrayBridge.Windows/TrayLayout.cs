using System.Drawing;

namespace TapScribe.TrayBridge.Windows;

/// <summary>
/// The three layout primitives the tray's windows share, so that neither of them is laid
/// out in literal pixels.
///
/// They exist because both windows had the same defect and it was reported from a 150%
/// display: a hand-placed control keeps its coordinate while the font it holds comes back
/// 1.6x taller, so the settings dialog truncated every description to one line and dropped
/// Save off the bottom edge, and the meeting window sliced its caption in half and clipped
/// the bottom off Copy. One home rather than a copy per form, because a second spelling of
/// "a label that wraps" is how one of them silently goes back to being wrong.
/// </summary>
internal static class TrayLayout
{
    /// <summary>
    /// Segoe UI 9pt at 96 DPI — the metrics every size in the tray's windows is written in.
    /// WinForms rescales a form's whole tree by the ratio of the runtime font metrics to
    /// these, which covers display DPI and the accessibility text-size slider alike.
    /// </summary>
    private static readonly SizeF DesignMetrics = new(7F, 15F);

    /// <summary>
    /// Turn on font-relative scaling for <paramref name="form"/>.
    ///
    /// Must be called with layout SUSPENDED, and the caller must resume afterwards: the
    /// <see cref="ContainerControl.AutoScaleDimensions"/> setter rescales IMMEDIATELY unless
    /// layout is suspended, so called on a live form it scales an empty tree and every size
    /// set afterwards — the form's own ClientSize included — stays in unscaled 96-DPI units.
    /// Resuming is what performs the scale, once, over the finished tree.
    /// </summary>
    internal static void UseFontScaling(ContainerControl form)
    {
        form.AutoScaleDimensions = DesignMetrics;
        form.AutoScaleMode = AutoScaleMode.Font;
    }

    /// <summary>
    /// A label that wraps to whatever width the layout gives it.
    ///
    /// It re-measures instead of declaring a height because WinForms has no AutoSize that
    /// wraps inside a <see cref="TableLayoutPanel"/> cell: a Percent column hands a control
    /// its width only AFTER the pass that decided the row height, so an AutoSize label
    /// reports its single-line width and never wraps at all. Docked to the top of an
    /// AutoSize row and re-measured on resize, the row grows to fit the text instead — a
    /// paragraph built any other way is the truncation bug again.
    ///
    /// All three triggers are load-bearing. Resize is the layout settling; TextChanged is
    /// the status lines, which get their text long after that; FontChanged is the operator
    /// moving the text-size slider, which moves the wrap without moving the width, so no
    /// resize fires for it.
    /// </summary>
    internal static Label Wrapped()
    {
        var label = new Label { AutoSize = false, Dock = DockStyle.Top, Margin = new Padding(3, 3, 3, 10) };
        label.Resize += (_, _) => Refit(label);
        label.TextChanged += (_, _) => Refit(label);
        label.FontChanged += (_, _) => Refit(label);
        return label;

        // Converges in one pass: Dock.Top fixes the width, so setting the height cannot
        // change what was measured. The guard is what stops the resize it triggers looping.
        // Floored at one line so an empty status still holds its row and what sits below it
        // doesn't hop as text arrives.
        static void Refit(Label label)
        {
            int needed = TextRenderer.MeasureText(
                label.Text,
                label.Font,
                new Size(Math.Max(label.Width, 1), int.MaxValue),
                TextFormatFlags.WordBreak).Height;
            needed = Math.Max(needed, TextRenderer.MeasureText("X", label.Font).Height);
            if (label.Height != needed)
                label.Height = needed;
        }
    }

    /// <summary>One explanatory paragraph in the quiet colour.</summary>
    internal static Label Paragraph(string text)
    {
        Label label = Wrapped();
        label.ForeColor = SystemColors.GrayText;
        label.Text = text;
        return label;
    }

    /// <summary>A button that fits its caption, in any language at any scale.</summary>
    internal static Button Action(string text) => new()
    {
        Text = text,
        AutoSize = true,
        AutoSizeMode = AutoSizeMode.GrowAndShrink,
        MinimumSize = new Size(90, 0),
    };
}
