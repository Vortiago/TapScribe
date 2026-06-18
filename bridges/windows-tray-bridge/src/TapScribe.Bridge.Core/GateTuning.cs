namespace TapScribe.Bridge.Core;

/// <summary>
/// Maps the operator-facing 0–100 <em>sensitivity</em> slider to the Level gate's
/// <see cref="GateOptions.OpenThreshold"/> (a linear RMS amplitude) and back. Higher
/// sensitivity = opens on quieter sound = a lower threshold. The mapping is logarithmic
/// because perceived loudness is, so equal slider steps feel like equal loudness steps.
/// Pure math, unit-tested without any UI; the WinForms slider is a thin binding over it.
/// </summary>
public static class GateTuning
{
    /// <summary>Most-sensitive end (slider 100): the lowest threshold (~-60 dBFS).</summary>
    public const double MinThreshold = 0.001;

    /// <summary>Least-sensitive end (slider 0): the highest threshold (~-14 dBFS).</summary>
    public const double MaxThreshold = 0.2;

    private static double LogSpan => Math.Log(MaxThreshold / MinThreshold);

    /// <summary>The linear RMS threshold for a 0–100 sensitivity (clamped).</summary>
    public static double SliderToThreshold(int sensitivity)
    {
        int s = Math.Clamp(sensitivity, 0, 100);
        // s=100 -> exponent 0 -> MinThreshold; s=0 -> exponent 1 -> MaxThreshold.
        return MinThreshold * Math.Pow(MaxThreshold / MinThreshold, (100 - s) / 100.0);
    }

    /// <summary>The 0–100 sensitivity for a linear RMS threshold (clamped to range).</summary>
    public static int ThresholdToSlider(double threshold)
    {
        double t = Math.Clamp(threshold, MinThreshold, MaxThreshold);
        double sensitivity = 100.0 * (1.0 - (Math.Log(t / MinThreshold) / LogSpan));
        return Math.Clamp((int)Math.Round(sensitivity), 0, 100);
    }
}
