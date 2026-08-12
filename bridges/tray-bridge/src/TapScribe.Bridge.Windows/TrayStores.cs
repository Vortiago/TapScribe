using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// The one spelling of the tray's per-user data folder (%APPDATA%\TapScribe),
/// shared by the three on-disk stores (settings / meeting state / meeting
/// history) so the folder name cannot drift per store.
/// </summary>
internal static class BridgeAppData
{
    public static string Directory => Path.Join(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "TapScribe");
}

/// <summary>
/// The Windows wiring of the portable storage layer: the three Core stores, each bound to
/// %APPDATA%\TapScribe, with DPAPI supplying the tap token's meaning at rest. Everything
/// platform-specific about where the tray's files live and how its one secret is protected
/// is decided here and nowhere else — the stores themselves are OS-agnostic.
/// </summary>
public static class TrayStores
{
    /// <summary>
    /// The on-disk settings filename — an operator-facing contract predating the
    /// bridges/tray-bridge/ directory rename. It deliberately still says
    /// "windows-tray-bridge" and is deliberately WINDOWS-side, not a Core constant:
    /// renaming it orphans every operator's saved settings (including the DPAPI-protected
    /// token), so a change here needs a migration, not a rename.
    /// </summary>
    public const string SettingsFileName = "windows-tray-bridge.json";

    /// <summary>Connection settings + device selection, token protected by DPAPI.</summary>
    public static BridgeSettingsStore Settings { get; } =
        new(new DpapiTapTokenStore(), BridgeAppData.Directory, SettingsFileName);

    /// <summary>The active meeting, for restart-resume (#107).</summary>
    public static MeetingStateStore MeetingState { get; } = new(BridgeAppData.Directory);

    /// <summary>The local Past-meetings list (#168).</summary>
    public static MeetingHistoryStore MeetingHistory { get; } = new(BridgeAppData.Directory);
}
