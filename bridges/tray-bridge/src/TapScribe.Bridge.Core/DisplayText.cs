namespace TapScribe.Bridge.Core;

/// <summary>
/// Fitting operator-facing text into a budget the platform imposes.
///
/// Here rather than in a shell because both have the same budget problem for the same reason,
/// and got it right independently until one of them did not: a Windows tray tooltip caps at 63
/// characters, a macOS menu item is one line, and the text going into either is device names
/// and exception messages. Those carry paths, and an emoji or a rarer CJK glyph in a path is
/// two chars, so a cut at a fixed index can land between the halves of one and render as the
/// replacement box rather than as a truncation.
/// </summary>
public static class DisplayText
{
    /// <summary>The first <paramref name="limit"/> characters of <paramref name="text"/>,
    /// backing off one when that would split a surrogate pair.</summary>
    /// <param name="text">The text to fit.</param>
    /// <param name="limit">How many chars may be kept. Callers that append an ellipsis pass a
    /// budget already reduced by it.</param>
    /// <returns><paramref name="text"/> unchanged when it fits, otherwise the kept part.
    /// </returns>
    public static string Clamp(string text, int limit)
    {
        ArgumentNullException.ThrowIfNull(text);
        ArgumentOutOfRangeException.ThrowIfNegative(limit);
        if (text.Length <= limit)
            return text;

        // Backing off cannot itself split anything: a high surrogate at limit-1 means the pair
        // starts there, so keeping one fewer ends the string before it.
        return text[..(limit > 0 && char.IsHighSurrogate(text[limit - 1]) ? limit - 1 : limit)];
    }
}
