using TapScribe.Bridge.MacOS;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The shell's entry point. Today it is only the macOS version floor: below 14.4 there are
/// no Core Audio process taps, so there is nothing to degrade to (ADR-0020) and the app
/// refuses rather than starting half-working. The menu bar, the meeting bracket and the
/// Settings window land in later slices of #419, and the refusal becomes an NSAlert when
/// there is an AppKit application to raise one from.
/// </summary>
internal static class Program
{
    private static int Main()
    {
        string? refusal = MacOSVersionFloor.Refusal(MacOSProductVersion.Current());
        if (refusal is null)
            return 0;

        Console.Error.WriteLine(refusal);
        return 1;
    }
}
