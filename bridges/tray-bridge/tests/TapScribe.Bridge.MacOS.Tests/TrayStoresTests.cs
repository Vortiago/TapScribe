using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS WIRING of the portable storage layer (#419). The stores themselves are covered
/// cross-platform in the Core suite; what is Mac-only, and so lives here, is where the tray's
/// files go, what the settings file is called (both on-disk contracts with the operator
/// rather than portable defaults), and that the set can be built over a directory and a token
/// store of the caller's choosing.
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
    public void FallbackIdentity_IsAMacsOwn_NotTheWindowsSlug()
    {
        // The identity is the WAV filename slug and the key the Recorder attributes recordings
        // under. Core's frozen default is "windows-tray", which is correct WHERE IT IS FROZEN
        // and simply wrong on a Mac: an operator whose box offers no username would have their
        // recordings filed under a Windows tray they have never run. Same class of value as
        // the settings filename beside it - platform-side, operator-facing, and a change here
        // re-attributes every recording made under it rather than renaming anything.
        Assert.Equal("mac-tray", TrayStores.FallbackIdentity);
    }

    [Fact]
    public void LoadedSettings_CarryTheMacFallback_SoNoWindowsSlugCanReachADefaultDevice()
    {
        // The settings the shell actually runs on. A blank Speaker ID is normal (the field is
        // optional), and every blank one resolves through the fallback: the base identity a
        // tap streams under, and the label the default microphone row is named with. Loaded
        // through the real store, because the stamp is the store's job and asserting on a
        // hand-built BridgeSettings would prove nothing about the path the app takes.
        string directory = Path.Join(Path.GetTempPath(), $"tapscribe-mac-fallback-{Guid.NewGuid():n}");
        try
        {
            var stores = new TrayStores(directory, new RecordingTapTokenStore());
            BridgeSettings loaded = stores.Settings.Load();
            loaded.Identity = "";

            Assert.Equal("mac-tray", loaded.FallbackIdentity);
            Assert.DoesNotContain(
                "windows",
                loaded.DefaultDevices()[0].Identity,
                StringComparison.OrdinalIgnoreCase);
        }
        finally
        {
            if (Directory.Exists(directory))
                Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void ProductionStores_AllLiveInTheApplicationSupportFolder()
    {
        // Settings, restart-resume state and Past-meetings history: three files, one folder,
        // and the folder is the only thing the platform contributes to the last two. The
        // filenames come from the stores that own them rather than being retyped, so this
        // asserts the wiring and not a copy of their contracts.
        Assert.Equal(
            Path.Join(BridgeAppData.Directory, TrayStores.SettingsFileName),
            TrayStores.Production.Settings.FilePath);
        Assert.Equal(
            Path.Join(BridgeAppData.Directory, MeetingStateStore.StateFileName),
            TrayStores.Production.MeetingState.FilePath);
        Assert.Equal(
            Path.Join(BridgeAppData.Directory, MeetingHistoryStore.HistoryFileName),
            TrayStores.Production.MeetingHistory.FilePath);
    }

    [Fact]
    public void Stores_BuiltForADirectory_PutAllThreeFilesInIt()
    {
        // The reason the set is an instance at all: the shell takes one, so a test of the
        // shell points it at a temp directory. A static set would give a shell test no
        // choice but the operator's own files.
        string directory = Path.Join(Path.GetTempPath(), Path.GetRandomFileName());

        var stores = new TrayStores(directory, new RecordingTapTokenStore());

        Assert.Equal(Path.Join(directory, TrayStores.SettingsFileName), stores.Settings.FilePath);
        Assert.Equal(Path.Join(directory, MeetingStateStore.StateFileName), stores.MeetingState.FilePath);
        Assert.Equal(Path.Join(directory, MeetingHistoryStore.HistoryFileName), stores.MeetingHistory.FilePath);
    }

    [Fact]
    public void Stores_BuiltWithATapTokenStore_PersistTheTokenThroughIt()
    {
        // The half that actually protects the developer's login Keychain: a Save must reach
        // the token store it was GIVEN. If TrayStores hard-wired KeychainTapTokenStore, this
        // very test would have written a real Keychain item, and a Save of a blank token
        // would have DELETED one, with no temp-directory escape available for either.
        string directory = Path.Join(Path.GetTempPath(), Path.GetRandomFileName());
        var tokens = new RecordingTapTokenStore();
        var stores = new TrayStores(directory, tokens);

        try
        {
            stores.Settings.Save(new BridgeSettings { Token = "tap-token" });

            Assert.Equal("tap-token", tokens.LastWritten);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    /// <summary>An <see cref="ITapTokenStore"/> that keeps the secret in memory: what a test
    /// hands <see cref="TrayStores"/> in place of the Keychain.</summary>
    private sealed class RecordingTapTokenStore : ITapTokenStore
    {
        /// <summary>The last token handed to <see cref="Write"/>, or null if none was.</summary>
        internal string? LastWritten { get; private set; }

        /// <summary>The plaintext behind an at-rest value.</summary>
        /// <param name="atRest">What the settings file carried, which for an out-of-band
        /// secret is nothing.</param>
        public string Read(string? atRest) => LastWritten ?? "";

        /// <summary>Record the token and keep it out of the file, exactly as the Keychain
        /// store does.</summary>
        /// <param name="token">The plaintext token being saved.</param>
        public string? Write(string token)
        {
            LastWritten = token;
            return null;
        }
    }
}
