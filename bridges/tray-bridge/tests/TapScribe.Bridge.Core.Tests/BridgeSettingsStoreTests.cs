using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Round-trips the tray's settings through a real file under an injected directory, with
/// the tap token's at-rest translation supplied by an injected <see cref="ITapTokenStore"/>.
/// This is the PORTABLE half of the storage layer — a platform contributes only the token
/// store behind that seam (DPAPI on Windows, the Keychain on macOS) — so every persistence
/// rule is pinned here and runs on every OS instead of only on the Windows CI job.
/// </summary>
[Collection(EnvironmentSeedCollection.Name)] // Load's fallback reads the TAPSCRIBE_* vars
public class BridgeSettingsStoreTests : IDisposable
{
    private readonly string _dir =
        Path.Join(Path.GetTempPath(), $"tapscribe-settings-{Guid.NewGuid():N}");

    private BridgeSettingsStore Store(ITapTokenStore tokens) => new(tokens, _dir, "settings.json");

    [Fact]
    public void SaveThenLoad_RoundTripsEveryField()
    {
        BridgeSettingsStore store = Store(new FakeTapTokenStore());
        var micGate = new GateSettings(Sensitivity: 45, HangoverMs: 800, PreRollMs: 300);
        var original = new BridgeSettings
        {
            Host = "rec.example",
            Port = 9100,
            Tls = true,
            AllowSelfSignedCert = true,
            Identity = "alice",
            Name = "Alice B",
            ProcessOnEnd = false,
            Token = "tok-xyz",
            Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "My Mic", micGate)],
        };

        store.Save(original);
        BridgeSettings loaded = store.Load();

        Assert.Equal("rec.example", loaded.Host);
        Assert.Equal(9100, loaded.Port);
        Assert.True(loaded.Tls);
        Assert.True(loaded.AllowSelfSignedCert); // the insecure opt-in survives the file
        Assert.Equal("alice", loaded.Identity);
        Assert.Equal("Alice B", loaded.Name);
        Assert.False(loaded.ProcessOnEnd); // the record-only opt-out survives the file
        Assert.Equal("tok-xyz", loaded.Token);

        var device = Assert.IsType<DeviceSelection.FollowDefault>(Assert.Single(loaded.Devices));
        Assert.Equal(DeviceFlow.Capture, device.Flow);
        Assert.Equal("mic", device.Identity);
        Assert.Equal("My Mic", device.Name);
        Assert.Equal(micGate, device.Gate);
    }

    [Fact]
    public void Save_NeverWritesTheTokenInCleartext()
    {
        // This is the test behind the README's security claim: the plaintext token must
        // never reach the file, only whatever opaque value the platform's token store
        // hands back. A change that serialised Token directly would fail here.
        const string secret = "super-secret-tap-token-abc123";
        BridgeSettingsStore store = Store(new FakeTapTokenStore());

        store.Save(new BridgeSettings { Host = "rec.example", Token = secret });
        string json = File.ReadAllText(store.FilePath);

        Assert.DoesNotContain(secret, json);        // plaintext token never on disk
        Assert.DoesNotContain("\"Token\"", json);   // the plaintext property isn't serialised
        Assert.Contains("ProtectedToken", json);    // only the store's opaque value is persisted
    }

    [Fact]
    public void Save_WhenTheTokenStoreKeepsTheSecretOutOfBand_WritesNoTokenKeyAtAll()
    {
        // The macOS shape: the Keychain holds the secret and Write returns null, so the
        // settings file must carry no token key whatsoever — not a null one. A stray
        // "ProtectedToken": null would read as an at-rest value that platform never wrote.
        BridgeSettingsStore store = Store(new OutOfBandTapTokenStore());

        store.Save(new BridgeSettings { Token = "tok-in-the-keychain" });

        Assert.DoesNotContain("ProtectedToken", File.ReadAllText(store.FilePath));
    }

    [Fact]
    public void Save_WithAClearedToken_DeletesTheStoredSecret_AndLoadsAsNoToken()
    {
        // Blanking the token field in the dialog means "forget my token". Both halves of
        // the seam have to hear it: the FILE loses the at-rest value, and the platform
        // store is told to delete an out-of-band secret (Write("") is that instruction) —
        // otherwise a Keychain entry outlives the settings that referenced it.
        var tokens = new OutOfBandTapTokenStore();
        BridgeSettingsStore store = Store(tokens);
        store.Save(new BridgeSettings { Token = "tok-1" });

        BridgeSettings loaded = store.Load();
        loaded.Token = "";
        store.Save(loaded);

        Assert.Null(tokens.Held);                          // the platform secret is gone
        Assert.Equal("", store.Load().Token);
    }

    [Fact]
    public void Load_WhenThePlatformDeniesTheTokenRead_DegradesToNoToken_AndKeepsTheRest()
    {
        // Reading the secret is platform IO that can fail for reasons the Bridge can't fix:
        // a DPAPI blob from another user, a Keychain the operator declined to unlock, a
        // secrets daemon that isn't up. None of those may stop the tray launching or lose
        // the settings around the token — the operator just re-enters it in the dialog.
        BridgeSettingsStore store = Store(new FakeTapTokenStore());
        store.Save(new BridgeSettings { Host = "rec.example", Port = 9100, Token = "tok-1" });

        BridgeSettings loaded = Store(new DeniedTapTokenStore()).Load(); // must not throw

        Assert.Equal("", loaded.Token);
        Assert.Equal("rec.example", loaded.Host); // proves the file parsed, not a defaults fallback
        Assert.Equal(9100, loaded.Port);
    }

    [Fact]
    public void SaveThenLoad_RoundTripsAPinnedSelection_AndItsPerDeviceGate()
    {
        // The polymorphic half of the device list: a pin carries a device id the
        // follow-default case has no field for, so the discriminator has to survive too.
        var gate = new GateSettings(Sensitivity: 70, HangoverMs: 500, PreRollMs: 250);
        BridgeSettingsStore store = Store(new FakeTapTokenStore());
        store.Save(new BridgeSettings
        {
            Devices = [new DeviceSelection.Pinned("usb-123", "system", "USB Loopback", gate)],
        });

        var pinned = Assert.IsType<DeviceSelection.Pinned>(Assert.Single(store.Load().Devices));

        Assert.Equal("usb-123", pinned.DeviceId);
        Assert.Equal("system", pinned.Identity);
        Assert.Equal("USB Loopback", pinned.Name);
        Assert.Equal(gate, pinned.Gate);
    }

    [Fact]
    public void Load_AFileWithoutProcessOnEnd_DefaultsToTrue()
    {
        // A settings file written before ProcessOnEnd existed has no such key. The default
        // must be true so upgrading keeps the original auto-process-on-End behaviour.
        BridgeSettingsStore store = Store(new FakeTapTokenStore());
        Directory.CreateDirectory(_dir);
        File.WriteAllText(store.FilePath, "{ \"Host\": \"rec.example\", \"Port\": 8001 }");

        BridgeSettings loaded = store.Load();

        Assert.Equal("rec.example", loaded.Host); // proves the file parsed (not the corrupt fallback)
        Assert.True(loaded.ProcessOnEnd);
    }

    [Fact]
    public void Load_AMissingFile_ReturnsSeededDefaults()
    {
        // Nothing written yet: a first run must land on the environment-seeded defaults.
        BridgeSettings loaded = Store(new FakeTapTokenStore()).Load();
        BridgeSettings expected = BridgeSettings.SeedFromEnvironment();

        Assert.Equal(expected.Host, loaded.Host);
        Assert.Equal(expected.Port, loaded.Port);
    }

    [Fact]
    public void Load_ACorruptFile_FallsBackToSeededDefaults_WithoutThrowing()
    {
        BridgeSettingsStore store = Store(new FakeTapTokenStore());
        Directory.CreateDirectory(_dir);
        File.WriteAllText(store.FilePath, "{ this is not valid json at all ");

        BridgeSettings loaded = store.Load(); // must not throw
        BridgeSettings expected = BridgeSettings.SeedFromEnvironment();

        Assert.Equal(expected.Host, loaded.Host);
        Assert.Equal(expected.Port, loaded.Port);
    }

    public void Dispose()
    {
        if (Directory.Exists(_dir))
            Directory.Delete(_dir, recursive: true);
    }
}
