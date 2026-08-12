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
    /// the NUL terminator and padding a C string arrives with.</summary>
    public static Version Parse(string reading) => Version.Parse(reading.Trim().Trim('\0'));
}
