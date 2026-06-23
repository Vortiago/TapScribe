using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// Round-trips settings through an actual file (a temp path), so the persistence
/// path — including the DPAPI-protected token surviving Save -> file -> Load — is
/// covered, not just in-memory JSON serialisation.
/// </summary>
public class BridgeSettingsStoreTests : IDisposable
{
    private readonly string _path =
        Path.Join(Path.GetTempPath(), $"tapscribe-tray-{Guid.NewGuid():N}.json");

    [Fact]
    public void SaveThenLoad_RoundTripsAllFields_IncludingToken()
    {
        var original = new BridgeSettings
        {
            Host = "rec.example",
            Port = 9100,
            Tls = true,
            Identity = "alice",
            Name = "Alice B",
            Token = "tok-xyz",
        };

        BridgeSettingsStore.Save(original, _path);
        BridgeSettings loaded = BridgeSettingsStore.Load(_path);

        Assert.Equal("rec.example", loaded.Host);
        Assert.Equal(9100, loaded.Port);
        Assert.True(loaded.Tls);
        Assert.Equal("alice", loaded.Identity);
        Assert.Equal("Alice B", loaded.Name);
        Assert.Equal("tok-xyz", loaded.Token); // DPAPI blob round-trips through the file
    }

    [Fact]
    public void SaveThenLoad_RoundTripsDeviceSelectionsAndTheirPerDeviceGate()
    {
        var micGate = new GateSettings(Sensitivity: 45, HangoverMs: 800, PreRollMs: 300);
        var systemGate = new GateSettings(Sensitivity: 70, HangoverMs: 500, PreRollMs: 250);
        var original = new BridgeSettings
        {
            Devices =
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "My Mic", micGate),
                new DeviceSelection.Pinned("usb-123", "system", "USB Loopback", systemGate),
            ],
        };

        BridgeSettingsStore.Save(original, _path);
        BridgeSettings loaded = BridgeSettingsStore.Load(_path);

        Assert.Equal(2, loaded.Devices.Count);

        // Polymorphic selections survive the file faithfully (kind + per-case fields +
        // the per-device gate).
        var follow = Assert.IsType<DeviceSelection.FollowDefault>(loaded.Devices[0]);
        Assert.Equal(DeviceFlow.Capture, follow.Flow);
        Assert.Equal("mic", follow.Identity);
        Assert.Equal("My Mic", follow.Name);
        Assert.Equal(micGate, follow.Gate);

        var pinned = Assert.IsType<DeviceSelection.Pinned>(loaded.Devices[1]);
        Assert.Equal("usb-123", pinned.DeviceId);
        Assert.Equal("system", pinned.Identity);
        Assert.Equal(systemGate, pinned.Gate);
    }

    [Fact]
    public void Load_CorruptFile_FallsBackToSeededDefaults_WithoutThrowing()
    {
        File.WriteAllText(_path, "{ this is not valid json at all ");

        BridgeSettings loaded = BridgeSettingsStore.Load(_path); // must not throw
        BridgeSettings expected = BridgeSettings.SeedFromEnvironment();

        Assert.Equal(expected.Host, loaded.Host);
        Assert.Equal(expected.Port, loaded.Port);
    }

    [Fact]
    public void Load_MissingFile_ReturnsSeededDefaults()
    {
        // _path does not exist (fresh temp GUID, not yet written).
        BridgeSettings loaded = BridgeSettingsStore.Load(_path);
        BridgeSettings expected = BridgeSettings.SeedFromEnvironment();

        Assert.Equal(expected.Host, loaded.Host);
        Assert.Equal(expected.Port, loaded.Port);
    }

    public void Dispose()
    {
        if (File.Exists(_path))
            File.Delete(_path);
    }
}
