using AppKit;
using CoreGraphics;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// A display-only input level bar with a marker at the gate's open threshold: the Mac sibling
/// of WinForms' <c>LevelMeterBar</c> (#421).
///
/// Both numbers are RMS in [0,1] and both go through Core's <see cref="LevelMeterScale"/>, so
/// the bar and the gate agree about what "half way" means. That is the whole point of the
/// marker: the operator drags Sensitivity until the marker sits just under where their speech
/// peaks, and a bar on its own scale would make that judgement a lie.
///
/// Display-only, and deliberately not on the tap path. It is fed by an
/// <see cref="InputLevelMeter"/> over a throwaway capture, so a meter that fails or is denied
/// costs a still bar and nothing else.
/// </summary>
internal sealed class LevelMeterView : NSView
{
    // Allocated once: ColorWithAlphaComponent makes a fresh NSColor, and this repaints
    // several times a second while a meter is live.
    private static readonly NSColor Shut = NSColor.SystemGreen.ColorWithAlphaComponent(0.35f);

    private double _level;
    private double _threshold;

    internal LevelMeterView(CGRect frame)
        : base(frame)
    {
    }

    /// <summary>Current RMS, [0,1]. Set from the poll tick.</summary>
    internal double Level
    {
        get => _level;
        set => Repaint(ref _level, value);
    }

    /// <summary>The gate's open threshold as RMS, [0,1]. Set from the sensitivity slider.
    /// </summary>
    internal double Threshold
    {
        get => _threshold;
        set => Repaint(ref _threshold, value);
    }

    // Repaint only on a visible change: both of these are set several times a second for as
    // long as the window is open, and NSView has no dirty-region cleverness to spare us a
    // redraw that would paint the same pixels. One helper rather than the rule written twice.
    private void Repaint(ref double field, double value)
    {
        if (Math.Abs(value - field) < 0.001)
            return;
        field = value;
        NeedsDisplay = true;
    }

    public override void DrawRect(CGRect dirtyRect)
    {
        CGRect bounds = Bounds;

        // A control-coloured track rather than a literal, so the bar reads as part of the
        // window in either appearance. One path for both the fill and the stroke.
        NSBezierPath track = NSBezierPath.FromRect(bounds);
        NSColor.ControlBackground.Set();
        track.Fill();
        NSColor.Separator.Set();
        track.Stroke();

        // Cast because NFloat is its own struct rather than an alias for double: multiplying it
        // by Fraction's double promotes the expression to double, so coming back needs one.
        // CodeQL reads these as cs/useless-cast-to-self, which the compiler refuses outright
        // (CS0266) if they are removed. Dismiss the alert; do not "fix" it.
        nfloat inner = bounds.Width - 2;
        nfloat fill = (nfloat)(LevelMeterScale.Fraction(_level) * inner);
        nfloat marker = (nfloat)(LevelMeterScale.Fraction(_threshold) * inner);

        // Two greens rather than one: below the threshold the gate is shut, so the fill says
        // "heard, not recorded" and above it says "recording". One colour would leave the
        // marker as the only cue, and the marker is a hairline. Open or shut is Core's call
        // (LevelMeterScale.IsOpen) rather than a comparison of the fractions just computed:
        // the WinForms bar asks the same question and both must answer it identically.
        if (fill > 0)
        {
            (LevelMeterScale.IsOpen(_level, _threshold) ? NSColor.SystemGreen : Shut).Set();
            NSBezierPath.FromRect(new CGRect(1, 1, fill, bounds.Height - 2)).Fill();
        }

        NSColor.SystemYellow.Set();
        NSBezierPath.FromRect(new CGRect(1 + marker, 1, 2, bounds.Height - 2)).Fill();
    }
}

