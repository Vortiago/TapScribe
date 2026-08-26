using System.Globalization;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// Reading a number back out of a Settings text field. A text field hands back whatever was
/// typed, so whether an entry is usable at all is a decision, and it is the same decision for
/// all three integer fields. What to DO about an unusable one is the window's
/// (<c>SettingsWindow.Number</c>): keep the value in force, put it back in the field, and say so.
/// </summary>
internal static class SettingsFields
{
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
