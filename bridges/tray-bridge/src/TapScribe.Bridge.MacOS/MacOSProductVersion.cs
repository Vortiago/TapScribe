namespace TapScribe.Bridge.MacOS;

/// <summary>
/// What macOS this Mac is running, as Apple's own "product version" (the 14.4.1 spelling,
/// not the Darwin kernel version). This is the ambient edge in front of the pure
/// <see cref="MacOSVersionFloor"/>: the policy takes a version as a parameter so it can be
/// tested against releases nobody here runs, and only this class touches the OS.
/// </summary>
public static class MacOSProductVersion
{
    /// <summary>The <paramref name="reading"/> the OS handed back, as a version. Tolerates
    /// the NUL terminator and padding a C string arrives with, and answers
    /// <see cref="Unreadable"/> for anything it cannot make sense of.</summary>
    public static Version Parse(string reading) =>
        Version.TryParse(reading.Trim().Trim('\0'), out Version? version) ? version : Unreadable;

    // A Mac that will not say what it runs is not one this Bridge supports, so the
    // unreadable case is spelled as a version below every real macOS: the floor then
    // refuses it through the same path as a genuinely old Mac, and nothing on the launch
    // path has to handle an exception.
    private static Version Unreadable { get; } = new(0, 0);
}
