using TapScribe.Bridge.Core;

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

/// <summary>
/// The macOS wiring of the portable storage layer. Everything platform-specific about where
/// the tray's files live and how its one secret is protected is decided here and nowhere
/// else; the stores themselves are OS-agnostic.
/// </summary>
public static class TrayStores
{
    /// <summary>
    /// The on-disk settings filename, an operator-facing contract: a change orphans every
    /// operator's saved settings, so it needs a migration rather than a rename. Its own
    /// name rather than the Windows file's, which is stuck saying "windows-tray-bridge"
    /// for a migration reason this platform has no share in.
    /// </summary>
    public const string SettingsFileName = "macos-tray-bridge.json";

    /// <summary>Connection settings + device selection, with the tap token in the Keychain
    /// rather than in the file.</summary>
    public static BridgeSettingsStore Settings { get; } =
        new(new KeychainTapTokenStore(), BridgeAppData.Directory, SettingsFileName);

    /// <summary>The active meeting, for restart-resume (#107).</summary>
    public static MeetingStateStore MeetingState { get; } = new(BridgeAppData.Directory);

    /// <summary>The local Past-meetings list (#168).</summary>
    public static MeetingHistoryStore MeetingHistory { get; } = new(BridgeAppData.Directory);
}
