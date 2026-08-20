using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The display axis the input-level meter is painted on: a log map from RMS level to a
/// [0,1] bar fraction, riding the very same axis as the 0–100 sensitivity slider. The
/// alignment invariant below is the whole point — it guarantees the threshold marker lands
/// exactly under the slider thumb, so "the bar crossed the line" == "the gate would open".
/// </summary>
public class LevelMeterScaleTests
{
    [Fact]
    public void Fraction_SpansZeroToOne_AcrossTheGateThresholdRange()
    {
        Assert.Equal(0.0, LevelMeterScale.Fraction(GateTuning.MinThreshold), precision: 6);
        Assert.Equal(1.0, LevelMeterScale.Fraction(GateTuning.MaxThreshold), precision: 6);
    }

    [Fact]
    public void Fraction_ClampsOutsideTheGateRange()
    {
        Assert.Equal(0.0, LevelMeterScale.Fraction(0.0));                          // silence
        Assert.Equal(0.0, LevelMeterScale.Fraction(GateTuning.MinThreshold / 10));
        Assert.Equal(1.0, LevelMeterScale.Fraction(GateTuning.MaxThreshold * 10)); // clipping-loud
    }

    [Fact]
    public void Fraction_IsStrictlyMonotonic_LouderMeansFuller()
    {
        double prev = -1;
        for (double rms = GateTuning.MinThreshold; rms <= GateTuning.MaxThreshold; rms *= 1.2)
        {
            double f = LevelMeterScale.Fraction(rms);
            Assert.True(f > prev, $"fraction should increase with level at rms={rms}");
            prev = f;
        }
    }

    // The marker for a device tuned to sensitivity s sits at Fraction(SliderToThreshold(s)).
    // Because the marker is on the *level* axis (where the audio must reach to open the
    // gate), it lands at (100 - s) / 100 — moving opposite the sensitivity thumb: turning
    // sensitivity up (s -> 100) lowers the line toward empty, so quieter audio clears it.
    // That inverse is exactly the tuning gesture: raise sensitivity until speech clears it.
    [Theory]
    [InlineData(0)]
    [InlineData(25)]
    [InlineData(41)]
    [InlineData(65)]
    [InlineData(100)]
    public void Fraction_OfASlidersThreshold_PlacesTheLineByInverseSensitivity(int sensitivity)
    {
        double marker = LevelMeterScale.Fraction(GateTuning.SliderToThreshold(sensitivity));
        Assert.Equal((100 - sensitivity) / 100.0, marker, precision: 6);
    }

    [Fact]
    public void IsOpen_IsTrueExactlyWhenTheLevelReachesTheThreshold()
    {
        // The predicate both meters draw their two-tone fill from. Shared because the Mac bar
        // and the WinForms bar had each decided it for themselves, in different arithmetic: one
        // compared raw RMS, the other compared bar fractions. Fraction is monotonic so they
        // agreed, which is exactly the kind of agreement that stops being true quietly.
        Assert.False(LevelMeterScale.IsOpen(0.004, 0.005));
        Assert.True(LevelMeterScale.IsOpen(0.005, 0.005));
        Assert.True(LevelMeterScale.IsOpen(0.006, 0.005));
    }

    [Fact]
    public void IsOpen_AgreesWithTheFractionsTheBarsDraw()
    {
        // The property that let the two spellings coexist, pinned so a change to Fraction that
        // broke it would fail here rather than in one shell's painting.
        Assert.Equal(
            LevelMeterScale.IsOpen(0.02, 0.01),
            LevelMeterScale.Fraction(0.02) >= LevelMeterScale.Fraction(0.01));
    }
}
