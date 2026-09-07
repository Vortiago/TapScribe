namespace TapScribe.Bundle.Core;

/// <summary>
/// Removes the <c>.app</c> TapScribe used to install itself as, on first launch of the one
/// that replaced it (ADR-0024).
///
/// The rename to <c>TapScribe.app</c> is what lets a Bundle upgrade a bridge-only install in
/// place — but <c>installer</c> overwrites BY PATH and never removes what a previous
/// receipt put somewhere else, so an operator upgrading from the old <c>.pkg</c> ends up
/// with the new app BESIDE an orphaned <c>TapScribe.TrayBridge.MacOS.app</c>: two trays in
/// the Applications folder, one of them dead, which is the exact outcome the rename exists
/// to prevent.
///
/// A first-launch removal rather than a <c>pkg</c> postinstall script, for the reason
/// ADR-0024 already rejects those: a script runs as root, and this way the rule is here,
/// in the assembly the Linux CI leg tests.
///
/// Deliberately narrow. It removes ONE known path, only when the app doing the removing is
/// itself the installed one, and it never touches anything it did not ship. A cleanup that
/// went looking for "things that look like old TapScribes" would eventually find something
/// that was not one.
/// </summary>
public static class LegacyAppBundle
{
    /// <summary>Where the bridge-only <c>.pkg</c> installed the tray before the rename.</summary>
    public const string LegacyName = "TapScribe.TrayBridge.MacOS.app";

    /// <summary>What it installs now. The removal is conditioned on running from here.</summary>
    public const string CurrentName = "TapScribe.app";

    /// <summary>The only folder either has ever been installed into.</summary>
    public const string ApplicationsDirectory = "/Applications";

    /// <summary>
    /// Remove the superseded bundle if this launch is the installed successor and the old
    /// one is still there. Answers the path removed, or null when there was nothing to do.
    ///
    /// Never throws: a tray that will not start because it could not tidy up is strictly
    /// worse than two icons in a folder. A failure is logged and the launch continues.
    /// </summary>
    /// <param name="runningApp">The <c>.app</c> this process is running from, or null when
    /// it is not running from one at all.</param>
    /// <param name="applicationsDirectory">Overridable for the tests only.</param>
    /// <param name="log">Where the removal, or the reason there was none, is said.</param>
    public static string? RemoveSuperseded(
        string? runningApp, Action<string> log, string applicationsDirectory = ApplicationsDirectory)
    {
        ArgumentNullException.ThrowIfNull(log);

        // Running from a build output, a `dotnet run`, or anywhere but /Applications. A
        // developer with the old app installed must not have it deleted by a debug launch,
        // and a tray started from a Downloads folder is not the successor to anything.
        string installed = Path.Join(applicationsDirectory, CurrentName);
        if (runningApp is null || !PathsMatch(runningApp, installed))
            return null;

        string legacy = Path.Join(applicationsDirectory, LegacyName);
        if (!Directory.Exists(legacy))
            return null;

        try
        {
            Directory.Delete(legacy, recursive: true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // /Applications is admin-writable and the operator may not be one, or a file
            // inside is open. Say so once and carry on: the cost is the second icon this
            // was meant to remove, and the operator can drag it to the Trash.
            log($"could not remove the superseded {legacy}: {error.Message}");
            return null;
        }

        log($"removed the superseded {legacy}.");
        return legacy;
    }

    /// <summary>
    /// The <c>.app</c> a directory sits inside, or null when it sits inside none.
    ///
    /// Climbs rather than assuming a depth, for the reason
    /// <see cref="BundleLayout.MacOSPayload"/> does: the SDK has moved managed assemblies
    /// between <c>Contents/MonoBundle</c> and <c>Contents/MacOS</c>.
    /// </summary>
    public static string? ContainingApp(string baseDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(baseDirectory);

        for (DirectoryInfo? dir = new(Path.GetFullPath(baseDirectory)); dir is not null; dir = dir.Parent)
        {
            if (dir.Name.EndsWith(".app", StringComparison.Ordinal))
                return dir.FullName;
        }

        return null;
    }

    /// <summary>Compared as paths rather than as strings: a trailing separator, or a
    /// symlinked /Applications, must not make the successor fail to recognise itself.</summary>
    private static bool PathsMatch(string a, string b) =>
        string.Equals(
            Path.TrimEndingDirectorySeparator(Path.GetFullPath(a)),
            Path.TrimEndingDirectorySeparator(Path.GetFullPath(b)),
            StringComparison.Ordinal);
}
