namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// The Windows WIRING of the portable storage layer. The stores themselves are covered
/// cross-platform in the Core suite; what is Windows-only — and so lives here — is the
/// settings filename, which stayed a Windows-side constant through the hoist into Core
/// precisely because it is an operator-facing on-disk contract, not a portable default.
/// </summary>
public class TrayStoresTests
{
    [Fact]
    public void SettingsFileName_StaysTheOnDiskContract()
    {
        // Every operator's saved settings (and DPAPI token) live under this exact name in
        // %APPDATA%. It deliberately still says "windows-tray-bridge" after the
        // bridges/tray-bridge/ directory rename — a tidy-up that "finishes" the rename here
        // orphans them all, so a change to this constant needs a migration, not just a
        // green build. The generic meeting-state / meeting-history filenames went to Core;
        // this one did not, and that asymmetry is the point.
        Assert.Equal("windows-tray-bridge.json", TrayStores.SettingsFileName);
    }

    [Fact]
    public void Settings_IsWiredToTheAppDataFolder_UnderThatName()
    {
        // The tray's settings file is %APPDATA%\TapScribe\windows-tray-bridge.json — the
        // one place the platform's folder choice is spelled out.
        string expected = Path.Join(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "TapScribe",
            TrayStores.SettingsFileName);

        Assert.Equal(expected, TrayStores.Settings.FilePath);
    }
}
