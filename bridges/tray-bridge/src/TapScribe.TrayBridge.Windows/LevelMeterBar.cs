using System.ComponentModel;
using System.Drawing;
using System.Windows.Forms;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.Windows;

/// <summary>
/// A horizontal input-level meter (issue #152): a fill that tracks the current RMS
/// <see cref="Level"/> with a vertical marker at the gate's open <see cref="Threshold"/>,
/// both placed on the shared log axis (<see cref="LevelMeterScale"/>) so the fill reaching
/// the marker is exactly the gate opening. The fill goes vivid once it clears the line, so
/// "am I above the threshold?" is answerable at a glance.
///
/// Display only: it owns no audio. The Settings dialog pushes <see cref="Level"/> from a
/// display-only <see cref="InputLevelMeter"/> on a UI timer and <see cref="Threshold"/>
/// from the sensitivity slider. All the scale math is in the tested core; this is paint
/// only, which is why the TrayBridge UI stays untested by convention.
/// </summary>
internal sealed class LevelMeterBar : Control
{
    // Process-lifetime GDI objects (the idiomatic WinForms cache): the bar repaints on every
    // timer tick while audio moves, so allocating a brush/pen per paint would churn ~160
    // finalizable GDI handles/sec across the two bars. The border reuses framework SystemPens.
    private static readonly Brush TrackBrush = new SolidBrush(Color.FromArgb(40, 40, 40));
    private static readonly Brush BelowBrush = new SolidBrush(Color.FromArgb(70, 110, 70));
    private static readonly Brush AboveBrush = new SolidBrush(Color.LimeGreen);
    private static readonly Pen MarkerPen = new(Color.Gold, 2);

    private double _level;     // current RMS, [0, 1]
    private double _threshold; // gate open threshold RMS, [0, 1]

    public LevelMeterBar()
    {
        DoubleBuffered = true; // no flicker as the bar repaints on every timer tick
        ResizeRedraw = true;
    }

    /// <summary>The live RMS level to fill to. Repaints on change.</summary>
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)] // runtime-only; never designer-set
    public double Level
    {
        get => _level;
        set
        {
            if (_level.Equals(value))
                return;
            _level = value;
            Invalidate();
        }
    }

    /// <summary>The gate's open threshold (RMS), drawn as the marker line. Repaints on change.</summary>
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)] // runtime-only; never designer-set
    public double Threshold
    {
        get => _threshold;
        set
        {
            if (_threshold.Equals(value))
                return;
            _threshold = value;
            Invalidate();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        int inner = Width - 2; // leave a 1px border each side

        g.FillRectangle(TrackBrush, 0, 0, Width, Height);

        int fillW = (int)Math.Round(LevelMeterScale.Fraction(_level) * inner);
        // Vivid once the level clears the gate's line. Core's call, not this bar's: the
        // AppKit sibling asks the same question and both must answer it identically.
        bool open = LevelMeterScale.IsOpen(_level, _threshold);
        g.FillRectangle(open ? AboveBrush : BelowBrush, 1, 1, fillW, Height - 2);

        int markerX = 1 + (int)Math.Round(LevelMeterScale.Fraction(_threshold) * inner);
        g.DrawLine(MarkerPen, markerX, 0, markerX, Height);

        g.DrawRectangle(SystemPens.ControlDark, 0, 0, Width - 1, Height - 1);
    }
}
