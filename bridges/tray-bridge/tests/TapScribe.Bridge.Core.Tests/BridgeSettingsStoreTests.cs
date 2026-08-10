using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Round-trips the tray's settings through a real file under an injected directory, with
/// the tap token's at-rest translation supplied by an injected <see cref="ITapTokenStore"/>.
/// This is the PORTABLE half of the storage layer — a platform contributes only the token
/// store behind that seam (DPAPI on Windows, the Keychain on macOS) — so every persistence
/// rule is pinned here and runs on every OS instead of only on the Windows CI job.
/// </summary>
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

    public void Dispose()
    {
        if (Directory.Exists(_dir))
            Directory.Delete(_dir, recursive: true);
    }
}
