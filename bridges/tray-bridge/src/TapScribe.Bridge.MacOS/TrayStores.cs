namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The one spelling of the tray's per-user data folder on macOS
/// (~/Library/Application Support/TapScribe), shared by the on-disk stores so the folder
/// cannot drift per store.
/// </summary>
internal static class BridgeAppData
{
    // Composed from the home directory and Apple's literal path rather than from
    // SpecialFolder.ApplicationData, which is what the Windows sibling uses: on Unix .NET
    // follows the XDG spec and resolves that to ~/.config. Mirroring the Windows line here
    // would put an operator's settings somewhere no Mac app looks, and no test that only
    // compared the two would notice.
    public static string Directory => Path.Join(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        "Library",
        "Application Support",
        "TapScribe");
}
