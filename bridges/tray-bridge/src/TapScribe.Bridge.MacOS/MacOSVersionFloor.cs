namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS version the Mac tray Bridge requires, and the refusal shown below it.
/// 14.4 is the release that opened Core Audio process taps to an ordinary app, which is
/// how this Bridge captures system audio at all (ADR-0020) - so an older Mac gets a clear
/// refusal rather than a half-working app.
/// </summary>
public static class MacOSVersionFloor
{
    /// <summary>The oldest macOS this Bridge runs on.</summary>
    public static Version Minimum { get; } = new(14, 4);

    /// <summary>
    /// Why <paramref name="running"/> cannot run this Bridge, or <c>null</c> when it can.
    /// </summary>
    public static string? Refusal(Version running) =>
        MajorMinor(running) >= MajorMinor(Minimum)
            ? null
            : $"TapScribe needs macOS {Minimum} or newer (this Mac runs macOS {running}).";

    // Both sides are cut to major.minor before comparing, because the floor is a
    // major.minor release and System.Version leaves an unstated component at -1: an
    // uncut Version(14, 4) sorts BELOW Version(14, 4, 0), so a Mac on 14.4.0 would be
    // refused by a floor spelled with three components. Cutting both makes the answer
    // independent of how either side happens to be written.
    private static Version MajorMinor(Version version) => new(version.Major, version.Minor);
}
