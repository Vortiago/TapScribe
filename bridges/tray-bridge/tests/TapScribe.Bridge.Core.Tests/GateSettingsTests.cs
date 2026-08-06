using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="GateSettings"/> — one device's level-gate tuning in operator
/// units (the 0–100 sensitivity slider + hangover / pre-roll in ms) and its conversion
/// to the engine-unit <see cref="GateOptions"/> the <see cref="LevelGate"/> consumes.
/// The per-device split (mic less sensitive than a system loopback) is the model behind
/// ADR-0007; this pins the defaults and the slider→threshold mapping with no UI.
/// </summary>
public class GateSettingsTests
{
    [Fact]
    public void ToGateOptions_MapsSensitivityThroughGateTuning_AndMillisecondsToTimeSpans()
    {
        var settings = new GateSettings(Sensitivity: 70, HangoverMs: 500, PreRollMs: 250);

        GateOptions options = settings.ToGateOptions();

        Assert.Equal(GateTuning.SliderToThreshold(70), options.OpenThreshold);
        Assert.Equal(TimeSpan.FromMilliseconds(500), options.Hangover);
        Assert.Equal(TimeSpan.FromMilliseconds(250), options.PreRoll);
    }

    [Fact]
    public void ToGateOptions_AlwaysProducesAGateLevelGateAccepts()
    {
        // Slider extremes map into GateTuning's [Min,Max] threshold band, which is inside
        // the [0,1) LevelGate requires — so building a gate from any slider never throws.
        foreach (int sensitivity in new[] { 0, 50, 100 })
            _ = new LevelGate(new GateSettings(sensitivity, HangoverMs: 0, PreRollMs: 0).ToGateOptions());
    }

    [Fact]
    public void DefaultForFlow_SystemLoopbackIsMoreSensitiveThanTheMicrophone()
    {
        // The whole point of per-device tuning: the system-loopback gate opens on quieter
        // sound (a LOWER RMS threshold) so the far end is captured, while the mic stays
        // less sensitive so room noise doesn't over-trigger it.
        double mic = GateSettings.DefaultForFlow(DeviceFlow.Capture).ToGateOptions().OpenThreshold;
        double system = GateSettings.DefaultForFlow(DeviceFlow.Render).ToGateOptions().OpenThreshold;

        Assert.True(system < mic, $"system loopback threshold {system} should be below mic threshold {mic}");
    }

    [Fact]
    public void DefaultForFlow_MicKeepsTheHistoricalGateDefaults()
    {
        // Upgrading must not silently re-tune an existing mic: the capture default still
        // maps to roughly the legacy global default (≈0.02 RMS) and keeps its hangover /
        // pre-roll, so a meeting sounds the same as before per-device tuning landed.
        GateOptions mic = GateSettings.DefaultForFlow(DeviceFlow.Capture).ToGateOptions();
        var legacy = new GateOptions();

        Assert.Equal(legacy.OpenThreshold, mic.OpenThreshold, precision: 2);
        Assert.Equal(legacy.Hangover, mic.Hangover);
        Assert.Equal(legacy.PreRoll, mic.PreRoll);
    }
}
