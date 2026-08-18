using TapScribe.Bridge.MacOS;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The shell's entry point, and the one decision in it: the macOS version floor. Below 14.4
/// there are no Core Audio process taps, so there is nothing to degrade to (ADR-0020) and the
/// app refuses rather than starting half-working. The refusal goes to stderr for now, and
/// becomes an NSAlert when there is a reason for an unsupported Mac to get as far as having a
/// window server connection.
///
/// The floor is decided BEFORE the menu bar is launched, which matters beyond tidiness:
/// launching reaches CoreAudio's HAL, the status bar and the operator's Keychain, which is
/// exactly the list of things an unsupported Mac cannot be asked for.
/// </summary>
internal static class Program
{
    private static int Main() => Run(MacOSProductVersion.Current(), Console.Error, TrayShell.RunMenuBar);

    /// <summary>The launch decision, with the ambient read, the output stream and the launch
    /// itself passed in so it can be driven for a macOS this box is not running and without
    /// AppKit, which cannot be constructed under a test host. Returns the process exit code:
    /// non-zero refuses the launch, and the reason goes to
    /// <paramref name="complaints"/>.</summary>
    /// <param name="running">This Mac's macOS version, or null when it could not be read.</param>
    /// <param name="complaints">Where a refusal is written.</param>
    /// <param name="launch">Starts the menu bar. Never reached on a Mac that was refused.</param>
    internal static int Run(Version? running, TextWriter complaints, Action launch)
    {
        ArgumentNullException.ThrowIfNull(complaints);
        ArgumentNullException.ThrowIfNull(launch);

        string? refusal = MacOSVersionFloor.Refusal(running);
        if (refusal is not null)
        {
            complaints.WriteLine(refusal);
            return 1;
        }

        // Returns when AppKit's run loop stops, which on a normal run it does not: quitting
        // terminates the process from inside the loop.
        launch();
        return 0;
    }
}
