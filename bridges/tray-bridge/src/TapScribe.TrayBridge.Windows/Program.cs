namespace TapScribe.TrayBridge.Windows;

internal static class Program
{
    /// <summary>
    /// The single-instance name, shared with <c>TapScribe.iss</c>'s <c>AppMutex</c> so the
    /// installer can ask a running tray to close instead of failing on files in use.
    /// Changing it means changing that list too.
    /// </summary>
    internal const string InstanceMutex = "Local\\TapScribe.TrayBridge";

    /// <summary>The main entry point for the tray Bridge.</summary>
    [STAThread]
    private static void Main()
    {
        // ONE tray per OS (ADR-0022), which since the tray carries the host role is a
        // correctness rule and not just tidiness: two instances of a Bundle install would
        // each boot a Recorder and fight over port 8001, and the loser would show as
        // "already running from somewhere else" — its own sibling.
        //
        // Local\ scope, not Global\: install is per-user (ADR-0015), so two different
        // users on one machine each get their own tray, their own Recorder and their own
        // data dir. The name is shared across the bridge-only and Bundle installs
        // deliberately: they are the same executable, and running both is precisely the
        // two-icons outcome ADR-0022 rejects.
        using var single = new Mutex(initiallyOwned: true, InstanceMutex, out bool isFirst);
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
        Application.Run(new TrayContext());
    }
}
