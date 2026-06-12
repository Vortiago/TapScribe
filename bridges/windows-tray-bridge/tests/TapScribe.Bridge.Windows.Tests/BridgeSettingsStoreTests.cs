namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// Round-trips settings through an actual file (a temp path), so the persistence
/// path — including the DPAPI-protected token surviving Save -> file -> Load — is
/// covered, not just in-memory JSON serialisation.
/// </summary>
public class BridgeSettingsStoreTests : IDisposable
{
    private readonly string _path =
        Path.Combine(Path.GetTempPath(), $"tapscribe-tray-{Guid.NewGuid():N}.json");

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
