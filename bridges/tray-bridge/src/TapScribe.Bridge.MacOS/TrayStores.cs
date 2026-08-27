using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The one spelling of the tray's per-user data folder on macOS
/// (~/Library/Application Support/TapScribe), shared by the on-disk stores so the folder
/// cannot drift per store.
/// </summary>
internal static class BridgeAppData
{
    // Composed from the home directory rather than from SpecialFolder.ApplicationData, which does
    // resolve to ~/Library/Application Support on a Mac. The reason is the LANE: this assembly is
    // plain net10.0 and its tests run on ubuntu, where that call answers ~/.config. Composing the
    // path makes this the operator's Mac path on every host.
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
///
/// An INSTANCE with a <see cref="Production"/> singleton, rather than a static set, because the
/// settings store's token half is the login Keychain: a Save writes a real Keychain item and a Save
/// of a blank token DELETES one, with no temp-directory escape the way there is for the three
/// files. So the shell takes a set, and anything exercising it hands over a directory and a token
/// store it owns. <c>Production</c> is what the app runs.
/// </summary>
public sealed class TrayStores
{
    /// <summary>
    /// The on-disk settings filename, an operator-facing contract: a change orphans every operator's
    /// saved settings, so it needs a migration rather than a rename. Its own name rather than the
    /// Windows file's, which is stuck saying "windows-tray-bridge".
    /// </summary>
    public const string SettingsFileName = "macos-tray-bridge.json";

    /// <summary>
    /// The identity a tap streams under when neither the operator nor the Mac offers one: the WAV
    /// filename slug, and the key the Recorder attributes those recordings by. Frozen and
    /// operator-facing like the filename above, since changing it re-attributes every recording
    /// made under it as a new speaker.
    ///
    /// Core's own default is "windows-tray", which would file a Mac operator's recordings under a
    /// Windows tray they have never run. <see cref="BridgeSettingsStore"/> takes this and stamps it,
    /// so there is one place per platform rather than one per member that falls back to it.
    /// </summary>
    public const string FallbackIdentity = "mac-tray";

    /// <summary>The operator's own set: the three files in
    /// ~/Library/Application Support/TapScribe, with the tap token in their login Keychain.
    /// The only place the Keychain-backed token store is constructed.</summary>
    public static TrayStores Production { get; } =
        new(BridgeAppData.Directory, new KeychainTapTokenStore());

    /// <summary>Build the three stores over one directory.</summary>
    public TrayStores(string directory, ITapTokenStore tokens)
    {
        ArgumentNullException.ThrowIfNull(directory);
        ArgumentNullException.ThrowIfNull(tokens);
        Settings = new BridgeSettingsStore(tokens, directory, SettingsFileName, FallbackIdentity);
        MeetingState = new MeetingStateStore(directory);
        MeetingHistory = new MeetingHistoryStore(directory);
    }

    /// <summary>Connection settings + device selection, with the tap token wherever the
    /// token store this was built with puts it.</summary>
    public BridgeSettingsStore Settings { get; }

    /// <summary>The active meeting, for restart-resume (#107).</summary>
    public MeetingStateStore MeetingState { get; }

    /// <summary>The local Past-meetings list (#168).</summary>
    public MeetingHistoryStore MeetingHistory { get; }
}
