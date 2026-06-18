using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="GateTuning"/> — the pure map between the operator-facing
/// 0–100 "sensitivity" slider and the <see cref="GateOptions.OpenThreshold"/> linear
/// RMS amplitude. The slider exists so the Level-gate threshold (CONTEXT.md) is tunable
/// without exposing a raw 0.02 amplitude; this is the mapping the WinForms slider binds
/// to, kept pure so it is unit-tested with no UI.
/// </summary>
public class GateTuningTests
{
    [Fact]
    public void SliderToThreshold_IsMonotonic_MoreSensitivityMeansLowerThreshold()
    {
        // Higher sensitivity opens the gate on quieter sound, i.e. a LOWER threshold.
        Assert.True(GateTuning.SliderToThreshold(80) < GateTuning.SliderToThreshold(20));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(25)]
    [InlineData(50)]
    [InlineData(75)]
    [InlineData(100)]
    public void Slider_RoundTripsThroughThreshold(int sensitivity)
    {
        double threshold = GateTuning.SliderToThreshold(sensitivity);
        Assert.Equal(sensitivity, GateTuning.ThresholdToSlider(threshold));
    }

    [Fact]
    public void DefaultThreshold_MapsToASensibleMidSliderValue()
    {
        // The GateOptions default (0.02) shouldn't sit at either extreme of the slider.
        int slider = GateTuning.ThresholdToSlider(new GateOptions().OpenThreshold);
        Assert.InRange(slider, 30, 60);
    }
}
