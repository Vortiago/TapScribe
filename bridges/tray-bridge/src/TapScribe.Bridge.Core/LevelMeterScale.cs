namespace TapScribe.Bridge.Core;

/// <summary>
/// The display axis for the Settings input-level meter: maps an RMS level (the reading
/// from <see cref="AudioLevel.Rms"/>) to a [0, 1] bar fraction. The axis is logarithmic
/// over the gate's threshold range — the same axis the 0–100 sensitivity slider rides —
/// so a level and a threshold drawn through this one function are on a shared scale and
/// the bar crossing the marker is exactly the gate opening. Louder is fuller: the floor
/// (<see cref="GateTuning.MinThreshold"/>) is 0, the ceiling
/// (<see cref="GateTuning.MaxThreshold"/>) is 1, and levels outside clamp.
///
/// Pure math, unit-tested without any UI; the WinForms meter is a thin painter over it.
/// </summary>
public static class LevelMeterScale
{
    /// <summary>The [0, 1] meter fraction for a linear RMS <paramref name="rms"/> (clamped
    /// to the gate's threshold range, then placed on its log axis — the very axis
    /// <see cref="GateTuning"/> maps the sensitivity slider through, via the shared
    /// <see cref="GateTuning.LogSpan"/>).</summary>
    public static double Fraction(double rms)
    {
        double clamped = Math.Clamp(rms, GateTuning.MinThreshold, GateTuning.MaxThreshold);
        return Math.Log(clamped / GateTuning.MinThreshold) / GateTuning.LogSpan;
    }

    /// <summary>Whether a level at <paramref name="rms"/> is at or past
    /// <paramref name="threshold"/>, which is what a meter's two-tone fill means: below, the
    /// gate is shut and the level is heard but not recorded.</summary>
    /// <remarks>Here rather than in each bar because both shells were deciding it separately,
    /// and in different arithmetic: one compared raw RMS and the other compared the fractions
    /// it had already computed for drawing. <see cref="Fraction"/> is monotonic so the two
    /// agreed, which is precisely the sort of agreement that stops holding without anyone
    /// noticing.</remarks>
    public static bool IsOpen(double rms, double threshold) => rms >= threshold;
}
