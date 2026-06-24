namespace TapScribe.Bridge.Core;

/// <summary>
/// One device's level-gate tuning in operator units — the 0–100 <em>sensitivity</em>
/// slider plus hangover / pre-roll in milliseconds — as persisted per device. This is
/// the per-device counterpart of the engine-unit <see cref="GateOptions"/>: it converts
/// to one via <see cref="ToGateOptions"/> (sensitivity → linear RMS threshold through
/// <see cref="GateTuning"/>), so each capture pipeline can be tuned independently.
///
/// Per-device because a microphone and a system loopback want opposite sensitivity: the
/// mic picks up room noise and should open conservatively, while the loopback carries
/// quiet far-end speech and should open more readily. <see cref="DefaultForFlow"/>
/// encodes that split (ADR-0007); the mic default deliberately matches the legacy global
/// default so upgrading doesn't re-tune an existing microphone.
/// </summary>
public sealed record GateSettings(int Sensitivity, int HangoverMs, int PreRollMs)
{
    // The mic default is pinned to the legacy global default (≈0.02 RMS) expressed as a
    // slider, so the per-device migration is behaviour-preserving for a microphone. The
    // system default is a higher slider value → a lower threshold → opens on quieter
    // sound, which is what makes the far end of a meeting audible.
    private static readonly GateOptions LegacyDefault = new();
    private static readonly int MicSensitivity = GateTuning.ThresholdToSlider(LegacyDefault.OpenThreshold);
    private const int SystemSensitivity = 65;
    private static readonly int DefaultHangoverMs = (int)LegacyDefault.Hangover.TotalMilliseconds;
    private static readonly int DefaultPreRollMs = (int)LegacyDefault.PreRoll.TotalMilliseconds;

    /// <summary>
    /// The engine-unit gate this tuning maps to. Sensitivity is mapped to a linear RMS
    /// open threshold via <see cref="GateTuning"/> (which clamps to a band inside
    /// <c>[0, 1)</c>, so the result is always a tuning <see cref="LevelGate"/> accepts);
    /// hangover / pre-roll are clamped non-negative and carried through as durations.
    /// </summary>
    public GateOptions ToGateOptions() => new()
    {
        OpenThreshold = GateTuning.SliderToThreshold(Sensitivity),
        Hangover = TimeSpan.FromMilliseconds(Math.Max(0, HangoverMs)),
        PreRoll = TimeSpan.FromMilliseconds(Math.Max(0, PreRollMs)),
    };

    /// <summary>
    /// The sensible default tuning for a device of <paramref name="flow"/>: a render
    /// (system loopback) endpoint gets the more-sensitive default; everything else (a
    /// capture mic, or a pinned device whose flow we treat as mic-like) gets the
    /// behaviour-preserving mic default.
    /// </summary>
    public static GateSettings DefaultForFlow(DeviceFlow flow) => flow switch
    {
        DeviceFlow.Render => new(SystemSensitivity, DefaultHangoverMs, DefaultPreRollMs),
        _ => new(MicSensitivity, DefaultHangoverMs, DefaultPreRollMs),
    };
}
