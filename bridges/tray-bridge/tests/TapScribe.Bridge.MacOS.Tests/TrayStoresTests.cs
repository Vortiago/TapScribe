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
        // ~/Library/Application Support/TapScribe, built from the home directory and Apple's
        // own path. Deliberately NOT SpecialFolder.ApplicationData, which every other
        // platform layer here reaches for: .NET follows the XDG spec on Unix and resolves
        // that to ~/.config, so a mirrored Windows line would ship the wrong folder with a
        // perfectly green test.
        string expected = Path.Join(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Library",
            "Application Support",
            "TapScribe");

        Assert.Equal(expected, BridgeAppData.Directory);
        Assert.DoesNotContain(".config", BridgeAppData.Directory);
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
}
