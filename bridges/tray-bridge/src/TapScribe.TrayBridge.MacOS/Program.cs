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
    private static int Main() => Run(MacOSProductVersion.Current(), Console.Error);

    /// <summary>The launch decision, with the ambient read and the output stream passed in
    /// so it can be driven for a macOS this box is not running. Returns the process exit
    /// code: non-zero refuses the launch, and the reason goes to
    /// <paramref name="complaints"/>.</summary>
    internal static int Run(Version? running, TextWriter complaints)
    {
        string? refusal = MacOSVersionFloor.Refusal(running);
        if (refusal is null)
            return 0;

        complaints.WriteLine(refusal);
        return 1;
    }
}
