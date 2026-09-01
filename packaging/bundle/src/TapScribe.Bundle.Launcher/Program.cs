using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Launcher;

internal static class Program
{
    /// <summary>
    /// Entry point for the Bundle's tray Launcher.
    ///
    /// Single-instance by a per-user named mutex: two Launchers would race for port 8001
    /// and the second would die on EADDRINUSE with no console to say so. The layout is
    /// resolved from where this exe actually sits (so a moved or portable install still
    /// works) and the user profile.
    /// </summary>
    [STAThread]
    private static void Main()
    {
        // Local\ scope, not Global\: install is per-user (ADR-0015), so two different
        // users on one machine each get their own Recorder and their own data dir.
        using var single = new Mutex(initiallyOwned: true, "Local\\TapScribe.Bundle.Launcher", out bool isFirst);
        if (!isFirst)
        {
            MessageBox.Show(
                "TapScribe is already running — look for its icon in the notification area.",
                "TapScribe",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return;
        }

        ApplicationConfiguration.Initialize();

        BundleLayout layout;
        try
        {
            layout = BundleLayout.Resolve(
                AppContext.BaseDirectory,
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        }
        catch (Exception error) when (error is ArgumentException or IOException)
        {
            // No tray icon exists yet, so a dialog is the only way to be heard.
            MessageBox.Show(
                $"TapScribe could not work out where it is installed:\n\n{error.Message}",
                "TapScribe",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return;
        }

        Application.Run(new LauncherContext(layout));
    }
}
