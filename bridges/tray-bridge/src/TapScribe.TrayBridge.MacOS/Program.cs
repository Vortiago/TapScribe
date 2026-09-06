using TapScribe.Bridge.MacOS;
using TapScribe.Bundle.Core;
using TapScribe.Bundle.MacOS;

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
    private static int Main(string[] args)
    {
        // BEFORE anything else, and before AppKit is touched at all: this same binary is
        // re-invoked as its own parent-death watchdog (ADR-0024), and that invocation must not
        // build a menu bar, read settings or claim a status item. It waits for the tray to die
        // and kills the process group — see ParentDeathWatch.
        if (ReapRequest.Parse(args) is { } reap)
            return ParentDeathWatch.Run(reap, Console.Error);

        return Run(
            MacOSProductVersion.Current(),
            Console.Error,
            TrayShell.RunMenuBar,
            () => LegacyAppBundle.RemoveSuperseded(
                LegacyAppBundle.ContainingApp(AppContext.BaseDirectory), Console.Error.WriteLine));
    }

    /// <summary>The launch decision, with the ambient read, the output stream and the launch itself
    /// passed in so it can be driven for a macOS this box is not running and without AppKit, which
    /// cannot be constructed under a test host. Returns the process exit code: non-zero refuses the
    /// launch.</summary>
    /// <param name="running">This Mac's macOS version, or null when it could not be read.</param>
    /// <param name="tidyUp">Removes the <c>.app</c> this one replaced (ADR-0024). AFTER the
    /// version floor, so an unsupported Mac that is refusing to run does not delete the app
    /// the operator may still want to drag somewhere; and before the launch, which does not
    /// return.</param>
    internal static int Run(Version? running, TextWriter complaints, Action launch, Action tidyUp)
    {
        ArgumentNullException.ThrowIfNull(complaints);
        ArgumentNullException.ThrowIfNull(launch);
        ArgumentNullException.ThrowIfNull(tidyUp);

        string? refusal = MacOSVersionFloor.Refusal(running);
        if (refusal is not null)
        {
            complaints.WriteLine(refusal);
            return 1;
        }

        tidyUp();

        // Returns when AppKit's run loop stops, which on a normal run it does not: quitting
        // terminates the process from inside the loop.
        launch();
        return 0;
    }
}
