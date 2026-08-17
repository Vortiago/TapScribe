using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS WIRING of the portable storage layer (#419). The stores themselves are covered
/// cross-platform in the Core suite; what is Mac-only, and so lives here, is where the tray's
/// files go and what the settings file is called, both of which are on-disk contracts with
/// the operator rather than portable defaults.
/// </summary>
public class TrayStoresTests
{
    [Fact]
    public void BridgeAppData_IsTheApplicationSupportFolder()
    {
        // ~/Library/Application Support/TapScribe on every host, which is the point: this
        // assembly's tests run on the ubuntu lane too, and SpecialFolder.ApplicationData
        // answers ~/.config there (it does answer the path below on a Mac, so the Windows
        // sibling's line is not wrong, just not lane-independent). Composing the path is
        // what lets this assertion run without [RequiresMacOS].
        string expected = Path.Join(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Library",
            "Application Support",
            "TapScribe");

        Assert.Equal(expected, BridgeAppData.Directory);
    }

    [Fact]
    public void SettingsFileName_StaysTheOnDiskContract()
    {
        // Every operator's saved settings live under this exact name in
        // ~/Library/Application Support/TapScribe, so a change here orphans them all and
        // needs a migration rather than an edit. Its own name, not the Windows file's: that
        // one is stuck saying "windows-tray-bridge" for a migration reason this platform has
        // no share in, and the two folders never meet anyway.
        Assert.Equal("macos-tray-bridge.json", TrayStores.SettingsFileName);
    }

    [Fact]
    public void TheThreeStores_AllLiveInTheApplicationSupportFolder()
    {
        // Settings, restart-resume state and Past-meetings history: three files, one folder,
        // and the folder is the only thing the platform contributes to the last two. The
        // filenames come from the stores that own them rather than being retyped, so this
        // asserts the wiring and not a copy of their contracts.
        Assert.Equal(
            Path.Join(BridgeAppData.Directory, TrayStores.SettingsFileName),
            TrayStores.Settings.FilePath);
        Assert.Equal(
            Path.Join(BridgeAppData.Directory, MeetingStateStore.StateFileName),
            TrayStores.MeetingState.FilePath);
        Assert.Equal(
            Path.Join(BridgeAppData.Directory, MeetingHistoryStore.HistoryFileName),
            TrayStores.MeetingHistory.FilePath);
    }
}
