namespace TapScribe.TrayBridge;

internal static class Program
{
    /// <summary>The main entry point for the tray Bridge.</summary>
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new TrayContext());
    }
}
