namespace TapScribe.Bridge.Core;

/// <summary>
/// What the two Settings dialogs accept, named once.
///
/// Both shells enforce these in their own UI tier, which carries no unit tests, so a drift
/// between them would ship silently and the two dialogs would accept different ranges for one
/// settings file.
///
/// Sensitivity is deliberately absent: <see cref="SettingsDraft.ToSettings"/> clamps it, so Core
/// already owns it and a shell's slider bounds are cosmetic. These three have no such clamp,
/// which leaves the shells as their only enforcement.
/// </summary>
public static class SettingsBounds
{
    /// <summary>Lowest Recorder port a dialog accepts.</summary>
    public const int PortMin = 1;

    /// <summary>Highest Recorder port a dialog accepts.</summary>
    public const int PortMax = 65535;

    /// <summary>Longest gate hangover a dialog accepts, in milliseconds.</summary>
    public const int HangoverMaxMs = 5000;

    /// <summary>Longest gate pre-roll a dialog accepts, in milliseconds.</summary>
    public const int PreRollMaxMs = 2000;

    /// <summary>How long a connection probe may take before it is abandoned. The Recorder is
    /// usually on the same machine or the same LAN, and an operator staring at a spinner learns
    /// nothing a refusal would not tell them sooner.</summary>
    public static readonly TimeSpan ConnectionTestTimeout = TimeSpan.FromSeconds(15);
}
