using System.Globalization;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Reading a number back out of a Settings text field. A text field hands back whatever was
/// typed, so what a nonsense value means is a decision, and it is the same decision for all
/// three integer fields: keep what was already saved.
/// </summary>
internal static class SettingsFields
{
    /// <summary>The number in <paramref name="text"/>, or <paramref name="fallback"/> when it
    /// is not one or is outside the range the field accepts.</summary>
    /// <param name="text">Whatever the operator left in the field.</param>
    /// <param name="fallback">The value currently in force, kept when the entry is unusable:
    /// a mistyped port is a typo, and clamping it to the nearest legal value would tell the
    /// operator their entry was accepted.</param>
    /// <param name="min">The lowest value the field accepts.</param>
    /// <param name="max">The highest value the field accepts.</param>
    internal static int Int(string? text, int fallback, int min, int max) =>
        TryInt(text, min, max, out int value) ? value : fallback;

    /// <summary>Whether <paramref name="text"/> is a number this field accepts, and what it is.
    /// One read rather than two, so a caller that both takes the value and reports the rejection
    /// cannot disagree with itself about which happened.</summary>
    /// <param name="text">Whatever the operator left in the field.</param>
    /// <param name="min">The lowest value the field accepts.</param>
    /// <param name="max">The highest value the field accepts.</param>
    /// <param name="value">The number, when the entry is usable.</param>
    internal static bool TryInt(string? text, int min, int max, out int value)
    {
        // Invariant and digits-only: a port is not a quantity, so accepting a thousands
        // separator would make whether "8,001" parses at all depend on the operator's locale.
        bool parsed = int.TryParse(
            text?.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out value);
        return parsed && value >= min && value <= max;
    }
}
