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
        $"TapScribe needs macOS {Minimum} or newer (this Mac runs macOS {running}).";
}
