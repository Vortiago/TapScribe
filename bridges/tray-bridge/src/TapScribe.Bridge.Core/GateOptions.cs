namespace TapScribe.Bridge.Core;

/// <summary>
/// Tuning for the <see cref="LevelGate"/> — the Bridge-side Mute. A system
/// loopback capture has no mute event, so the Bridge decides utterance
/// boundaries itself from the audio level. All three knobs are operator-tunable
/// (surfaced in the tray settings in a later PRD #99 slice) with sensible
/// defaults here.
/// </summary>
public sealed record GateOptions
{
    /// <summary>
    /// Open level, as normalised RMS amplitude in [0, 1) (a sample of
    /// magnitude <c>v</c> contributes <c>v / 32768</c>). A 20 ms frame whose RMS
    /// reaches this opens an utterance; sustained level below it (for
    /// <see cref="Hangover"/>) closes it. The default ~0.02 is roughly −34 dBFS:
    /// above a quiet room floor, below conversational speech.
    /// </summary>
    public double OpenThreshold { get; init; } = 0.02;

    /// <summary>
    /// How long the level must stay below <see cref="OpenThreshold"/> before the
    /// utterance closes. Bridges natural speech pauses so a breath mid-sentence
    /// doesn't chop the utterance in two. Rounded to whole 20 ms frames, floored
    /// at one frame.
    /// </summary>
    public TimeSpan Hangover { get; init; } = TimeSpan.FromMilliseconds(800);

    /// <summary>
    /// How much audio captured <em>before</em> the open is prepended to the
    /// utterance, so the leading consonant isn't clipped while the level was
    /// still climbing past the threshold. Rounded to whole 20 ms frames; 0
    /// disables pre-roll.
    /// </summary>
    public TimeSpan PreRoll { get; init; } = TimeSpan.FromMilliseconds(300);
}
